"""Read-only provider adapters. No arbitrary-URL fetches, execution, or LLM claims.

Only repository-root URLs are accepted for automatic resource checks. API
requests are constructed from validated IDs, not forwarded from user input.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import httpx

from .models import Evidence, Observation, PaperInput, normalize_arxiv, normalize_doi

ALLOWED_HOSTS = {"api.github.com", "huggingface.co", "export.arxiv.org", "api.crossref.org",
                 "api.openalex.org", "api.semanticscholar.org", "api2.openreview.net"}
MAX_BYTES = 2 * 1024 * 1024
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,150}$")
_arxiv_lock = threading.Lock()
_arxiv_last = 0.0


class ProviderError(Exception):
    def __init__(self, message: str, status: str = "indeterminate", http_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


class ProviderClient:
    """A bounded, allowlisted client.

    Credentials are scoped per host and read from the environment at call time
    (`GITHUB_TOKEN` for api.github.com, `SEMANTIC_SCHOLAR_API_KEY` for Semantic
    Scholar, and so on). A connector supplies only its own provider's header, so no
    key can travel to a host it does not belong to. No key is logged or cached.
    """

    def __init__(self, transport: httpx.BaseTransport | None = None, *,
                 max_calls: int = 8, seconds: float = 35, read_timeout: float = 8):
        self.client = httpx.Client(transport=transport, timeout=read_timeout, follow_redirects=False, trust_env=False)
        self.max_calls, self.deadline = max_calls, time.monotonic() + seconds
        self.read_timeout = read_timeout
        self.calls = 0
        self.digests: list[str] = []

    def close(self):
        self.client.close()

    def read(self, url: str, params: dict | None = None, headers: dict | None = None) -> bytes:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
                or parsed.port not in (None, 443) or parsed.username or parsed.password):
            raise ProviderError("请求目标不在允许的提供商列表内", "unsupported")
        if self.calls >= self.max_calls or time.monotonic() >= self.deadline:
            raise ProviderError("本次检查已达到请求预算；没有据此判断资源不存在")
        self.calls += 1
        request_headers = {"User-Agent": "re0/0.1.0 (local research resource checker)", "Accept": "application/json"}
        if parsed.hostname == "api.github.com":
            request_headers["X-GitHub-Api-Version"] = "2022-11-28"
            token = os.getenv("GITHUB_TOKEN", "")
            if token:
                request_headers["Authorization"] = f"Bearer {token}"
        if headers:
            # Caller-supplied per-provider credentials. The caller owns the scoping.
            request_headers.update(headers)
        try:
            with self.client.stream("GET", url, params=params, headers=request_headers,
                                    timeout=min(self.read_timeout, max(0.1, self.deadline - time.monotonic()))) as response:
                status = response.status_code
                if status != 200:
                    if status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
                        raise ProviderError("提供商限流；稍后重试，不能据此认定资源未发布", http_status=status)
                    if status in (401, 403):
                        raise ProviderError("访问被拒绝；可能需要授权，尚不能确定资源状态", "access_failed", status)
                    if status == 404:
                        raise ProviderError("接口返回 404：可能不存在、已移动或无访问权限", "indeterminate", status)
                    if 300 <= status < 400:
                        raise ProviderError("链接发生重定向；为避免越权请求，本次未跟随", http_status=status)
                    raise ProviderError(f"提供商返回 HTTP {status}，本次未完成验证", http_status=status)
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ProviderError("响应超过 2 MiB 检查预算；未扫描完整内容")
                    if time.monotonic() > self.deadline:
                        raise ProviderError("检查超时；未扫描完整内容")
        except httpx.HTTPError as exc:
            # Never echo exceptions containing credentials or remote-controlled text.
            raise ProviderError("网络连接或读取失败，本次未完成验证") from exc
        raw = bytes(data)
        self.digests.append(hashlib.sha256(raw).hexdigest())
        return raw

    def json(self, url: str, params: dict | None = None, headers: dict | None = None):
        try:
            return json.loads(self.read(url, params, headers))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError("提供商返回了无法解析的数据") from exc


def repository_identity(url: str) -> tuple[str, str, str]:
    p = urlsplit(url)
    if p.scheme != "https" or p.port not in (None, 443) or p.username or p.password:
        raise ProviderError("自动检查仅支持 HTTPS 仓库首页，不请求自定义端口", "unsupported")
    parts = p.path.strip("/").split("/")
    if p.query or p.fragment:
        raise ProviderError("请使用不带查询参数和锚点的仓库首页链接", "unsupported")
    if p.hostname == "github.com" and len(parts) == 2:
        parts[1] = parts[1].removesuffix(".git")
        if all(SEGMENT.fullmatch(x) and x not in {".", ".."} for x in parts):
            return "github", "repos", "/".join(parts)
    if p.hostname == "huggingface.co":
        category = "models"
        if parts[0] == "datasets":
            category, parts = "datasets", parts[1:]
        if len(parts) == 2 and parts[0] not in {"spaces", "models", "datasets", "collections", "papers", "api"} and all(SEGMENT.fullmatch(x) for x in parts):
            return "huggingface", category, "/".join(parts)
    raise ProviderError("此版本只自动检查 GitHub 仓库首页及 Hugging Face 模型/数据集首页；其他链接仅保存", "unsupported")


def classify_files(paths: list[str]) -> dict[str, list[str]]:
    """Filename heuristics only. A hit is a candidate, never a functional verdict."""
    result = {k: [] for k in ("training", "inference", "evaluation", "weights", "data", "environment")}
    for path in paths:
        name = PurePosixPath(path).name.lower()
        suffix = PurePosixPath(name).suffix
        is_code = suffix in {".py", ".sh", ".ipynb", ".r", ".jl"}
        if is_code and re.search(r"(^|[_-])(train|training|finetune)([_\-.]|$)", name):
            result["training"].append(path)
        if is_code and re.search(r"(^|[_-])(infer|inference|predict|sample|generate)([_\-.]|$)", name):
            result["inference"].append(path)
        if is_code and re.search(r"(^|[_-])(eval|evaluate|evaluation|benchmark)([_\-.]|$)", name):
            result["evaluation"].append(path)
        if suffix in {".safetensors", ".pt", ".pth", ".ckpt", ".bin", ".onnx"}:
            result["weights"].append(path)
        if suffix in {".parquet", ".arrow", ".csv", ".jsonl", ".h5", ".hdf5"}:
            result["data"].append(path)
        if name in {"requirements.txt", "pyproject.toml", "environment.yml", "environment.yaml", "dockerfile", "uv.lock", "poetry.lock"}:
            result["environment"].append(path)
    return {k: v[:12] for k, v in result.items()}


def github_check(client: ProviderClient, identity: str) -> Observation:
    api = f"https://api.github.com/repos/{identity}"
    home = f"https://github.com/{identity}"
    metadata = client.json(api)
    if not isinstance(metadata, dict) or not metadata.get("default_branch"):
        raise ProviderError("仓库元数据缺少默认分支")
    # Ref is resolved to a commit first, so all file evidence remains pinned.
    commit = client.json(f"{api}/commits/{quote(metadata['default_branch'], safe='')}")
    sha = commit.get("sha", "")
    if not re.fullmatch(r"[a-f0-9]{40,64}", sha):
        raise ProviderError("未取得有效的仓库提交版本")
    tree_url = f"{api}/git/trees/{sha}"
    tree = client.json(tree_url, {"recursive": "1"})
    if not isinstance(tree.get("tree"), list):
        raise ProviderError("仓库文件树响应无效")
    paths = [x["path"] for x in tree["tree"] if x.get("type") == "blob" and isinstance(x.get("path"), str)]
    indicators = classify_files(paths)
    limitations = ["仅检查默认分支文件名、README 和前 10 个 Release；未下载权重或数据、未运行代码。",
                   "候选文件名不是功能验证；未发现候选文件不表示资源不存在。",
                   "未自动验证作者身份、论文关联关系、模型变体或许可证适用范围。"]
    if tree.get("truncated"):
        limitations.append("GitHub 标记文件树 truncated=true；结果不完整，缺失项不能视为不存在。")
    evidence = [Evidence(source_url=home + "/tree/" + sha, locator="GitHub Git Trees API",
                         excerpt=f"commit={sha}; 返回 {len(paths)} 个文件条目；truncated={bool(tree.get('truncated'))}")]
    for category, hits in indicators.items():
        for path in hits[:4]:
            evidence.append(Evidence(source_url=f"{home}/blob/{sha}/{quote(path, safe='/')}",
                                     locator=path, excerpt=f"文件树中的候选路径：{path}", category="file_name_candidate"))
    discovered: list[dict[str, str]] = []
    try:
        releases = client.json(f"{api}/releases", {"per_page": "10", "page": "1"})
        if not isinstance(releases, list):
            raise ProviderError("Release 列表格式无效")
        for release in releases[:10]:
            for asset in release.get("assets", [])[:20]:
                name = str(asset.get("name", ""))[:300]
                evidence.append(Evidence(source_url=f"{home}/releases", locator=f"Release / {name}",
                                         excerpt=f"发布附件名称：{name}；声明大小：{asset.get('size', '未知')} 字节；未下载", category="release_asset"))
                if len(evidence) >= 40:
                    break
            if len(evidence) >= 40:
                break
    except ProviderError as exc:
        limitations.append("Release 检查未完成：" + str(exc))
    try:
        readme = client.json(f"{api}/readme", {"ref": sha})
        if readme.get("encoding") != "base64":
            raise ProviderError("README 内容编码不支持")
        text = base64.b64decode(readme.get("content", "")).decode("utf-8", errors="replace")
        if len(text) > 60000:
            limitations.append("README 仅扫描前 60,000 字符。")
        source = f"{home}/blob/{sha}/{quote(readme.get('path', 'README.md'), safe='/')}"
        for line_no, line in enumerate(text[:60000].splitlines(), 1):
            urls = re.findall(r'https://(?:github\.com|huggingface\.co)/[^\s<>\)\]"\'`]+', line)
            for link in urls:
                link = link.rstrip(".,;")
                try:
                    provider, category, target = repository_identity(link)
                except (ProviderError, ValueError):
                    continue
                canonical = f"https://github.com/{target}" if provider == "github" else f"https://huggingface.co/{'datasets/' if category == 'datasets' else ''}{target}"
                if canonical == home or any(x["url"] == canonical for x in discovered):
                    continue
                discovered.append({"url": canonical, "kind": "code" if provider == "github" else "dataset" if category == "datasets" else "checkpoint",
                                   "source_url": source + f"#L{line_no}", "excerpt": line[:600]})
                if len(discovered) >= 12:
                    break
            if len(discovered) >= 12:
                break
        for candidate in discovered:
            evidence.append(Evidence(source_url=candidate["source_url"], locator="README 外链（未跟随）",
                                     excerpt=candidate["excerpt"], category="resource_link_candidate"))
    except (ProviderError, ValueError) as exc:
        limitations.append("README 检查未完成：" + str(exc))
    license_id = (metadata.get("license") or {}).get("spdx_id", "")
    if license_id in {"NOASSERTION", "OTHER"}:
        license_id = ""
    return Observation(status="metadata_accessible", summary=f"仓库元数据可访问；扫描到 {len(paths)} 个文件条目，功能与可复现性尚未验证。",
                       provider="github", revision=sha, depth="file_listing", scope="默认分支文件树、README、前 10 个 Release",
                       indicators=indicators, evidence=evidence, discovered=discovered,
                       limitations=limitations, license_id=license_id)


def huggingface_check(client: ProviderClient, category: str, identity: str) -> Observation:
    api = f"https://huggingface.co/api/{category}/{identity}"
    metadata = client.json(api)
    if not isinstance(metadata, dict):
        raise ProviderError("Hub 元数据格式无效，本次无法验证")
    gated = metadata.get("gated") in (True, "auto", "manual")
    has_listing = isinstance(metadata.get("siblings"), list)
    if not has_listing and not gated:
        raise ProviderError("Hub 元数据未返回文件清单，本次无法验证")
    files = [x["rfilename"] for x in metadata.get("siblings", []) or [] if isinstance(x, dict) and isinstance(x.get("rfilename"), str)] if has_listing else []
    prefix = "datasets/" if category == "datasets" else ""
    home = f"https://huggingface.co/{prefix}{identity}"
    revision = metadata.get("sha", "")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40,64}", revision):
        revision = ""
    license_id = (metadata.get("cardData") or {}).get("license", "")
    if not isinstance(license_id, str):
        license_id = ""
    evidence = [Evidence(source_url=api, locator="gated / siblings / sha / cardData.license",
                         excerpt=f"gated={metadata.get('gated')}; sha={revision}; 文件名：{', '.join(files[:30])}"[:8000])]
    for path in files[:8]:
        evidence.append(Evidence(source_url=f"{home}/blob/{revision or 'main'}/{quote(path, safe='/')}",
                                 locator=path, excerpt="Hub 文件清单列出了该路径；未请求文件字节", category="file_name_candidate"))
    limitations = ["只读取模型或数据集元数据及文件名；未验证文件下载、模型加载或实验结果。",
                   "权重和数据文件名不能证明它们与论文使用的版本、训练或评测协议匹配。",
                   "许可证值来自模型/数据集卡声明；本工具不判断使用权限。"]
    if not revision:
        limitations.append("提供商未返回提交版本；文件链接使用 main，内容可能随时间改变。")
    if not has_listing:
        limitations.append("接口声明需要申请访问，但未返回文件清单；空候选列表不代表没有文件。")
    return Observation(status="gated" if gated else "metadata_accessible", provider="huggingface",
                       summary="元数据可访问，但资源需要申请访问；本次未下载文件。" if gated else "元数据和文件清单可访问；尚未验证文件下载及使用。",
                       revision=revision, depth="file_listing" if has_listing else "metadata_only", scope="Hub 元数据、访问门槛声明与 siblings 文件列表（若提供）",
                       indicators=classify_files(files), evidence=evidence, license_id=license_id,
                       limitations=limitations)


def check_resource(url: str, transport: httpx.BaseTransport | None = None) -> Observation:
    provider = "unsupported"
    client = ProviderClient(transport)
    try:
        provider, category, identity = repository_identity(url)
        result = github_check(client, identity) if provider == "github" else huggingface_check(client, category, identity)
        result.content_sha256 = hashlib.sha256("|".join(client.digests).encode()).hexdigest()
        return result
    except (ProviderError, ValueError, KeyError, TypeError, AttributeError) as exc:
        reason = str(exc) if isinstance(exc, ProviderError) else "提供商响应格式异常；没有据此认定资源不存在"
        return Observation(status=exc.status if isinstance(exc, ProviderError) else "indeterminate", provider=provider,
                           summary=reason, scope="仅限允许提供商的公开元数据接口", depth="not_verified",
                           evidence=[Evidence(source_url=url, locator="请求结果", excerpt=reason, category="check_outcome")],
                           limitations=["本次没有完成内容验证；失败或未支持不等于资源未开放。"])
    finally:
        client.close()


def resolve_metadata(identifier: str, transport: httpx.BaseTransport | None = None) -> PaperInput:
    """Resolve one DOI/arXiv ID. Never fetch the supplied URL directly."""
    client = ProviderClient(transport)
    try:
        try:
            arxiv_id = normalize_arxiv(identifier)
        except ValueError:
            arxiv_id = ""
        if arxiv_id:
            global _arxiv_last
            with _arxiv_lock:
                delay = 3 - (time.monotonic() - _arxiv_last)
                if delay > 0:
                    time.sleep(delay)
                _arxiv_last = time.monotonic()
                raw = client.read("https://export.arxiv.org/api/query", {"id_list": arxiv_id, "max_results": "1"})
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ProviderError("拒绝包含外部实体声明的 XML")
            root = ET.fromstring(raw)
            ns = {"a": "http://www.w3.org/2005/Atom", "ar": "http://arxiv.org/schemas/atom"}
            entry = root.find("a:entry", ns)
            if entry is None or entry.findtext("a:title", "", ns).lower() == "error":
                raise ProviderError("arXiv 没有返回有效条目")
            returned_id = normalize_arxiv(entry.findtext("a:id", "", ns))
            if re.sub(r"v\d+$", "", returned_id) != re.sub(r"v\d+$", "", arxiv_id):
                raise ProviderError("arXiv 返回的论文 ID 与请求不符")
            if re.search(r"v\d+$", arxiv_id) and returned_id != arxiv_id:
                raise ProviderError("arXiv 返回的版本与明确请求的版本不符；未自动替换")
            return PaperInput(title=" ".join(entry.findtext("a:title", "", ns).split()),
                              authors=[x.text for x in entry.findall("a:author/a:name", ns) if x.text],
                              year=int(entry.findtext("a:published", "", ns)[:4]),
                              abstract=" ".join(entry.findtext("a:summary", "", ns).split()),
                              arxiv_id=returned_id, doi=entry.findtext("ar:doi", "", ns),
                              paper_url=f"https://arxiv.org/abs/{returned_id}",
                              version_label=(re.search(r"v\d+$", returned_id).group() if re.search(r"v\d+$", returned_id) else ""))
        doi = normalize_doi(identifier)
        if not doi:
            raise ValueError("请输入 DOI 或 arXiv ID")
        data = client.json(f"https://api.crossref.org/works/{quote(doi, safe='')}")
        item = data.get("message", {})
        if not item.get("title"):
            raise ProviderError("Crossref 没有返回论文标题")
        returned_doi = normalize_doi(item.get("DOI", ""))
        if returned_doi != doi:
            raise ProviderError("Crossref 返回的 DOI 与请求不符")
        date = (item.get("published") or item.get("issued") or {}).get("date-parts", [[]])[0]
        return PaperInput(title=item["title"][0], authors=[" ".join(filter(None, [x.get("given"), x.get("family")])) or x.get("name", "Unknown") for x in item.get("author", [])[:100]],
                          year=date[0] if date else None, venue=(item.get("container-title") or [""])[0],
                          abstract=re.sub(r"<[^>]+>", "", item.get("abstract", "")), doi=doi,
                          paper_url=f"https://doi.org/{doi}")
    except (ET.ParseError, KeyError, TypeError, IndexError) as exc:
        raise ProviderError("元数据格式不符合预期，请改用手动录入") from exc
    finally:
        client.close()
