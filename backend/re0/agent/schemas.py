"""Public task contracts. A model can propose observations, not authorize writes."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


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


class TaskDefaults(StrictModel):
    """Workspace-wide defaults for new tasks. Never holds credentials."""

    max_model_calls: int = Field(default=12, ge=2, le=24)
    max_tool_calls: int = Field(default=20, ge=1, le=40)
    attempt_seconds: int = Field(default=360, ge=30, le=900)
    use_library: bool = False

    def merged(self, task: "TaskInput") -> "TaskDefaults":
        """A task may omit budgets; the stored workspace default then applies.

        Explicit per-task values still win, so an existing client can keep
        sending a narrower budget for one run.
        """
        supplied = {name: getattr(task, name) for name in BUDGET_FIELDS if getattr(task, name) is not None}
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


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=5, ge=1, le=8)


class PaperSearchArgs(SearchArgs):
    source: Literal["arxiv", "crossref"] = "arxiv"


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


class Report(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    limitations: list[str] = Field(min_length=1, max_length=12)
    outcome: Literal["findings", "insufficient_evidence"]

    @field_validator("limitations")
    @classmethod
    def bounded_limits(cls, limits):
        if any(not x.strip() or len(x) > 1000 for x in limits):
            raise ValueError("限制说明过长或为空")
        return limits
