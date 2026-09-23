"""Public task contracts. A model can propose observations, not authorize writes."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def clean_secret(value: SecretStr) -> SecretStr:
    """Keys are single-line printable ASCII; anything else is rejected, not truncated."""
    raw = value.get_secret_value()
    if len(raw) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in raw):
        raise ValueError("密钥格式无效")
    return value


class ModelConfig(StrictModel):
    base_url: str = Field(min_length=8, max_length=400)
    model: str = Field(min_length=1, max_length=150, pattern=r"^[\w./:@-]+$")
    api_key: SecretStr = SecretStr("")
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    max_output_tokens: int = Field(default=3000, ge=256, le=8192)
    trust_endpoint: Literal[True]

    @field_validator("api_key")
    @classmethod
    def valid_secret(cls, value: SecretStr):
        return clean_secret(value)

    def public(self) -> dict:
        return {**self.model_dump(exclude={"api_key", "trust_endpoint"}),
                "configured": True, "has_api_key": bool(self.api_key.get_secret_value()),
                "storage": "server_memory", "protocol": "chat-completions-tools"}


class ModelListRequest(StrictModel):
    """Transient probe for `GET {base_url}/models`. The key is used for that one
    request and is never stored, returned, or written into a task record."""

    base_url: str = Field(min_length=8, max_length=400)
    api_key: SecretStr = SecretStr("")
    trust_endpoint: Literal[True]

    @field_validator("api_key")
    @classmethod
    def valid_secret(cls, value: SecretStr):
        return clean_secret(value)


BUDGET_FIELDS = ("max_model_calls", "max_tool_calls", "attempt_seconds", "use_library")


class SessionCaps(StrictModel):
    """Ceiling on a whole conversation, not on one turn.

    A follow-up is a new run, so a per-run budget alone would let an endless chain of turns spend
    without limit. The caps are cumulative across turns and can only be raised by an explicit
    `raise_session_caps` on a later request, which is recorded in that turn's snapshot.
    """

    max_session_model_calls: int = Field(default=36, ge=4, le=200)
    max_session_tool_calls: int = Field(default=80, ge=4, le=400)


class TaskDefaults(StrictModel):
    """Workspace-wide defaults for new tasks. Never holds credentials.

    These are *per-turn* budgets. The conversation-level ceiling lives in `SessionCaps` and is stored
    under its own key, because the two answer different questions and a turn budget that silently
    carried a session cap would let one field mean two things.
    """

    max_model_calls: int = Field(default=12, ge=2, le=24)
    max_tool_calls: int = Field(default=20, ge=1, le=40)
    attempt_seconds: int = Field(default=360, ge=30, le=900)
    use_library: bool = False

    def merged(self, task) -> "TaskDefaults":
        """A task may omit budgets; the stored workspace default then applies.

        Explicit per-task values still win, so an existing client can keep
        sending a narrower budget for one run. `getattr` with a default because a
        continuing turn (`RetryInput`) carries budgets but no library flag: that
        one is inherited deliberately, and its absence must read as "not given".
        """
        supplied = {name: getattr(task, name, None) for name in BUDGET_FIELDS
                    if getattr(task, name, None) is not None}
        return self.model_copy(update=supplied)


class TaskInput(StrictModel):
    goal: str = Field(min_length=5, max_length=6000)
    # None means "use the workspace default", not "unlimited".
    max_model_calls: int | None = Field(default=None, ge=2, le=24)
    max_tool_calls: int | None = Field(default=None, ge=1, le=40)
    attempt_seconds: int | None = Field(default=None, ge=30, le=900)
    use_library: bool | None = None
    consent_to_send: Literal[True]


class Approval(StrictModel):
    confirmed: Literal[True]


class TurnBudget(StrictModel):
    """The three per-turn knobs a follow-up or retry may set. Omitted means workspace default."""

    max_model_calls: int | None = Field(default=None, ge=2, le=24)
    max_tool_calls: int | None = Field(default=None, ge=1, le=40)
    attempt_seconds: int | None = Field(default=None, ge=30, le=900)


class FollowUpInput(TurnBudget):
    """One more turn on work already done: a new constraint, not a new question.

    Every field that widens what the turn may touch is explicit, because each one is a separate
    authorization. Nothing is inherited silently from the parent turn — least of all `use_library`,
    which sends local bibliography metadata to a model provider.
    """

    parent_run: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=5, max_length=6000,
                      description="what this turn adds or changes, e.g. 只保留有训练代码的两篇，并补查它们的数据划分")
    # Evidence ids from an earlier turn of the SAME conversation. An id from another conversation is
    # refused rather than quietly ignored: reuse is scoped, not a general ability to cite any row.
    reuse_evidence: list[str] = Field(default_factory=list, max_length=40)
    # Content-addressed source ids from an opt-in workspace (see re0.workspace). This is the only
    # route by which material recorded outside the conversation can enter it.
    reuse_sources: list[str] = Field(default_factory=list, max_length=40)
    workspace_id: str = Field(default="", max_length=19,
                              pattern=r"^(|ws_[0-9a-f]{16})$",
                              description="owner-scoped server workspace imported through the Web API")
    workspace: str = Field(default="", max_length=400,
                           description="local CLI only: explicit directory holding reuse_sources; "
                                       "hosted Web calls must use workspace_id")
    use_library: bool | None = None
    # Required on every continuing turn: a follow-up spends money, and spending is authorized per
    # turn rather than once for the conversation.
    authorize_spend: Literal[True]
    consent_to_send: Literal[True]
    # Only honoured when this turn's model destination differs from the parent's. Without it, old
    # material would travel to a provider the user never agreed to send it to.
    trust_new_destination: bool = False
    raise_session_caps: SessionCaps | None = None
    idempotency_key: str = Field(default="", max_length=120,
                                 description="a repeated submit with the same key returns the run it "
                                             "already created instead of starting a second turn")


class RetryInput(TurnBudget):
    """The same request again, as a new turn. Used when resume is unavailable or exhausted.

    There is no `goal` field: a retry repeats the parent's goal verbatim, so it cannot smuggle in
    new scope. New scope is a follow-up, and a follow-up has to name what it reuses.
    """

    parent_run: str = Field(min_length=1, max_length=80)
    authorize_spend: Literal[True]
    consent_to_send: Literal[True]
    trust_new_destination: bool = False
    raise_session_caps: SessionCaps | None = None
    idempotency_key: str = Field(default="", max_length=120)


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    # 25 rather than 8. At 8, one source could not contribute enough for a survey to be
    # anything but luck, and recall is bounded by this rather than by how many queries run.
    # Conversation cost is bounded separately, by the excerpt budget per tool result.
    limit: int = Field(default=5, ge=1, le=25)


PAPER_SOURCES = ("semanticscholar", "openalex", "arxiv", "openreview", "crossref")


class PaperSearchArgs(SearchArgs):
    # `all` queries every source whose credentials or public access allow it, then
    # merges duplicates. A single source stays available for a targeted recheck.
    source: Literal["all", "semanticscholar", "openalex", "arxiv", "openreview", "crossref"] = "all"
    # Several short queries in one budgeted call. A caller that instead merges JSON files by hand
    # loses which query found what and the per-source counts that make a gap visible.
    queries: list[str] | None = Field(default=None, max_length=5)
    # A subset, for rechecking two sources together. `source` stays for one at a time.
    sources: list[Literal["semanticscholar", "openalex", "arxiv", "openreview", "crossref"]] | None = \
        Field(default=None, max_length=5)
    # Either one query or several, so the single-query form keeps working unchanged.
    query: str | None = Field(default=None, min_length=1, max_length=300)
    start_year: int | None = Field(default=None, ge=1800, le=2100)
    end_year: int | None = Field(default=None, ge=1800, le=2100)
    # Paging is offered only where the cursor semantics are documented (OpenAlex, Semantic
    # Scholar). Every other source is read once and says so, rather than being paged on a guess
    # that silently stops early. `limit` is the per-page ceiling, so a paged run's recall
    # ceiling is limit * max_pages.
    max_pages: int = Field(default=1, ge=1, le=10)
    # Shared across every source in the call, so a session cannot spend more than it was given
    # no matter how many (query, source) pairs it walks.
    max_requests: int = Field(default=40, ge=1, le=200)
    # A venue constraint. Where a service can filter on a resolved stable source ID it does, and
    # the coverage block names the sources it resolved to; everywhere else the name stays a query
    # hint, because an unverified filter must never be presented as one.
    venue: str = Field(default="", max_length=200)
    # Force a re-fetch instead of serving a cached response inside its TTL.
    refresh: bool = False

    @model_validator(mode="after")
    def ordered_years(self):
        if self.start_year and self.end_year and self.end_year < self.start_year:
            raise ValueError("结束年份不能早于开始年份")
        if not self.query and not self.queries:
            raise ValueError("需要 query 或 queries 之一")
        if self.queries and any(not item.strip() for item in self.queries):
            raise ValueError("queries 里不能有空查询")
        return self

    def queries_effective(self) -> list[str]:
        """The query list a search runs, with the single-query form normalised into it."""
        return [item.strip() for item in (self.queries or ([self.query] if self.query else []))]

    def sources_effective(self) -> list[str]:
        """The source subset a search runs. An explicit subset wins over `source`."""
        return list(self.sources) if self.sources else \
            (list(PAPER_SOURCES) if self.source == "all" else [self.source])


class FullTextArgs(StrictModel):
    """A paper to read, named by identifier.

    There is no `url` field on purpose. The destination is built from a normalised identifier and a
    fixed allowlist, so a document that contains a link cannot make this tool fetch it: an
    arbitrary-URL reader reachable by a model is an SSRF surface and a paywall route at once.
    """
    identifier: str = Field(min_length=3, max_length=120,
                            description="arXiv ID such as 2312.00286v1, or an ACL Anthology ID such "
                                        "as 2024.acl-long.1 or P18-1001. A DOI is refused: resolving "
                                        "one leads to a publisher, which may be paywalled.")
    locator: str = Field(default="", max_length=80,
                         description="which chunk to return, e.g. 'p.4' or '§3¶12'. Empty means the "
                                     "first. Only one bounded slice comes back; the rest are listed "
                                     "by locator.")
    slice_chars: int = Field(default=4000, ge=200, le=12000,
                             description="ceiling on the returned slice, not on what was read")


class ResolveArgs(StrictModel):
    identifier: str = Field(min_length=3, max_length=300)


class ResourceArgs(StrictModel):
    url: str = Field(min_length=10, max_length=600)


class HubSearchArgs(SearchArgs):
    kind: Literal["models", "datasets"] = "models"


class FileArgs(StrictModel):
    repository: str = Field(min_length=3, max_length=310)
    path: str = Field(min_length=1, max_length=300)
    ref: str = Field(default="HEAD", min_length=1, max_length=150)
    start_line: int = Field(default=1, ge=1, le=100000)
    line_count: int = Field(default=100, ge=1, le=200)


class EvidenceReadArgs(StrictModel):
    """Read back a stored evidence body. The conversation only carries an excerpt,
    so the model asks for a slice by ID instead of every result staying in context."""

    evidence_id: str = Field(min_length=1, max_length=80)
    offset: int = Field(default=0, ge=0, le=100000)
    chars: int = Field(default=6000, ge=200, le=12000)


class PlanArgs(StrictModel):
    steps: list[str] = Field(min_length=1, max_length=8)

    @field_validator("steps")
    @classmethod
    def bounded_steps(cls, steps):
        if any(not x.strip() or len(x) > 240 for x in steps):
            raise ValueError("计划每步须为 1–240 字符")
        return steps


class Finding(StrictModel):
    claim: str = Field(min_length=1, max_length=1800)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    assessment: Literal["observed", "inference", "uncertain"] = "uncertain"


class ResourceLink(StrictModel):
    """A model-proposed paper/resource candidate; the owner still confirms library import."""
    paper_evidence_id: str = Field(min_length=1, max_length=80)
    resource_evidence_id: str = Field(min_length=1, max_length=80)
    relation_evidence_ids: list[str] = Field(min_length=2, max_length=8)
    rationale: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def cites_both_sides(self):
        if len(self.relation_evidence_ids) != len(set(self.relation_evidence_ids)):
            raise ValueError("候选关联的证据 ID 不能重复")
        if not {self.paper_evidence_id, self.resource_evidence_id}.issubset(self.relation_evidence_ids):
            raise ValueError("候选关联必须同时引用论文证据和资源核验证据")
        return self


class Report(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    resource_links: list[ResourceLink] = Field(default_factory=list, max_length=40)
    limitations: list[str] = Field(min_length=1, max_length=12)
    outcome: Literal["findings", "insufficient_evidence"]

    @field_validator("limitations")
    @classmethod
    def bounded_limits(cls, limits):
        if any(not x.strip() or len(x) > 1000 for x in limits):
            raise ValueError("限制说明过长或为空")
        return limits

    @model_validator(mode="after")
    def unique_resource_links(self):
        pairs = [(link.paper_evidence_id, link.resource_evidence_id) for link in self.resource_links]
        if len(pairs) != len(set(pairs)):
            raise ValueError("同一篇论文与资源的候选关联只能提交一次")
        return self
