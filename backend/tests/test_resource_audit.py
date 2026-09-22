"""Resource audit: the states, the failure semantics, and what a check is not allowed to claim.

Each case here is one of the situations issue #6 names, driven through the real `check_resource`
adapter with only the provider HTTP replaced. The point of going through the adapter rather than
hand-building an `Observation` is that the mapping from what a provider says to what the audit
claims is the thing under test.
"""
import base64
import csv as csv_module
import io
import json

import httpx
import pytest
from pydantic import ValidationError

from re0 import resource_audit, resource_matrix, result_model
from re0.models import AUDIT_COMPONENTS, ComponentFinding, Evidence, ResourceAudit
from re0.providers import ProviderError

SHA = "a" * 40
PAPER = {"title": "LayerKit: A Study", "abstract": "", "arxiv_id": "2401.00001", "doi": ""}
PUBLICATION = {"state": "preprint", "venue": "arXiv", "label": "仅见预印本版本"}


def github(tree, *, licence="Apache-2.0", repo_status=200, branch="main", releases=None,
           readme=None, truncated=False, identity="lab/paper"):
    """A GitHub repository as the API returns it. `branch=None` is an empty repository.

    An unexpected path fails the test rather than being answered, so a check that silently ran
    against the wrong repository cannot pass for the right reason.
    """
    api = f"/repos/{identity}"

    def handler(request):
        path = request.url.path
        if path == api:
            if repo_status != 200:
                return httpx.Response(repo_status, json={"message": "Not Found"})
            return httpx.Response(200, json={"default_branch": branch,
                                             "license": {"spdx_id": licence} if licence else None})
        if path == f"{api}/commits/main":
            return httpx.Response(200, json={"sha": SHA})
        if path == f"{api}/git/trees/{SHA}":
            return httpx.Response(200, json={
                "truncated": truncated,
                "tree": [{"path": item, "type": "blob"} for item in tree]})
        if path == f"{api}/releases":
            return httpx.Response(200, json=releases if releases is not None else [])
        if path == f"{api}/readme":
            if readme is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"encoding": "base64", "path": "README.md",
                                             "content": base64.b64encode(readme.encode()).decode()})
        raise AssertionError(f"unexpected request {request.url}")
    return httpx.MockTransport(handler)


def hub(*, gated=False, files=None, licence="apache-2.0", datasets=False, status=200):
    category = "datasets" if datasets else "models"

    def handler(request):
        assert request.url.path == f"/api/{category}/lab/data"
        if status != 200:
            return httpx.Response(status, json={"error": "no"})
        payload = {"sha": SHA, "gated": gated, "cardData": {"license": licence}}
        if files is not None:
            payload["siblings"] = [{"rfilename": item} for item in files]
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handler)


def audit(url, transport, *, origin="", declared=False):
    return resource_audit.audit_candidate(
        url, candidate={"url": url, "origin": origin or "摘要中自述", "declared": declared},
        paper=dict(PAPER), publication=dict(PUBLICATION), transport=transport)


GITHUB_URL = "https://github.com/lab/paper"
HUB_DATASET = "https://huggingface.co/datasets/lab/data"


def states(row) -> dict:
    return {name: finding.state for name, finding in row.coverage.items()}


# --- the eight situations the acceptance list names -------------------------------------------


def test_an_official_empty_repository_is_a_finding_not_an_access_failure():
    """GitHub answered; what it said is that there is nothing there.

    Reporting this as `access_failed` would claim a reachability problem that did not happen, and
    the reader would retry a repository that will stay empty.
    """
    row = audit(GITHUB_URL, github([], branch=None))
    assert row.status == "not_found_in_scope"
    assert row.provider_status == "empty_repository"
    assert row.verification_depth == "metadata_only"
    assert any("仓库为空" in text for text in row.limitations)
    # Every class is answered, and each answer points at the repository page it came from.
    assert states(row) == {name: "absent_in_scope" for name in AUDIT_COMPONENTS}
    assert all(finding.sources for finding in row.coverage.values())


def test_a_third_party_repository_with_the_same_name_stays_a_candidate():
    """The hard case: a repository whose identifier *exactly* matches the project name, owned by
    somebody else. Name agreement is the strongest cheap signal there is and it still proves
    nothing, so it cannot be promoted automatically."""
    tools = StubNameSearch([{"source_url": "https://github.com/someone/layerkit",
                             "content": '{"full_name": "someone/layerkit", "description": null}'},
                            {"source_url": "https://github.com/other/layer-stuff",
                             "content": '{"full_name": "other/layer-stuff", "description": null}'}])
    document = {"paper": dict(PAPER, title="LayerKit: A Study"), "locator": "fixture", "content": ""}
    found = resource_audit.discover(document, tools)
    assert "标识名与项目名一致" in found["candidates"][0]["origin"]
    assert "仅名称匹配" in found["candidates"][1]["origin"]

    resource_audit.audit_document(document, tools, resource_audit.Budget(find=1, verify=1),
                                  transport=github(["inference.py"], identity="someone/layerkit"))
    # Two candidates, a budget of one: the leading candidate is checked and the other stays a row
    # rather than disappearing, so the matrix shows both and only one carries a conclusion.
    row, unchecked = document["resource_audits"]
    assert row["verification_depth"] == "file_listing"
    assert unchecked["status"] == "not_checked" and unchecked["verification_depth"] == "not_checked"
    assert "预算" in unchecked["limitations"][0]
    assert row["attribution"] == "unconfirmed"
    assert "标识名与项目名一致" in row["candidate_origin"]
    # Promoting it is a human act, and the model refuses to record one without a source.
    revised = json.loads(json.dumps(row))
    revised["attribution"] = "third_party"
    with pytest.raises(ValidationError) as caught:
        ResourceAudit.model_validate(revised)
    assert "交叉证据" in str(caught.value)
    revised["attribution_evidence"] = [{"source_url": "https://github.com/someone/layerkit",
                                        "locator": "README", "excerpt": "个人练习项目，与论文无关。"}]
    assert ResourceAudit.model_validate(revised).attribution == "third_party"
    # And the matrix carries the doubt forward instead of dropping it.
    (line,) = resource_matrix.rows([{"paper": dict(PAPER), "resource_audits": [row],
                                     "artifact_outcome": row["status"]}])
    assert any("官方归属未确认" in item for item in line["blockers"])


def test_inference_only_code_does_not_claim_training_code():
    row = audit(GITHUB_URL, github(["inference.py", "README.md"]))
    found = states(row)
    assert found["code_inference"] == "present"
    assert found["code_training"] == "absent_in_scope"
    assert found["checkpoint"] == "absent_in_scope"
    assert row.status == "metadata_readable"
    # The affirmative conclusion carries the file it rests on, so it can be opened.
    assert row.coverage["code_inference"].paths == ["inference.py"]
    assert any(SHA in source for source in row.coverage["code_inference"].sources)


def test_adapter_weights_are_reported_as_needing_a_base_model():
    """An adapter is not a checkpoint a reader can run, and calling it one hides the missing half."""
    row = audit(GITHUB_URL, github(["adapter_model.safetensors", "adapter_config.json", "infer.py"]))
    assert row.status == "partially_available"
    assert states(row)["checkpoint"] == "present"
    assert any("adapter" in text and "基础模型" in text for text in row.limitations)
    # The matrix turns that limitation into a reason the row is not a drop-in baseline.
    (line,) = resource_matrix.rows([{"paper": dict(PAPER), "resource_audits": [row.model_dump(mode="json")],
                                     "artifact_outcome": row.status}])
    assert any("adapter" in item for item in line["blockers"])


def test_a_gated_dataset_requires_an_application_and_is_not_reported_as_absent():
    row = audit(HUB_DATASET, hub(gated="auto", files=None, datasets=True))
    assert row.status == "access_required"
    assert row.access == "requires_application"
    assert states(row) == {name: "requires_access" for name in AUDIT_COMPONENTS}
    # The distinction the whole vocabulary exists for: gated is not "not released".
    assert row.status not in {"access_failed", "not_found_in_scope"}
    assert row.licences == {"dataset": "apache-2.0"}


def test_a_dead_link_is_an_access_failure_and_never_evidence_of_nothing():
    row = audit(GITHUB_URL, github([], repo_status=404))
    assert row.status == "access_failed"
    assert row.provider_status == "HTTP 404"
    assert row.access == "unknown"
    assert any("不代表资源不存在或未开放" in text for text in row.limitations)
    assert states(row) == {name: "check_failed" for name in AUDIT_COMPONENTS}


def test_a_rate_limit_is_kept_apart_from_a_missing_resource():
    """Both are a failed check, and only the provider's own code says which one to retry."""
    limited = audit(GITHUB_URL, github([], repo_status=429))
    missing = audit(GITHUB_URL, github([], repo_status=404))
    assert limited.status == missing.status == "access_failed"
    assert limited.provider_status == "HTTP 429" and missing.provider_status == "HTTP 404"
    assert "限流" in limited.summary


def test_a_paper_that_needs_no_checkpoint_records_not_applicable_not_unknown():
    """"This work has no checkpoint" and "we could not tell" are different answers."""
    row = audit(GITHUB_URL, github(["train.py"]))
    assert states(row)["checkpoint"] == "absent_in_scope"
    revised = json.loads(json.dumps(row.model_dump(mode="json")))
    revised["coverage"]["checkpoint"] = {"state": "not_applicable"}
    human = ResourceAudit.model_validate(revised)
    assert human.coverage["checkpoint"].state == "not_applicable"
    assert "checkpoint" not in human.unresolved()
    unknown = ResourceAudit.model_validate({**revised,
                                            "coverage": {**revised["coverage"],
                                                         "dataset": {"state": "unknown"}}})
    assert unknown.unresolved() == ["dataset"]


def test_a_version_mismatch_has_to_bring_its_own_evidence():
    row = audit(GITHUB_URL, github(["train.py"]))
    assert row.version_match == "unknown"
    assert any("version_match=unknown 表示未判断" in text for text in row.limitations)
    revised = json.loads(json.dumps(row.model_dump(mode="json")))
    revised["version_match"] = "mismatched"
    with pytest.raises(ValidationError) as caught:
        ResourceAudit.model_validate(revised)
    assert "版本" in str(caught.value)
    revised["version_evidence"] = "仓库 README 声明对应 v1，论文实验使用 v2 配置。"
    assert ResourceAudit.model_validate(revised).version_match == "mismatched"


# --- the discipline around the states ----------------------------------------------------------


def test_a_truncated_listing_turns_absences_into_unknowns():
    """The provider said the listing is incomplete, so "not in it" stops meaning "not there"."""
    complete = audit(GITHUB_URL, github(["train.py"]))
    truncated = audit(GITHUB_URL, github(["train.py"], truncated=True))
    assert states(complete)["checkpoint"] == "absent_in_scope"
    assert states(truncated)["checkpoint"] == "unknown"
    assert truncated.status == "partially_available"
    assert states(truncated)["code_training"] == "present"


def test_an_affirmative_component_without_a_source_is_refused():
    with pytest.raises(ValidationError) as caught:
        ComponentFinding(state="present", paths=["train.py"])
    assert "来源" in str(caught.value)
    # An absence needs its scope too: "not in this listing" is only checkable next to the listing.
    with pytest.raises(ValidationError):
        ComponentFinding(state="absent_in_scope")
    assert ComponentFinding(state="unknown").sources == []


def test_an_unknown_component_class_is_refused_rather_than_carried():
    row = audit(GITHUB_URL, github(["train.py"])).model_dump(mode="json")
    row["coverage"]["model_card"] = {"state": "present", "sources": ["https://github.com/lab/paper"]}
    with pytest.raises(ValidationError) as caught:
        ResourceAudit.model_validate(row)
    assert "model_card" in str(caught.value)


def test_a_check_that_raises_becomes_a_row_that_says_so():
    def explode(url, transport=None):
        raise RuntimeError("boom: /home/someone/secret")

    original, resource_audit.check_resource = resource_audit.check_resource, explode
    try:
        row = resource_audit.audit_candidate(GITHUB_URL, candidate={"url": GITHUB_URL, "declared": True},
                                             paper=dict(PAPER), publication=dict(PUBLICATION))
    finally:
        resource_audit.check_resource = original
    assert row.status == "not_checked" and row.provider_status == "check_error"
    assert "boom" not in json.dumps(row.model_dump(mode="json"), ensure_ascii=False)
    assert "secret" not in json.dumps(row.model_dump(mode="json"), ensure_ascii=False)


def test_a_declared_link_is_recorded_as_the_authors_declaration_with_its_sentence():
    row = audit(GITHUB_URL, github(["train.py"]), declared=True)
    assert row.author_declaration == "released"
    (item,) = row.author_declaration_evidence
    assert item.locator == "论文摘要" and item.category == "author_declaration"
    assert item.source_url == "https://arxiv.org/abs/2401.00001"
    # A name match carries no declaration, because nobody declared anything.
    assert audit(GITHUB_URL, github(["train.py"]), origin="仅名称匹配").author_declaration == "undeclared"


# --- budgets, denominators and the exits -------------------------------------------------------


class StubNameSearch:
    """Stands in for ResearchTools so discovery runs without any network."""

    def __init__(self, hits, kinds=None):
        self.hits, self.calls = hits, []

    def execute(self, name, args):
        self.calls.append((name, args.get("kind")))
        return {"documents": self.hits if name == "search_repositories" else []}


def document(title, abstract=""):
    return {"paper": dict(PAPER, title=title, abstract=abstract), "locator": "fixture", "content": ""}


def test_a_partially_failed_name_search_is_not_reported_as_a_complete_one():
    """One endpoint answering is not three answering.

    `searched` used to be written the moment the search started, so "GitHub answered, both Hub
    endpoints were rate-limited" came back looking like a completed search that found nothing — and
    the reasons were printed and never reached the JSON at all.
    """
    class HalfBroken(StubNameSearch):
        def execute(self, name, args):
            if name == "search_hub":
                raise ProviderError("提供商限流；稍后重试", http_status=429)
            return super().execute(name, args)

    tools = HalfBroken([{"source_url": "https://github.com/lab/layerkit",
                         "content": '{"full_name": "lab/layerkit"}'}])
    item = document("LayerKit: A Study")
    budget = resource_audit.Budget(find=1)
    resource_audit.audit_document(item, tools, budget)
    assert item["artifact_search"] == "partial"
    detail = item["artifact_search_detail"]
    assert [endpoint["ok"] for endpoint in detail["endpoints"]] == [True, False, False]
    assert len(detail["failures"]) == 2 and all("限流" in text for text in detail["failures"])
    # What GitHub did return survives beside the two endpoints that never answered.
    assert [candidate["url"] for candidate in item["artifact_candidates"]] == \
        ["https://github.com/lab/layerkit"]
    # A partial search that found something is not "found nothing", so the outcome names the
    # candidate rather than the gap.
    assert item["artifact_outcome"] == "candidate_located"
    # And the run summary carries the failures, so a reader of the coverage block alone
    # still learns that two endpoints never answered.
    assert len(resource_audit.run_coverage([item], budget)["failures"]) == 2


def test_a_complete_search_with_no_hits_is_the_only_not_found_in_scope():
    """`not_found_in_scope` has to mean the search actually finished."""
    tools = StubNameSearch([])
    searched = document("LayerKit: A Study")
    resource_audit.audit_document(searched, tools, resource_audit.Budget(find=1))
    assert searched["artifact_search"] == "searched"
    assert searched["artifact_outcome"] == "not_found_in_scope"
    assert searched["artifact_search_detail"]["failures"] == []


def test_a_check_that_ran_and_failed_is_not_counted_as_one_nobody_tried(capsys):
    """A 404 reaches no depth at all, and reading that as "unchecked" hides the attempt.

    A real run caught this: the row said `access_failed` while the run summary counted it as
    unverified and the terminal printed it as 未核验 — the exact confusion the states exist to
    prevent, arriving through a field nobody was reading.
    """
    from re0 import skill_search

    item = document("LayerKit: A Study", "Code at https://github.com/lab/paper")
    budget = resource_audit.Budget(verify=1)
    resource_audit.audit_document(item, None, budget, transport=github([], repo_status=404))
    (row,) = item["resource_audits"]
    assert row["status"] == "access_failed" and row["verification_depth"] == "not_checked"
    coverage = resource_audit.run_coverage([item], budget)
    assert coverage["candidates"]["verified"] == 1 and coverage["candidates"]["unchecked"] == 0
    assert coverage["audits"]["access_failed"] == 1
    assert item["artifact_outcome"] == "access_failed"

    skill_search.print_document(1, item, False)
    printed = capsys.readouterr().out
    assert "未核验" not in printed
    assert "访问失败" in printed and "HTTP 404" in printed


def test_a_paper_with_no_project_name_does_not_spend_the_name_search_budget():
    """`--find-artifacts 10` means ten name searches, not ten papers looked at.

    A survey title has no project name to search for. Letting it consume a slot silently shortened
    the run, and the paper that lost its search was then told the budget had run out — a reason
    about money spent, for a search that was never possible.
    """
    tools = StubNameSearch([{"source_url": "https://github.com/lab/layerkit",
                             "content": '{"full_name": "lab/layerkit"}'}])
    nameless = document("A Survey of Large Language Models")
    named = document("LayerKit: A Study")
    budget = resource_audit.Budget(find=1)
    resource_audit.audit_document(nameless, tools, budget)
    resource_audit.audit_document(named, tools, budget)
    assert nameless["artifact_search"] == "skipped"
    assert named["artifact_search"] == "searched"
    assert named["artifact_search_detail"]["reason_not_run"] == ""
    assert budget.find_total == 1 and budget.find_left == 0
    assert resource_audit.run_coverage([nameless, named], budget)["name_search"]["states"] == {
        "searched": 1, "partial": 0, "failed": 0, "skipped": 1, "not-run": 0}


def test_the_budgets_are_shared_across_papers_and_the_denominators_are_reported():
    documents = [document("First: A", "Code at https://github.com/a/b"),
                 document("Second: B", "Code at https://github.com/c/d"),
                 document("A title with no project name")]
    budget = resource_audit.Budget(find=2, verify=1)
    checked = []

    def fake(url, transport=None):
        checked.append(url)
        from re0.models import Observation
        return Observation(status="metadata_accessible", summary="ok", provider="github",
                           depth="file_listing", scope="tree", revision=SHA,
                           evidence=[Evidence(source_url="https://github.com/a/b/tree/" + SHA,
                                              locator="tree", excerpt="commit",
                                              category="provider_metadata")])

    original, resource_audit.check_resource = resource_audit.check_resource, fake
    try:
        for item in documents:
            resource_audit.audit_document(item, None, budget)
    finally:
        resource_audit.check_resource = original
    # One check for the whole run, spent on the first paper's first candidate.
    assert checked == ["https://github.com/a/b"]
    coverage = resource_audit.run_coverage(documents, budget)
    assert coverage["papers"] == 3 and coverage["name_search"]["denominator"] == 3
    assert coverage["name_search"]["states"] == {"searched": 0, "partial": 0, "failed": 0,
                                                 "skipped": 0, "not-run": 3}
    assert coverage["candidates"] == {"found": 2, "declared": 2, "name_matched": 0, "verified": 1,
                                      "unchecked": 1, "budget": 1, "remaining": 0}
    assert coverage["audits"]["metadata_readable"] == 1 and coverage["audits"]["not_checked"] == 1


def test_a_name_search_budget_is_spent_per_paper_and_the_reason_is_kept():
    tools = StubNameSearch([{"source_url": "https://github.com/lab/layerkit",
                             "content": '{"full_name": "lab/layerkit"}'}])
    documents = [document("LayerKit: A"), document("LayerKit: B")]
    budget = resource_audit.Budget(find=1)
    for item in documents:
        resource_audit.audit_document(item, tools, budget)
    reasons = [item["artifact_search_detail"]["reason_not_run"] for item in documents]
    assert reasons == ["", "budget"]
    coverage = resource_audit.run_coverage(documents, budget)
    assert coverage["name_search"]["states"]["not-run"] == 1
    assert coverage["name_search"]["spent"] == 1 and coverage["name_search"]["remaining"] == 0


def test_the_audit_survives_the_versioned_result_instead_of_landing_in_unrecognised():
    """The acceptance is about the field reaching the exits, not about it existing somewhere."""
    row = audit(GITHUB_URL, github(["train.py"]))
    payload = {"documents": [{"source_url": "https://arxiv.org/abs/2401.00001", "kind": "paper",
                              "locator": "fixture", "content": "body", "paper": dict(PAPER),
                              "artifact_candidates": [{"url": GITHUB_URL, "origin": "摘要中自述"}],
                              "artifact_search": "searched",
                              "artifact_search_detail": {"failures": [], "project_name": "LayerKit",
                                                         "reason_not_run": "", "endpoints": []},
                              "artifact_outcome": "metadata_readable",
                              "resource_audits": [row.model_dump(mode="json")]}],
               "audit": {"papers": 1}}
    structure = result_model.normalize(payload)
    normalized = structure["documents"][0]
    assert "unrecognised" not in normalized
    assert normalized["resource_audits"][0]["status"] == "metadata_readable"
    assert normalized["resource_audits"][0]["coverage"]["code_training"]["state"] == "present"
    assert normalized["artifact_search_detail"]["project_name"] == "LayerKit"
    assert structure["audit"] == {"papers": 1}
    assert result_model.unknown_field_names(structure) == []


def test_inspect_resource_carries_the_audit_row_into_the_mcp_summary():
    from re0 import mcp_server
    from re0.agent.tools import ResearchTools

    tools = ResearchTools(None, github(["train.py", "model.safetensors"]))
    text, structure, failed = mcp_server.call_tool(tools, "inspect_resource", {"url": GITHUB_URL},
                                                   web_enabled=False)
    assert not failed
    (row,) = structure["documents"][0]["resource_audits"]
    assert row["status"] == "metadata_readable"
    assert states(ResourceAudit.model_validate(row))["checkpoint"] == "present"
    # The summary names the state and the classes it did not resolve, so a reader of the text alone
    # is not left with a bare "metadata_readable".
    assert "resource audit: metadata_readable" in text
    assert "present (filename candidates, not verified)" in text
    assert "attribution unconfirmed" in text and "version_match unknown" in text


def test_a_workspace_keeps_the_audit_beside_the_source_without_changing_its_identity(tmp_path):
    """A re-check must not mint a second id for one repository, and must not drop the audit."""
    from re0.workspace import Workspace

    row = audit(GITHUB_URL, github(["train.py"])).model_dump(mode="json")
    source = {"source_url": GITHUB_URL, "kind": "resource_check", "locator": "bounded inspection",
              "content": "body", "resource_audits": [row], "artifact_outcome": "metadata_readable"}
    workspace = Workspace(tmp_path / "ws").open()
    identifier = workspace.record(source, tool="inspect_resource")
    stored = workspace.read(identifier)
    assert stored["resource_audits"][0]["status"] == "metadata_readable"
    assert stored["artifact_outcome"] == "metadata_readable"
    # The audit is outside the identity: the same source without it has the same id.
    without = {key: value for key, value in source.items()
               if key not in {"resource_audits", "artifact_outcome"}}
    assert workspace.record(without, tool="inspect_resource") == identifier


# --- the matrix --------------------------------------------------------------------------------


def audited_document(title, tree, abstract=""):
    item = document(title, abstract)
    resource_audit.audit_document(item, None, resource_audit.Budget(verify=1),
                                  transport=github(tree))
    return item


def test_the_matrix_gives_a_paper_with_no_candidate_a_row_of_its_own():
    """Dropping it would make "searched and found nothing" and "the search failed" look alike."""
    found = document("LayerKit: A Study", "Code at https://github.com/lab/paper")
    resource_audit.audit_document(found, None, resource_audit.Budget(verify=1),
                                  transport=github(["train.py"]))
    failed = document("OtherWork: B", "no link")
    failed["artifact_search"] = "failed"
    failed["artifact_outcome"] = "access_failed"
    failed["artifact_search_detail"] = {"project_name": "OtherWork", "reason_not_run": "",
                                        "endpoints": [], "failures": ["GitHub 名称检索: HTTP 429"]}
    payload = resource_matrix.matrix([found, failed], {"name_search": {}, "candidates": {}},
                                     generated_at="2026-09-22T00:00:00")
    assert payload["papers"] == 2 and len(payload["rows"]) == 2
    absent = payload["rows"][1]
    assert absent["status"] == "access_failed" and absent["resource_url"] == ""
    assert any("名称检索未完成" in item for item in absent["blockers"])
    assert absent["limitations"] == ["GitHub 名称检索: HTTP 429"]


def test_a_spreadsheet_formula_in_a_foreign_string_cannot_execute():
    """Every cell here arrived from another service, and `=` starts a formula in a spreadsheet."""
    hostile = document("=HYPERLINK(\"http://evil\",\"x\")", "")
    hostile["resource_audits"] = [resource_audit.unchecked_audit(
        "https://github.com/lab/paper", candidate={"url": "https://github.com/lab/paper",
                                                   "origin": "+cmd|' /C calc'!A0"},
        paper={"title": "=1+1"}, publication={}, reason="测试用").model_dump(mode="json")]
    hostile["artifact_outcome"] = "not_checked"
    text = resource_matrix.csv_text(resource_matrix.matrix([hostile], {}, generated_at="x"))
    rows = list(csv_module.reader(io.StringIO(text)))
    body = rows[1]
    for cell in body:
        assert not cell.startswith(("=", "+", "-", "@", "\t", "\r")), cell
    # The value is still readable; the quote is the standard prefix, not a redaction.
    assert any(cell.startswith("'=HYPERLINK") for cell in body)
    assert any("'+cmd|" in cell for cell in body)


def test_only_links_a_reader_can_open_reach_the_matrix():
    row = resource_audit.unchecked_audit(
        "https://github.com/lab/paper", candidate={"url": "https://github.com/lab/paper"},
        paper=dict(PAPER), publication={}, reason="测试用").model_dump(mode="json")
    row["coverage"]["checkpoint"] = {"state": "present", "paths": ["w.safetensors"],
                                     "sources": ["javascript:alert(1)", "file:///etc/passwd",
                                                 "https://github.com/lab/paper/blob/x/w.safetensors"]}
    (line,) = resource_matrix.rows([{"paper": dict(PAPER), "resource_audits": [row],
                                     "artifact_outcome": "not_checked"}])
    assert line["sources"] == ["https://github.com/lab/paper/blob/x/w.safetensors"]


def test_the_three_exports_are_written_from_one_structure(tmp_path):
    item = audited_document("LayerKit: A Study", ["train.py", "model.safetensors"],
                            "Code at https://github.com/lab/paper")
    budget = resource_audit.Budget(verify=1)
    coverage = resource_audit.run_coverage([item], budget)
    written = resource_matrix.write(tmp_path / "matrix.json", [item], coverage,
                                    generated_at="2026-09-22T00:00:00")
    assert [path.suffix for path in written] == [".json", ".md", ".csv"]
    assert all(path.is_file() for path in written)
    assert not list(tmp_path.glob("*.tmp"))
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == resource_matrix.MATRIX_SCHEMA_VERSION
    assert payload["rows"][0]["coverage"]["checkpoint"] == "有候选"

    body = written[1].read_text(encoding="utf-8")
    assert "| 论文 | 资源 |" in body and "github.com/lab/paper" in body
    # A matrix that only says what it found would be read as a verdict, so it states its own reach.
    assert "这张表不能回答" in body and "复现" in body
    table = list(csv_module.reader(io.StringIO(written[2].read_text(encoding="utf-8"))))
    assert table[0][0] == "paper_title" and len(table) == 2
    assert len({len(line) for line in table}) == 1


def test_a_matrix_wider_than_a_comparison_set_says_so():
    documents = [audited_document(f"Paper{i}: T", ["train.py"]) for i in range(7)]
    payload = resource_matrix.matrix(documents, {}, generated_at="x")
    assert payload["comparable"] is False and payload["papers"] == 7
    assert "超出" in payload["note"]
    narrow = resource_matrix.matrix(documents[:2], {}, generated_at="x")
    assert narrow["comparable"] is True and "超出" not in narrow["note"]


def test_every_artifact_class_the_model_names_is_one_the_check_answers():
    """The mapping is the only thing standing between a class and silence.

    A component in the vocabulary with no indicator behind it would simply never appear in a row,
    and a class nobody reported is indistinguishable from a class nobody looked for. Failing here is
    louder than defaulting it to `unknown` at runtime.
    """
    from re0.models import AUDIT_COMPONENTS
    mapped = [component for _, component in resource_audit.COMPONENT_FROM_INDICATOR]
    assert set(mapped) == set(AUDIT_COMPONENTS)
    # No duplicates either: two indicators answering for one class would silently outrank each other.
    assert len(mapped) == len(set(mapped)) == len(AUDIT_COMPONENTS)
