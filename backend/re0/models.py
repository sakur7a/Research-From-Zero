"""Public schemas. Machine observations never replace user assertions."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal
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


TEMPLATE_KINDS = ("direction", "task", "method", "input", "output", "experiment_condition")


class TemplateDimension(StrictModel):
    key: str = Field(min_length=1, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)


class TemplateConcept(StrictModel):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["direction", "task", "method", "input", "output", "experiment_condition"]
    label: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    parent: str | None = Field(default=None, max_length=80)

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 120 for value in cleaned):
            raise ValueError("别名须为 1–120 字符")
        return list(dict.fromkeys(cleaned))


class ResearchTemplateDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    dimensions: list[TemplateDimension] = Field(min_length=1, max_length=12)
    concepts: list[TemplateConcept] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def template_tree_is_well_formed(self):
        dimensions = [item.key for item in self.dimensions]
        keys = [item.key for item in self.concepts]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("方向模板的维度 key 不能重复")
        if len(keys) != len(set(keys)):
            raise ValueError("方向模板的概念 key 不能重复")
        if any(item.kind not in dimensions for item in self.concepts):
            raise ValueError("每个概念类型都必须出现在 dimensions 中")
        by_key = {item.key: item for item in self.concepts}
        for item in self.concepts:
            if item.parent and item.parent not in by_key:
                raise ValueError(f"概念 {item.key} 的父节点不存在")
            if item.parent == item.key:
                raise ValueError(f"概念 {item.key} 不能以自身为父节点")
        for item in self.concepts:
            seen = {item.key}
            parent = item.parent
            while parent:
                if parent in seen:
                    raise ValueError("方向模板父子关系不能形成循环")
                seen.add(parent)
                parent = by_key[parent].parent
        names: dict[str, str] = {}
        for item in self.concepts:
            for name in [item.label, *item.aliases]:
                folded = name.casefold()
                if folded in names and names[folded] != item.key:
                    raise ValueError(f"名称或别名冲突：{name}")
                names[folded] = item.key
        return self


class TopicAssignmentInput(StrictModel):
    template_version_id: str = Field(min_length=1, max_length=100)
    concept_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")


class ResearchRelationInput(StrictModel):
    relation_type: Literal["uses_method", "evaluated_on", "has_resource", "claim"]
    target: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=1, max_length=2000)
    conditions: str = Field(default="", max_length=2000)
    assertion_kind: Literal["author_statement", "model_inference", "human_confirmation"]
    source_snapshot_id: str = Field(min_length=1, max_length=100)
    paper_version_id: str = Field(default="", max_length=100)
    locator: str = Field(min_length=1, max_length=1000)
    supersedes_id: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def claim_has_conditions(self):
        if self.relation_type == "claim" and not self.conditions.strip():
            raise ValueError("claim 关系需要填写假设或适用条件")
        return self


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
    # Kept beside `status` because the two answer different questions. A 404, a 429 and a timeout
    # all collapse into an indeterminate status, and reading any of them as "not released" is the
    # mistake this field exists to make checkable.
    http_status: int | None = Field(default=None, ge=100, le=599)


# The audit vocabulary is deliberately coarse and closed. Every state answers "what did the check
# establish", never "does the resource exist": `access_failed` and `not_found_in_scope` are both
# statements about the check, and reading either as "not open source" is the mistake this exists to
# prevent. The provider's own status is kept beside it, so a 404 and a 429 stay distinguishable.
AUDIT_STATES = ("not_checked", "candidate_located", "metadata_readable", "access_required",
                "partially_available", "access_failed", "not_found_in_scope", "unsupported")
AUDIT_LABELS = {
    "not_checked": "未检查",
    "candidate_located": "候选已定位",
    "metadata_readable": "元数据可读",
    "access_required": "需申请",
    "partially_available": "部分可用",
    "access_failed": "访问失败",
    "not_found_in_scope": "检查范围内未找到",
    "unsupported": "不支持",
}

# Per-artifact-class coverage. `not_applicable` and `unknown` are different answers and are kept
# apart: "this paper needs no checkpoint" is a finding, "we could not tell" is a gap.
AUDIT_COMPONENTS = ("code_training", "code_inference", "code_evaluation", "checkpoint",
                    "dataset", "data_split", "preprocessing", "environment")
COMPONENT_STATES = ("present", "absent_in_scope", "not_applicable", "unknown",
                    "requires_access", "check_failed")
COMPONENT_LABELS = {
    "present": "有候选",
    "absent_in_scope": "检查范围内未见",
    "not_applicable": "不适用",
    "unknown": "未知",
    "requires_access": "需申请",
    "check_failed": "检查未完成",
}
# Which artifact classes a declared licence is read for. Each is stored as its own claim from its
# own source; nothing here concludes anything about permission to use.
LICENCE_COMPONENTS = ("code", "checkpoint", "dataset")

VERIFICATION_DEPTHS = ("not_checked", "metadata_only", "file_listing", "content_read")


class ComponentFinding(StrictModel):
    """One artifact class inside one resource, and where that answer came from.

    A state with no source is not an audit result, so the affirmative ones are required to carry
    one: `present` points at the file, and `absent_in_scope` points at the listing that was searched
    — because "not in this listing" is only meaningful next to the listing.
    """

    state: Literal["present", "absent_in_scope", "not_applicable", "unknown",
                   "requires_access", "check_failed"]
    paths: list[str] = Field(default_factory=list, max_length=4)
    sources: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("paths", "sources")
    @classmethod
    def bounded(cls, values):
        if any(len(item) > 600 for item in values):
            raise ValueError("路径或来源链接过长")
        return values

    @model_validator(mode="after")
    def claims_carry_a_source(self):
        if self.state in {"present", "absent_in_scope"} and not self.sources:
            raise ValueError(f"「{COMPONENT_LABELS[self.state]}」需要附可跳转的来源")
        return self


class ResourceAudit(StrictModel):
    """One resource candidate for one paper, audited field by field.

    A row is an *observation about a check*, not a verdict about a resource. Everything affirmative
    has to point at a source; everything else stays `unknown` rather than being rounded down to a
    negative, because a negative here reads as "the authors did not release it".
    """

    paper_title: str = Field(min_length=1, max_length=600)
    work_identifier: str = Field(default="", max_length=300)
    work_version: str = Field(default="", max_length=200)
    resource_url: str = Field(min_length=1, max_length=2000)
    resource_type: Literal["code", "checkpoint", "dataset", "evaluation", "environment",
                           "demo", "unknown"] = "unknown"
    candidate_origin: str = Field(default="", max_length=300)
    # A name match is a candidate. `official` needs cross-evidence a reader can open.
    attribution: Literal["unconfirmed", "official", "third_party"] = "unconfirmed"
    attribution_evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    author_declaration: Literal["undeclared", "promised", "released"] = "undeclared"
    author_declaration_evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    status: Literal["not_checked", "candidate_located", "metadata_readable", "access_required",
                    "partially_available", "access_failed", "not_found_in_scope", "unsupported"]
    provider_status: str = Field(default="", max_length=120)
    provider: str = Field(default="", max_length=60)
    summary: str = Field(default="", max_length=1000)
    # Whether a reader can obtain the resource. There is no "failed" value: a check that did not
    # complete leaves this unknown, and claiming an access failure would be a second guess about a
    # resource nobody reached.
    access: Literal["open", "requires_application", "not_applicable", "unknown"] = "unknown"
    scope: str = Field(default="", max_length=600)
    revision: str = Field(default="", max_length=120)
    checked_at: str = Field(default="", max_length=60)
    verification_depth: Literal["not_checked", "metadata_only", "file_listing",
                                "content_read"] = "not_checked"
    coverage: dict[str, ComponentFinding] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    licences: dict[str, str] = Field(default_factory=dict)
    version_match: Literal["matched", "mismatched", "unknown"] = "unknown"
    version_evidence: str = Field(default="", max_length=2000)
    limitations: list[str] = Field(default_factory=list, max_length=20)

    _url = field_validator("resource_url")(safe_url)

    @field_validator("coverage")
    @classmethod
    def known_components(cls, value):
        unknown = sorted(set(value) - set(AUDIT_COMPONENTS))
        if unknown:
            raise ValueError(f"未知的资源类别：{'、'.join(unknown)}")
        return value

    @field_validator("licences")
    @classmethod
    def known_licence_components(cls, value):
        unknown = sorted(set(value) - set(LICENCE_COMPONENTS))
        if unknown:
            raise ValueError(f"许可证只能按 {'、'.join(LICENCE_COMPONENTS)} 记录")
        if any(len(item) > 200 for item in value.values()):
            raise ValueError("许可证标识过长")
        return value

    @field_validator("limitations")
    @classmethod
    def bounded_limitations(cls, value):
        if any(not item.strip() or len(item) > 1000 for item in value):
            raise ValueError("限制说明须为 1–1000 字符")
        return value

    @model_validator(mode="after")
    def affirmative_claims_need_a_source(self):
        """The same rule `ResourceInput` already enforces, applied to the audit row."""
        if self.attribution != "unconfirmed" and not self.attribution_evidence:
            raise ValueError("标记官方或第三方归属时请附可定位的交叉证据")
        if self.author_declaration != "undeclared" and not self.author_declaration_evidence:
            raise ValueError("记录作者发布声明时请附原文出处")
        if self.version_match != "unknown" and not self.version_evidence:
            raise ValueError("判断论文与资源版本是否对应时请附依据")
        return self

    @model_validator(mode="after")
    def audit_sources_are_safe_links(self):
        """Audits are durable, exportable claims; every citation must be a real web source.

        Low-level provider observations may describe a rejected input URL, so the shared Evidence
        shape stays permissive there. Once those observations become an audit or a human revision,
        citation links must be safe HTTP(S) URLs before they can be imported or stored.
        """
        groups = [self.attribution_evidence, self.author_declaration_evidence, self.evidence]
        for group in groups:
            for item in group:
                try:
                    if not safe_url(item.source_url):
                        raise ValueError
                except ValueError as exc:
                    raise ValueError("资源审计的证据来源必须是无凭据的 http(s) 链接") from exc
        for finding in self.coverage.values():
            for source in finding.sources:
                try:
                    if not safe_url(source):
                        raise ValueError
                except ValueError as exc:
                    raise ValueError("类别覆盖的证据来源必须是无凭据的 http(s) 链接") from exc
        return self

    @property
    def label(self) -> str:
        return AUDIT_LABELS[self.status]

    def unresolved(self) -> list[str]:
        """Components this row could not answer, named — so a gap is not read as an absence."""
        return [name for name, finding in self.coverage.items()
                if finding.state in {"unknown", "check_failed"}]
