import base64
import json

import httpx
import pytest

from re0.models import normalize_arxiv, normalize_doi
from re0.providers import ProviderClient, ProviderError, check_resource, classify_files, repository_identity, resolve_metadata, MAX_BYTES

SHA = "a" * 40


def gh_transport(*, truncated=False, readme=True):
    def handler(request):
        path = request.url.path
        if path == "/repos/lab/paper":
            return httpx.Response(200, json={"default_branch": "main", "license": {"spdx_id": "Apache-2.0"}})
        if path == "/repos/lab/paper/commits/main":
            return httpx.Response(200, json={"sha": SHA})
        if path == f"/repos/lab/paper/git/trees/{SHA}":
            assert request.url.params["recursive"] == "1"
            return httpx.Response(200, json={"truncated": truncated, "tree": [{"path": p, "type": "blob"} for p in ["train.py", "inference.py", "eval.py", "requirements.txt", "docs/training.md"]]})
        if path == "/repos/lab/paper/releases":
            return httpx.Response(200, json=[{"assets": [{"name": "model.safetensors", "size": 4096}]}])
        if path == "/repos/lab/paper/readme":
            if not readme:
                return httpx.Response(404)
            text = "# Resources\nModel: https://huggingface.co/lab/model\nData: https://huggingface.co/datasets/lab/data\nUntrusted: https://github.com/lab/../../admin\nPrivate: http://127.0.0.1/admin\n"
            return httpx.Response(200, json={"encoding": "base64", "path": "README.md", "content": base64.b64encode(text.encode()).decode()})
        raise AssertionError(f"Unexpected request: {request.url}")
    return httpx.MockTransport(handler)


def test_github_check_is_pinned_and_does_not_overclaim():
    result = check_resource("https://github.com/lab/paper", gh_transport())
    assert result.status == "metadata_accessible"
    assert result.depth == "file_listing"
    assert result.revision == SHA
    assert result.indicators["training"] == ["train.py"]
    assert result.indicators["inference"] == ["inference.py"]
    assert len(result.discovered) == 2
    assert result.discovered[0]["source_url"].endswith("#L2")
    assert result.license_id == "Apache-2.0"
    assert len(result.content_sha256) == 64
    assert "未运行代码" in " ".join(result.limitations)
    assert all(SHA in e.source_url for e in result.evidence if e.category == "file_name_candidate")


def test_truncated_tree_preserves_uncertainty():
    result = check_resource("https://github.com/lab/paper", gh_transport(truncated=True))
    assert result.status == "metadata_accessible"
    assert any("truncated=true" in text for text in result.limitations)


def test_missing_readme_does_not_discard_tree_evidence():
    result = check_resource("https://github.com/lab/paper", gh_transport(readme=False))
    assert result.status == "metadata_accessible"
    assert result.indicators["training"]
    assert any("README 检查未完成" in x for x in result.limitations)


@pytest.mark.parametrize("code,headers,status", [(404, {}, "indeterminate"), (403, {}, "access_failed"), (401, {}, "access_failed"), (429, {}, "indeterminate"), (403, {"x-ratelimit-remaining": "0"}, "indeterminate"), (302, {"location": "http://127.0.0.1/admin"}, "indeterminate"), (500, {}, "indeterminate")])
def test_network_errors_are_not_no_code_verdicts(code, headers, status):
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(code, headers=headers)
    result = check_resource("https://github.com/lab/paper", httpx.MockTransport(handler))
    assert result.status == status
    assert result.depth == "not_verified"
    assert len(seen) == 1
    assert result.evidence


def test_timeout_is_graceful():
    def handler(request):
        raise httpx.ReadTimeout("Private details should not leak")
    result = check_resource("https://github.com/lab/paper", httpx.MockTransport(handler))
    assert result.status == "indeterminate"
    assert "Private" not in result.summary


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "https://github.com.evil.com/lab/paper", "https://github.com@evil.com/lab/paper", "https://github.com/lab/../admin", "https://github.com/lab/%2e%2e", "https://github.com/lab/repo?token=x", "https://github.com/lab/repo/tree/main", "https://huggingface.co/spaces/lab/demo", "https://github.com:8443/lab/paper", "file:///etc/passwd"])
def test_non_root_or_untrusted_resource_urls_never_trigger_network(url):
    def handler(request):
        raise AssertionError("Should not make requests")
    result = check_resource(url, httpx.MockTransport(handler))
    assert result.status in {"unsupported", "indeterminate"}


def test_filename_matches_are_scoped_and_report_only_candidates():
    found = classify_files(["docs/training.md", "train.py", "models/weights.safetensors", "data/split.csv", "evaluate.py", "pretrain.py", "environment.yml", "inference.sh"])
    assert found["training"] == ["train.py"]
    assert found["weights"] == ["models/weights.safetensors"]
    assert found["evaluation"] == ["evaluate.py"]
    assert "pretrain.py" not in found["training"]  # Bounded heuristic; not complete semantic parsing.


@pytest.mark.parametrize("gated,expected", [(False, "metadata_accessible"), ("manual", "gated"), ("auto", "gated"), (True, "gated")])
def test_hub_access_gates_are_independent_of_file_listing(gated, expected):
    def handler(request):
        assert request.url.path == "/api/models/lab/model"
        return httpx.Response(200, json={"sha": SHA, "gated": gated, "siblings": [{"rfilename": "model.safetensors"}], "cardData": {"license": "apache-2.0"}})
    result = check_resource("https://huggingface.co/lab/model", httpx.MockTransport(handler))
    assert result.status == expected
    assert result.indicators["weights"] == ["model.safetensors"]
    assert result.depth == "file_listing"


def test_hub_dataset_endpoint_and_missing_revision():
    def handler(request):
        assert request.url.path == "/api/datasets/lab/data"
        return httpx.Response(200, json={"gated": False, "siblings": [{"rfilename": "data/train.parquet"}]})
    result = check_resource("https://huggingface.co/datasets/lab/data", httpx.MockTransport(handler))
    assert result.status == "metadata_accessible"
    assert result.indicators["data"] == ["data/train.parquet"]
    assert any("未返回提交版本" in x for x in result.limitations)


def test_oversized_response_cannot_be_used_as_positive_evidence():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x"*(MAX_BYTES+1)))
    result = check_resource("https://github.com/lab/paper", transport)
    assert result.status == "indeterminate"
    assert "预算" in result.summary


def test_github_token_is_never_sent_to_hub(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-do-not-leak")
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})
    client = ProviderClient(httpx.MockTransport(handler))
    client.json("https://api.github.com/repos/lab/paper")
    client.json("https://huggingface.co/api/models/lab/model")
    client.close()
    assert seen[0].headers["authorization"] == "Bearer test-do-not-leak"
    assert "authorization" not in seen[1].headers


def test_client_rejects_unknown_hosts_and_redirect_options():
    client = ProviderClient(httpx.MockTransport(lambda r: pytest.fail("No network expected")))
    for url in ["http://api.github.com/repos/lab/paper", "https://127.0.0.1", "https://api.github.com:9000/repos/lab/paper"]:
        with pytest.raises(ProviderError):
            client.read(url)
    client.close()


def test_crossref_resolver_checks_id_and_maps_authors():
    def handler(request):
        assert request.url.host == "api.crossref.org"
        return httpx.Response(200, json={"message": {"DOI": "10.1234/test", "title": ["An article"], "author": [{"given": "A", "family": "B"}], "published": {"date-parts": [[2024]]}, "container-title": ["A journal"]}})
    result = resolve_metadata("https://doi.org/10.1234/TEST", httpx.MockTransport(handler))
    assert result.doi == "10.1234/test"
    assert result.authors == ["A B"]
    assert result.year == 2024


def test_crossref_wrong_identifier_is_not_silently_imported():
    response = {"message": {"DOI": "10.1234/other", "title": ["Wrong"]}}
    with pytest.raises(ProviderError):
        resolve_metadata("10.1234/test", httpx.MockTransport(lambda r: httpx.Response(200, json=response)))


def test_arxiv_resolver_preserves_version(monkeypatch):
    import re0.providers as providers
    monkeypatch.setattr(providers, "_arxiv_last", 0)
    xml = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Attention Is All You Need</title><published>2017-06-12T00:00:00Z</published><summary>A summary.</summary><author><name>A Author</name></author></entry></feed>'''
    result = resolve_metadata("1706.03762", httpx.MockTransport(lambda r: httpx.Response(200, content=xml)))
    assert result.arxiv_id == "1706.03762v7"
    assert result.version_label == "v7"
    assert result.year == 2017


def test_xml_entities_rejected(monkeypatch):
    import re0.providers as providers
    monkeypatch.setattr(providers, "_arxiv_last", 0)
    with pytest.raises(ProviderError):
        resolve_metadata("1706.03762", httpx.MockTransport(lambda r: httpx.Response(200, content='<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]><feed/>')))


def test_identifier_normalization():
    assert normalize_doi("https://doi.org/10.1234/ABC") == "10.1234/abc"
    assert normalize_arxiv("https://arxiv.org/pdf/1706.03762v2.pdf") == "1706.03762v2"
    assert normalize_arxiv("hep-th/9901001") == "hep-th/9901001"
    with pytest.raises(ValueError):
        normalize_arxiv("https://evil.example/1706.03762")
    assert repository_identity("https://github.com/lab/paper.git") == ("github", "repos", "lab/paper")


def test_gated_metadata_without_listing_preserves_gate_not_file_claim():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"gated": "manual"}))
    result = check_resource("https://huggingface.co/lab/model", transport)
    assert result.status == "gated"
    assert result.depth == "metadata_only"
    assert not result.indicators["weights"]
    assert any("未返回文件清单" in item for item in result.limitations)


def test_arxiv_explicit_version_is_not_silently_replaced(monkeypatch):
    import re0.providers as providers
    monkeypatch.setattr(providers, "_arxiv_last", 0)
    xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Title</title></entry></feed>'
    with pytest.raises(ProviderError, match="版本"):
        resolve_metadata("1706.03762v1", httpx.MockTransport(lambda r: httpx.Response(200, content=xml)))
