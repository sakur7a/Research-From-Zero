"""Public schemas. Machine observations never replace user assertions."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlsplit, urlunsplit, unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_url(value: str) -> str:
    """Validate stored links; this does not authorize fetching the address."""
    value = value.strip()
    if not value:
        return ""
    if any(ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("链接不能包含控制字符或反斜线")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("请使用完整的 http(s) 链接")
    if parsed.username or parsed.password:
        raise ValueError("链接不能包含账号或密码")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("链接端口无效") from exc
    return value


ARXIV_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-zA-Z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$")
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.I)


def normalize_arxiv(value: str) -> str:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        p = urlsplit(value)
        if p.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise ValueError("不是 arXiv 链接")
        value = re.sub(r"^/(abs|pdf)/", "", p.path)
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I).removesuffix(".pdf")
    if value and not ARXIV_RE.fullmatch(value):
        raise ValueError("arXiv ID 格式不正确")
    return value


def normalize_doi(value: str) -> str:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        p = urlsplit(value)
        if p.hostname not in {"doi.org", "dx.doi.org"}:
            raise ValueError("不是 DOI 链接")
        value = unquote(p.path.lstrip("/"))
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    if value and (not DOI_RE.fullmatch(value) or any(ord(c) < 32 for c in value)):
        raise ValueError("DOI 格式不正确")
    return value.lower()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadingStatus(str, Enum):
    inbox = "inbox"
    reading = "reading"
    read = "read"
    baseline = "baseline"
    archived = "archived"


class PaperInput(StrictModel):
    title: str = Field(min_length=1, max_length=600)
    authors: list[str] = Field(default_factory=list, max_length=100)
    year: int | None = Field(default=None, ge=1800, le=2100)
    venue: str = Field(default="", max_length=200)
    abstract: str = Field(default="", max_length=30000)
    doi: str = Field(default="", max_length=300)
    arxiv_id: str = Field(default="", max_length=100)
    paper_url: str = Field(default="", max_length=2000)
    topics: list[str] = Field(default_factory=list, max_length=20)
    status: ReadingStatus = ReadingStatus.inbox
    notes: str = Field(default="", max_length=30000)
    version_label: str = Field(default="", max_length=100)
    zotero_item_key: str = Field(default="", max_length=100)
    zotero_library_id: str = Field(default="", max_length=100)
    zotero_library_type: str = Field(default="", pattern=r"^(|user|group)$")

    _url = field_validator("paper_url")(safe_url)
    _doi = field_validator("doi")(normalize_doi)
    _arxiv = field_validator("arxiv_id")(normalize_arxiv)

    @field_validator("authors", "topics")
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        if any(not x.strip() or len(x) > 160 for x in values):
            raise ValueError("作者或方向需为 1–160 字符")
        return list(dict.fromkeys(x.strip() for x in values))


class ResourceKind(str, Enum):
    code = "code"
    checkpoint = "checkpoint"
    dataset = "dataset"
    evaluation = "evaluation"
    environment = "environment"
    demo = "demo"


class ResourceInput(StrictModel):
    kind: ResourceKind
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    ownership: str = Field(default="unconfirmed", pattern=r"^(unconfirmed|official|third_party)$")
    ownership_evidence: str = Field(default="", max_length=4000)
    claim: str = Field(default="undeclared", pattern=r"^(undeclared|promised|released)$")
    claim_evidence: str = Field(default="", max_length=4000)
    applicable_version: str = Field(default="", max_length=200)

    _url = field_validator("url")(safe_url)

    @model_validator(mode="after")
    def claims_need_evidence(self):
        if self.ownership != "unconfirmed" and not self.ownership_evidence:
            raise ValueError("标记资源归属时请记录依据（原文、页码或来源链接）")
        if self.claim != "undeclared" and not self.claim_evidence:
            raise ValueError("记录作者发布声明时请同时记录原文依据")
        return self


class MetadataRequest(StrictModel):
    identifier: str = Field(min_length=1, max_length=1000)


class TopicInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)


class Evidence(StrictModel):
    source_url: str
    locator: str = ""
    excerpt: str = Field(max_length=8000)
    category: str = "provider_metadata"


class Observation(StrictModel):
    status: str
    summary: str
    provider: str
    checked_at: str = Field(default_factory=now)
    revision: str = ""
    depth: str = "metadata_only"
    scope: str
    limitations: list[str] = Field(default_factory=list)
    indicators: dict[str, list[str]] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    discovered: list[dict[str, str]] = Field(default_factory=list)
    license_id: str = ""
    content_sha256: str = ""
