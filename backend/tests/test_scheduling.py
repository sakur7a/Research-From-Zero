"""Request scheduling: budgets, provider deferrals, cache scope, pagination stops.

No network here. The point of these tests is that every way a paginated run can end has its own
observable state, because "we stopped asking" and "there was nothing there" must not collapse
into the same empty list.
"""
import pytest

from re0.literature import credential_scope
from re0.scheduling import (BACKOFF_CAP, MAX_ATTEMPTS, PAGINATED_SOURCES, RETRY_AFTER_CAP,
                            BudgetExhausted, Cancelled, Governor, ProviderFailure, RateLimited,
                            ResponseCache, paginate, retry_after_seconds)


class FakeClock:
    """Monotonic and wall clock plus a sleep that only moves time, so a test never waits."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def governor(**kwargs) -> tuple[Governor, FakeClock]:
    clock = FakeClock()
    return Governor(sleep=clock.sleep, clock=clock.monotonic, wall=clock.time, **kwargs), clock


def walker(pages, *, fail_at=None, rate_limit_at=None):
    """A fetch that yields `(items, next_cursor)` per page, optionally blowing up on one of them."""
    state = {"calls": 0}

    def fetch(cursor):
        index = state["calls"]
        state["calls"] += 1
        if fail_at == index:
            raise ProviderFailure("第 %d 页解析失败" % index)
        if rate_limit_at == index:
            raise RateLimited("提供商限流")
        items = pages[index] if index < len(pages) else []
        return items, f"cursor-{index + 1}" if index + 1 < len(pages) else ""

    return fetch, state


# --- pagination stops ---------------------------------------------------------


def test_a_source_that_runs_out_of_pages_reports_complete():
    fetch, state = walker([["a", "b"], ["c"]])
    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=5)
    assert outcome.stop_reason == "complete" and outcome.truncated is False
    assert outcome.items == ["a", "b", "c"] and outcome.pages_fetched == 2
    assert outcome.requests_used == 2 == state["calls"]


def test_the_page_ceiling_stops_first_and_says_the_rest_is_unread():
    fetch, state = walker([["a"], ["b"], ["c"], ["d"]])
    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=2)
    assert outcome.stop_reason == "page_budget" and outcome.truncated is True
    assert outcome.items == ["a", "b"] and outcome.next_cursor == "cursor-2"
    assert state["calls"] == 2, "no request may be made for a page the ceiling excludes"
    assert "还有后续页未读取" in outcome.detail


def test_max_pages_zero_makes_no_request_at_all():
    fetch, state = walker([["a"]])
    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=0)
    assert outcome.stop_reason == "page_budget" and state["calls"] == 0
    assert outcome.items == []


def test_an_empty_page_is_an_answer_not_a_failure():
    fetch, _ = walker([["a"], []])
    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=5)
    assert outcome.stop_reason == "empty_page" and outcome.truncated is False
    assert outcome.items == ["a"]
    assert "这不是失败" in outcome.detail


def test_a_source_that_stops_supplying_a_cursor_is_complete_not_truncated():
    def fetch(cursor):
        return ["a"], ""

    outcome = paginate(fetch, governor=None, provider="semanticscholar", max_pages=5)
    assert outcome.stop_reason == "complete" and outcome.pages_fetched == 1


def test_a_repeated_cursor_stops_the_walk_instead_of_looping():
    def fetch(cursor):
        return ["a"], "same-cursor"

    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=50)
    assert outcome.stop_reason == "cursor_repeated" and outcome.truncated is True
    assert outcome.pages_fetched == 2, "the second identical cursor is what proves the loop"


REJECTED_CURSORS = ["a b", "x" * 600, "https://evil.invalid/?c=1", "a" + chr(10) + "b", "a#frag"]


@pytest.mark.parametrize("cursor", REJECTED_CURSORS)
def test_a_cursor_outside_the_accepted_charset_is_refused_not_forwarded(cursor):
    calls = []

    def fetch(seen):
        calls.append(seen)
        return ["a"], cursor

    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=5)
    assert outcome.stop_reason == "cursor_rejected" and outcome.truncated is True
    assert outcome.next_cursor == "", "a refused cursor must not be handed back as resumable"
    assert calls == [""], "the rejected value is never sent to the provider"


def test_a_path_shaped_cursor_can_only_be_a_parameter_value_never_a_destination():
    """The charset cannot tell a base64 cursor from a traversal string, so the guarantee is
    structural: whatever comes back is handed to the connector as the cursor argument, and the
    connector is what builds the URL. A cursor therefore cannot reach the scheme, host or path."""
    seen = []

    def fetch(cursor):
        seen.append(cursor)
        return ["a"], "" if cursor else "../../etc/passwd"

    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=5)
    assert outcome.stop_reason == "complete"
    assert seen == ["", "../../etc/passwd"], "forwarded as a value, for the connector to place in a param"


def test_a_source_without_documented_paging_reads_one_page_and_says_so():
    fetch, state = walker([["a"], ["b"], ["c"]])
    outcome = paginate(fetch, governor=None, provider="openreview", max_pages=9)
    assert outcome.stop_reason == "not_supported" and outcome.truncated is True
    assert outcome.items == ["a"] and state["calls"] == 1
    assert "没有可公开核对的文档" in outcome.detail
    assert "openreview" not in PAGINATED_SOURCES


def test_a_page_that_fails_after_earlier_pages_keeps_them():
    fetch, _ = walker([["a", "b"], None, None], fail_at=1)
    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=5)
    assert outcome.stop_reason == "provider_failed"
    assert outcome.items == ["a", "b"], "what was read was established and must not be discarded"
    assert outcome.truncated is True


def test_a_first_page_that_fails_leaves_nothing_and_does_not_invent_a_result():
    fetch, _ = walker([None], fail_at=0)
    outcome = paginate(fetch, governor=None, provider="openalex", max_pages=5)
    assert outcome.stop_reason == "provider_failed" and outcome.items == []
    assert outcome.pages_fetched == 0


def test_a_rate_limit_is_reported_as_the_provider_deferring_not_as_no_results():
    fetch, _ = walker([["a"], None], rate_limit_at=1)
    outcome = paginate(fetch, governor=None, provider="semanticscholar", max_pages=5)
    assert outcome.stop_reason == "rate_limited" and outcome.truncated is True
    assert outcome.items == ["a"]


# --- shared budget ------------------------------------------------------------


def test_the_shared_request_ceiling_is_never_exceeded():
    gov, _ = governor(max_requests=3)
    for _ in range(3):
        gov.acquire("api.openalex.org")
    assert gov.requests_used == 3
    with pytest.raises(BudgetExhausted) as caught:
        gov.acquire("api.openalex.org")
    assert "不表示来源没有更多内容" in str(caught.value)


def test_a_per_provider_cap_leaves_the_rest_of_the_budget_usable():
    gov, _ = governor(max_requests=10, per_provider={"api.github.com": 1})
    gov.acquire("api.github.com")
    with pytest.raises(BudgetExhausted) as caught:
        gov.acquire("api.github.com")
    assert "单源请求预算" in str(caught.value)
    gov.acquire("api.openalex.org")  # another provider still has budget
    assert gov.requests_used == 2


def budgeted(gov, pages, host="api.openalex.org"):
    """A fetch that spends the budget the way the real client does: one reservation per request.

    The paginator only pre-checks, so a test whose fetch reserves nothing would never hit the
    ceiling — and would be testing a shape the production path does not have.
    """
    inner, state = walker(pages)

    def fetch(cursor):
        gov.acquire(host)
        return inner(cursor)

    return fetch, state


def test_a_pagination_walk_stops_at_the_shared_ceiling_and_keeps_its_cursor():
    gov, _ = governor(max_requests=2)
    fetch, state = budgeted(gov, [["a"], ["b"], ["c"], ["d"]])
    outcome = paginate(fetch, governor=gov, provider="openalex", max_pages=9)
    assert outcome.stop_reason == "request_budget" and outcome.truncated is True
    assert state["calls"] == 2 and outcome.next_cursor == "cursor-2"
    assert gov.requests_used == 2, "one request per page, never two"


def test_a_page_costs_exactly_one_request_not_one_per_layer():
    """Regression: the paginator and the client used to reserve from the same counter, so a
    single page spent two slots and a budget of one could not fetch anything at all."""
    gov, _ = governor(max_requests=1)
    fetch, state = budgeted(gov, [["a"], ["b"]])
    outcome = paginate(fetch, governor=gov, provider="openalex", max_pages=1)
    assert state["calls"] == 1 and gov.requests_used == 1
    assert outcome.items == ["a"] and outcome.stop_reason == "page_budget"


def test_a_per_provider_cap_stops_only_that_provider():
    gov, _ = governor(max_requests=10, per_provider={"api.openalex.org": 1})
    fetch, state = budgeted(gov, [["a"], ["b"], ["c"]])
    outcome = paginate(fetch, governor=gov, provider="openalex", max_pages=5)
    assert outcome.stop_reason == "request_budget" and state["calls"] == 1
    assert outcome.items == ["a"] and outcome.truncated is True


def test_cancellation_stops_scheduling_new_pages():
    gov, _ = governor(max_requests=50)
    fetch, state = budgeted(gov, [["a"], ["b"], ["c"]])

    def fetch_then_cancel(cursor):
        items, next_cursor = fetch(cursor)
        if state["calls"] >= 2:
            gov.cancel()
        return items, next_cursor

    outcome = paginate(fetch_then_cancel, governor=gov, provider="openalex", max_pages=9)
    assert outcome.stop_reason == "cancelled" and outcome.items == ["a", "b"]
    assert state["calls"] == 2, "a cancelled run schedules nothing further"
    with pytest.raises(Cancelled):
        gov.acquire("api.openalex.org")


def test_a_cancelled_run_reports_itself_in_the_summary():
    gov, _ = governor()
    gov.cancel()
    assert gov.cancelled is True and gov.summary()["cancelled"] is True


# --- provider deferrals -------------------------------------------------------


def test_retry_after_in_seconds_is_honoured_and_recorded():
    gov, clock = governor()
    deferred = gov.defer("api.openalex.org", {"Retry-After": "7"})
    assert deferred == 7.0
    assert clock.slept == [], "deferring only sets a window; the wait happens on the next acquire"
    gov.acquire("api.openalex.org")
    assert clock.slept == [7.0]
    assert gov.summary()["deferred_seconds"] == {"api.openalex.org": 7.0}


def test_retry_after_as_an_http_date_is_converted_to_a_wait():
    from email.utils import parsedate_to_datetime
    header = "Wed, 21 Oct 2026 07:28:00 GMT"
    moment = parsedate_to_datetime(header).timestamp()
    assert retry_after_seconds({"Retry-After": header}, wall=lambda: moment - 30) == 30.0
    # A date already in the past means no wait at all, never a negative one.
    assert retry_after_seconds({"Retry-After": header}, wall=lambda: moment + 30) == 0.0


def test_github_rate_limit_reset_becomes_a_wait():
    gov, clock = governor()
    deferred = gov.defer("api.github.com", {"x-ratelimit-reset": str(int(clock.now) + 11)})
    assert deferred == 11.0
    gov.acquire("api.github.com")
    assert clock.slept == [11.0]


def test_an_absurd_retry_after_is_clamped_rather_than_obeyed():
    assert retry_after_seconds({"Retry-After": "86400"}) == RETRY_AFTER_CAP


def test_an_unparseable_retry_after_falls_back_to_bounded_backoff_not_to_an_immediate_retry():
    assert retry_after_seconds({"Retry-After": "soon"}) is None
    assert retry_after_seconds({}) is None
    gov, clock = governor()
    waits = []
    for _ in range(5):
        gov.note_retry("api.openalex.org")   # the order the client uses: count, then defer
        waits.append(gov.defer("api.openalex.org", {"Retry-After": "nonsense"}))
    assert waits[0] < waits[1] < waits[2], "backoff grows"
    assert max(waits) == BACKOFF_CAP, "and stops growing at the cap"
    assert 0 < min(waits), "a deferral of zero would be an immediate retry"
    assert clock.slept == [], "deferring only sets a window; acquire is what waits"


def test_retries_are_bounded_so_they_cannot_nest_or_loop():
    gov, _ = governor()
    allowed = [gov.note_retry("api.openalex.org") for _ in range(MAX_ATTEMPTS + 2)]
    assert allowed[:MAX_ATTEMPTS - 1] == [True] * (MAX_ATTEMPTS - 1)
    assert allowed[MAX_ATTEMPTS - 1:] == [False] * 3
    gov.note_success("api.openalex.org")
    assert gov.note_retry("api.openalex.org") is True, "a success clears the count"


def test_a_deferral_longer_than_the_time_budget_stops_instead_of_ignoring_the_provider():
    gov, clock = governor(max_requests=10, seconds=5)
    gov.defer("api.openalex.org", {"Retry-After": "60"})
    with pytest.raises(BudgetExhausted) as caught:
        gov.acquire("api.openalex.org")
    assert "超出本次调用的时间预算" in str(caught.value)
    assert clock.slept == [], "it refuses rather than sleeping through the deadline"


def test_a_spent_time_budget_is_reported_as_the_runs_own_limit():
    gov, clock = governor(max_requests=10, seconds=5)
    clock.now += 10
    with pytest.raises(BudgetExhausted):
        gov.acquire("api.openalex.org")


# --- cache --------------------------------------------------------------------


def test_a_cached_response_is_reused_inside_its_ttl_and_dropped_after_it():
    clock = FakeClock()
    cache = ResponseCache(ttl=60, clock=clock.monotonic, wall=clock.time)
    cache.put("https://api.openalex.org/works", {"search": "x"}, "anonymous", b"{}", "application/json")
    hit = cache.get("https://api.openalex.org/works", {"search": "x"}, "anonymous")
    assert hit is not None and hit.raw == b"{}" and hit.age_seconds == 0.0
    assert hit.stored_at.endswith("+00:00"), "the age of an answer is part of the answer"
    clock.now += 61
    assert cache.get("https://api.openalex.org/works", {"search": "x"}, "anonymous") is None
    assert cache.summary()["expired"] == 1


def test_a_forced_refresh_bypasses_and_replaces_the_entry():
    clock = FakeClock()
    cache = ResponseCache(ttl=600, clock=clock.monotonic, wall=clock.time)
    cache.put("u", {}, "anonymous", b"first")
    assert cache.get("u", {}, "anonymous", refresh=True) is None
    cache.put("u", {}, "anonymous", b"second")
    assert cache.get("u", {}, "anonymous").raw == b"second"


def test_a_credentialed_response_is_never_served_to_an_uncredentialed_call():
    clock = FakeClock()
    cache = ResponseCache(ttl=600, clock=clock.monotonic, wall=clock.time)
    cache.put("u", {"q": "1"}, "openalex+semanticscholar", b"gated-records")
    assert cache.get("u", {"q": "1"}, "anonymous") is None, "wider entitlement must not leak"
    cache.put("u", {"q": "1"}, "anonymous", b"public-records")
    assert cache.get("u", {"q": "1"}, "openalex+semanticscholar").raw == b"gated-records"
    assert cache.get("u", {"q": "1"}, "anonymous").raw == b"public-records"


def test_a_zero_ttl_disables_the_cache_entirely():
    cache = ResponseCache(ttl=0)
    cache.put("u", {}, "anonymous", b"x")
    assert cache.get("u", {}, "anonymous") is None and cache.summary()["entries"] == 0


def test_the_cache_scope_names_the_entitlement_without_naming_a_secret(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("SEMANTICSCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("OPENREVIEW_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert credential_scope() == "anonymous"
    monkeypatch.setenv("SEMANTICSCHOLAR_API_KEY", "s2-secret-value")
    monkeypatch.setenv("OPENALEX_MAILTO", "someone@example.invalid")
    scope = credential_scope()
    assert scope == "semanticscholar", "a mailto is not a credential and does not widen the scope"
    assert "s2-secret-value" not in scope
    monkeypatch.setenv("OPENALEX_API_KEY", "openalex-secret-value")
    assert credential_scope() == "openalex+semanticscholar"
