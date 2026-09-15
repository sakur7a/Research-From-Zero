"""Read-only research tools. Model arguments never become arbitrary network destinations."""
from __future__ import annotations

import base64
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlsplit

import httpx
from pydantic import ValidationError

from ..models import PaperInput, normalize_arxiv
from ..providers import (ProviderClient, ProviderError, check_resource, resolve_metadata,
                         repository_identity, _arxiv_lock)
from .schemas import (SearchArgs, PaperSearchArgs, ResolveArgs, ResourceArgs, HubSearchArgs,
                      FileArgs, PlanArgs, Report)

TOOL_TYPES = {
    "update_plan": (PlanArgs, "Publish or revise a short user-visible research plan. Do not include private chain-of-thought."),
    "search_papers": (PaperSearchArgs, "Search arXiv or Crossref for real paper metadata and abstracts. Results are not full paper text."),
    "resolve_paper": (ResolveArgs, "Resolve a DOI/arXiv identifier to source-derived paper metadata."),
    "search_repositories": (SearchArgs, "Search public GitHub repositories. Name matches do NOT establish official authorship."),
    "search_hub": (HubSearchArgs, "Search Hugging Face models/datasets. Returns candidates, NOT verified paper-resource relationships."),
    "inspect_resource": (ResourceArgs, "Inspect a GitHub repository root or Hugging Face model/dataset. Files, release metadata and resource links; no code execution or downloads."),
    "read_repository_file": (FileArgs, "Read a bounded text/code file from a GitHub repository, pinned to a resolved commit; never execute it."),
    "search_release_discussions": (SearchArgs, "Search GitHub issues/PRs for release/checkpoint discussions. Include repo:owner/name in query. Discussion is a declaration, not a verified release."),
    "search_library": (SearchArgs, "Search user-authorized local library metadata. Never returns private notes or fictional demo data."),
    "search_web": (SearchArgs, "Search the public web using configured Tavily. Returns search snippets, NOT fetched full pages."),
    "finish_report": (Report, "Submit a structured final report. Every finding MUST cite evidence IDs actually returned by tools; summarize limitations explicitly."),
}


def specifications(use_library=False, web_enabled=False):
    return [{"type": "function", "function": {"name": name, "description": description, "parameters": schema.model_json_schema()}}
            for name, (schema, description) in TOOL_TYPES.items()
            if (name != "search_library" or use_library) and (name != "search_web" or web_enabled)]


def doc(url, content, *, kind="source", locator="", paper=None):
    # Source locators from APIs are not subsequently fetched. The UI permits only safe links.
    item = {"source_url": str(url)[:800], "content": str(content)[:5000], "kind": kind, "locator": str(locator)[:400]}
    if paper is not None:
        item["paper"] = PaperInput.model_validate(paper).model_dump(mode="json")
    return item


class ResearchTools:
    def __init__(self, library, transport=None):
        self.library, self.transport = library, transport

    @property
    def web_enabled(self):
        return bool(os.getenv("TAVILY_API_KEY"))

    def execute(self, name, raw_args, *, use_library=False):
        if name not in TOOL_TYPES or name in {"update_plan", "finish_report"}:
            raise ValueError("未知或非检索工具")
        args = TOOL_TYPES[name][0].model_validate(raw_args)
        if name == "search_library" and not use_library:
            raise ValueError("用户未授权发送文献库元数据")
        if name == "search_web" and not self.web_enabled:
            raise ValueError("尚未配置网页检索服务，不能假装已经搜索")
        method = getattr(self, name)
        return method(args)

    def search_papers(self, args):
        client = ProviderClient(self.transport)
        try:
            if args.source == "crossref":
                url = "https://api.crossref.org/works"
                data = client.json(url, {"query.bibliographic": args.query, "rows": args.limit})
                documents = []
                for item in data.get("message", {}).get("items", [])[:args.limit]:
                    doi = item.get("DOI", "")
                    title = " ".join(item.get("title", []))[:600]
                    if not doi or not title:
                        continue
                    date = item.get("issued", {}).get("date-parts", [[]])[0]
                    paper = PaperInput(title=title, doi=doi,
                        authors=[((" ".join([a.get("given", ""), a.get("family", "")])).strip() or a.get("name", "Unknown"))[:160] for a in item.get("author", [])[:30]],
                        year=date[0] if date and isinstance(date[0], int) and 1900 <= date[0] <= 2100 else None,
                        paper_url="https://doi.org/" + doi)
                    documents.append(doc(paper.paper_url, json.dumps(paper.model_dump(mode="json"), ensure_ascii=False), kind="paper", locator="Crossref metadata", paper=paper))
                return {"documents": documents, "scope": "Crossref 书目匹配；未阅读全文", "query": args.query}
            # A search is bounded and serialized according to arXiv API guidance.
            from .. import providers
            with _arxiv_lock:
                delay = 3.0 - (time.monotonic() - providers._arxiv_last)
                if delay > 0 and self.transport is None:
                    time.sleep(delay)
                raw = client.read("https://export.arxiv.org/api/query", {
                    "search_query": args.query if re.search(r"\b(all|ti|au|abs|cat):", args.query) else "all:" + args.query,
                    "start": 0, "max_results": args.limit, "sortBy": "relevance"})
                providers._arxiv_last = time.monotonic()
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ProviderError("拒绝包含实体声明的 XML")
            root = ET.fromstring(raw)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            documents = []
            for entry in root.findall("a:entry", ns)[:args.limit]:
                identifier = normalize_arxiv(entry.findtext("a:id", "", ns))
                title = " ".join(entry.findtext("a:title", "", ns).split())
                if not identifier or not title:
                    continue
                abstract = " ".join(entry.findtext("a:summary", "", ns).split())[:8000]
                published = entry.findtext("a:published", "", ns)
                paper = PaperInput(title=title, arxiv_id=identifier, abstract=abstract,
                    authors=[x.findtext("a:name", "", ns) for x in entry.findall("a:author", ns)][:30],
                    year=int(published[:4]) if published[:4].isdigit() else None,
                    paper_url="https://arxiv.org/abs/" + identifier)
                documents.append(doc(paper.paper_url, title + "\n" + abstract, kind="paper", locator="arXiv Atom title/abstract; not full text", paper=paper))
            return {"documents": documents, "scope": "arXiv 元数据与摘要，不是全文分析", "query": args.query}
        finally:
            client.close()

    def resolve_paper(self, args):
        paper = resolve_metadata(args.identifier, self.transport)
        return {"documents": [doc(paper.paper_url, json.dumps(paper.model_dump(mode="json"), ensure_ascii=False), kind="paper", locator="DOI/arXiv metadata resolution", paper=paper)], "scope": "元数据，不是论文全文"}

    def search_repositories(self, args):
        client = ProviderClient(self.transport)
        try:
            obj = client.json("https://api.github.com/search/repositories", {"q": args.query, "per_page": args.limit})
            docs = [doc("https://github.com/" + x["full_name"], json.dumps({
                "full_name": x["full_name"], "description": x.get("description"), "updated_at": x.get("updated_at"),
                "official": "unverified"}, ensure_ascii=False), kind="repository", locator="GitHub repository search")
                    for x in obj.get("items", [])[:args.limit]]
            return {"documents": docs, "scope": "仓库候选；未证明论文关联或官方身份", "incomplete_results": bool(obj.get("incomplete_results"))}
        finally:
            client.close()

    def search_hub(self, args):
        client = ProviderClient(self.transport)
        try:
            rows = client.json("https://huggingface.co/api/" + args.kind, {"search": args.query, "limit": args.limit})
            if not isinstance(rows, list):
                raise ProviderError("Hub 搜索响应格式无效")
            documents = []
            for x in rows[:args.limit]:
                identity = str(x.get("id", ""))
                url = "https://huggingface.co/" + ("datasets/" if args.kind == "datasets" else "") + identity
                try:
                    repository_identity(url)
                except ProviderError:
                    continue
                documents.append(doc(url, json.dumps({"id": identity, "kind": args.kind, "paper_relationship": "unverified"}, ensure_ascii=False), kind="resource", locator="Hugging Face Hub search"))
            return {"documents": documents, "scope": "Hub 名称匹配候选；权重、数据和论文的对应关系需进一步查证"}
        finally:
            client.close()

    def inspect_resource(self, args):
        observation = check_resource(args.url, self.transport).model_dump(mode="json")
        return {"documents": [doc(args.url, json.dumps({k: v for k, v in observation.items() if k != "evidence"}, ensure_ascii=False),
                                         kind="resource_check", locator="bounded provider inspection"),
                *[doc(x["source_url"], x["excerpt"], locator=x["locator"]) for x in observation["evidence"][:7]]],
                "scope": observation["scope"], "status": observation["status"],
                "limitations": observation["limitations"], "discovered": observation["discovered"]}

    def read_repository_file(self, args):
        provider, _, identity = repository_identity("https://github.com/" + args.repository)
        parts = args.path.split("/")
        if any(not p or p in {".", ".."} for p in parts) or "\\" in args.path:
            raise ValueError("文件路径无效")
        if not re.fullmatch(r"[\w./-]+", args.ref) or ".." in args.ref:
            raise ValueError("版本标识无效")
        if not re.search(r"\.(md|txt|py|sh|json|toml|yml|yaml|r|rst|cfg|ini)$", args.path, re.I) and args.path.lower() not in {"dockerfile", "license", "readme"}:
            raise ValueError("只允许读取文档、配置或源码文本；不下载权重或执行文件")
        client = ProviderClient(self.transport)
        try:
            api = "https://api.github.com/repos/" + identity
            commit = client.json(api + "/commits/" + quote(args.ref, safe=""))
            sha = commit.get("sha", "")
            if not re.fullmatch(r"[a-f0-9]{40,64}", sha):
                raise ProviderError("无法固定仓库版本")
            obj = client.json(api + "/contents/" + quote(args.path, safe="/"), {"ref": sha})
            if obj.get("type") != "file" or obj.get("encoding") != "base64" or obj.get("size", 0) > 200000:
                raise ProviderError("该对象不是受支持的小型文本文件")
            text = base64.b64decode(obj.get("content", "")).decode("utf-8")
            lines = text.splitlines()
            if args.start_line > len(lines):
                raise ProviderError("请求起始行超出文件范围，没有生成空白证据")
            end = min(len(lines), args.start_line + args.line_count - 1)
            excerpt = "\n".join(f"{i + args.start_line}: {line}" for i, line in enumerate(lines[args.start_line-1:end]))
            return {"documents": [doc("https://github.com/" + identity + "/blob/" + sha + "/" + quote(args.path, safe="/") + f"#L{args.start_line}", excerpt,
                                       locator=f"{args.path}:{args.start_line}-{end}; commit={sha}")],
                    "scope": "固定提交的文本片段，最多 5,000 字符；未运行代码", "total_lines": len(lines), "revision": sha}
        finally:
            client.close()

    def search_release_discussions(self, args):
        if not re.search(r"\brepo:[\w.-]+/[\w.-]+", args.query):
            raise ValueError("讨论检索必须用 repo:owner/name 限定仓库")
        client = ProviderClient(self.transport)
        try:
            obj = client.json("https://api.github.com/search/issues", {"q": args.query, "per_page": args.limit})
            return {"documents": [doc(x.get("html_url", ""), str(x.get("title", "")) + "\n" + str(x.get("body", ""))[:2500],
                                       kind="discussion", locator="GitHub issue/PR body; author statement, not a release verification") for x in obj.get("items", [])[:args.limit]],
                    "scope": "讨论正文，不含所有评论；声明不等于已经发布", "incomplete_results": bool(obj.get("incomplete_results"))}
        finally:
            client.close()

    def search_library(self, args):
        words = args.query.casefold().split()
        documents = []
        for p in self.library.list_papers():
            if p["is_demo"]:
                continue
            public = {k: p[k] for k in ("title", "authors", "year", "doi", "arxiv_id", "paper_url", "abstract", "topics")}
            if not all(w in json.dumps(public, ensure_ascii=False).casefold() for w in words):
                continue
            documents.append(doc(public.get("paper_url", ""), json.dumps(public, ensure_ascii=False), kind="library_metadata", locator="local paper " + p["id"] + "; notes excluded"))
            if len(documents) >= args.limit:
                break
        return {"documents": documents, "scope": "经用户授权的书目与摘要；没有发送笔记、附件或演示数据"}

    def search_web(self, args):
        # Tavily is an optional independent search provider, not an LLM capability.
        payload = {"query": args.query, "max_results": args.limit, "search_depth": "basic", "include_answer": False, "include_raw_content": False}
        try:
            with httpx.Client(transport=self.transport, timeout=15, follow_redirects=False, trust_env=False) as client:
                with client.stream("POST", "https://api.tavily.com/search", headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"]}, json=payload) as response:
                    if response.status_code != 200:
                        raise ProviderError(f"网页搜索服务返回 HTTP {response.status_code}")
                    data = bytearray()
                    deadline = time.monotonic() + 15
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 1024 * 1024 or time.monotonic() > deadline:
                            raise ProviderError("网页搜索响应超过大小或时间预算")
            obj = json.loads(data)
            return {"documents": [doc(x.get("url", ""), str(x.get("title", "")) + "\n" + str(x.get("content", "")),
                                       kind="search_snippet", locator="Tavily search snippet; full page not fetched") for x in obj.get("results", [])[:args.limit]],
                    "scope": "网页搜索片段，不是完整网页核验；没有访问任意结果 URL"}
        except httpx.HTTPError as exc:
            raise ProviderError("网页搜索网络失败") from exc
