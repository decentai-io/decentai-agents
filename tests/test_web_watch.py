"""The Web Watch agent in a real worker, watching fictional pages on a
loopback web server that the tests change between checks. Each kind of
watch must report a change exactly once, and a page that cannot be
fetched must never wake anyone.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.web_stub import LOOPBACK_VARIABLE, WebStub, web  # noqa: F401


def product(price: str, stock: str = "In stock") -> str:
    return f"""<html><head><title>Northlight Desk Lamp</title></head><body>
    <nav><a href="/">Shop</a> <span>{price} basket</span></nav>
    <h1>Northlight Desk Lamp</h1>
    <p class="price">Price: USD {price}</p>
    <p>{stock}. Ships in 2 working days.</p>
    </body></html>"""


def news(*items: str) -> str:
    rows = "".join(f"<li>{item}</li>" for item in items)
    return f"<html><body><h1>Riverside Book Fair news</h1><ul>{rows}</ul></body></html>"


def run(awaitable):
    return asyncio.run(awaitable)


def make():
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["web_watch"], name, inputs, chat_level=chat_level))


def records(provider):
    return provider.data.get("web_watch__watch", {})


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["web_watch"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_changed_is_declared_schedulable_at_level_one(self, agents):
        _, function = agents["web_watch"].manifest.function("web_watch.watches.changed")
        assert function["schedulable"] is True
        assert function["permission_level"] == 1
        assert not function.get("llm")
        levels = {name: agents["web_watch"].manifest.function(f"web_watch.watches.{name}")[1]["permission_level"]
                  for name in ("add", "list", "remove", "check_now")}
        assert levels == {"add": 1, "list": 0, "remove": 1, "check_now": 0}


class TestPatternWatch:
    def test_a_price_change_is_reported_once_with_before_and_after(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        added, status = invoke(agents, ex, "web_watch.watches.add", {
            "url": url, "kind": "pattern", "pattern": r"Price: USD\s+([\d.,]+)",
            "label": "Desk lamp price"})
        assert status == "success", added
        assert (added["found"], added["value"]) == (True, "129.00")
        assert "Price: USD 129.00" in added["excerpt"]

        quiet, status = invoke(agents, ex, "web_watch.watches.changed", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "changed": [], "failed": [], "more": False}

        web.page("/lamp", product("119.00"))
        news_, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        (change,) = news_["changed"]
        assert (change["before"], change["after"]) == ("129.00", "119.00")
        assert change["watch_ref"] == added["watch_ref"]
        assert change["label"] == "Desk lamp price"

        again, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert again["changed"] == []

    def test_other_text_changing_is_not_a_pattern_change(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        invoke(agents, ex, "web_watch.watches.add", {
            "url": url, "kind": "pattern", "pattern": r"USD\s+([\d.]+)"})
        web.page("/lamp", product("129.00", stock="Only 2 left"))
        result, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert result["changed"] == []

    def test_an_invalid_regular_expression_is_refused_with_why(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        result, status = invoke(agents, ex, "web_watch.watches.add", {
            "url": url, "kind": "pattern", "pattern": r"Price: (USD"})
        assert status == "error" and result["kind"] == "invalid"
        assert "missing )" in result["error"]
        assert records(provider) == {}
        assert web.hits == {}


class TestContainsWatch:
    def test_a_phrase_appearing_and_disappearing_is_reported_each_time_once(self, agents, web):
        url = web.page("/news", news("Author talks announced"))
        ex, provider = make()
        added, status = invoke(agents, ex, "web_watch.watches.add", {
            "url": url, "kind": "contains", "phrase": "registration   open"})
        assert status == "success", added
        assert added["found"] is False and "note" in added
        assert added["target"] == "registration open"

        web.page("/news", news("Author talks announced", "Registration\nopen for stall holders"))
        appeared, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        (change,) = appeared["changed"]
        assert (change["before"], change["after"]) == ("absent", "present")
        assert "Registration open for stall holders" in change["excerpt_after"]
        again, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert again["changed"] == []

        web.page("/news", news("Registration closed"))
        gone, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        (change,) = gone["changed"]
        assert (change["before"], change["after"]) == ("present", "absent")

    def test_a_contains_watch_needs_its_phrase(self, agents, web):
        ex, provider = make()
        result, status = invoke(agents, ex, "web_watch.watches.add",
                                {"url": web.url + "/news", "kind": "contains"})
        assert status == "error" and result["kind"] == "invalid"
        assert records(provider) == {}


class TestPageWatch:
    def test_a_text_change_is_reported_once_with_a_line_diff(self, agents, web):
        url = web.page("/news", news("Author talks announced", "Parking opens at 8"))
        ex, provider = make()
        added, status = invoke(agents, ex, "web_watch.watches.add", {"url": url, "kind": "page"})
        assert status == "success", added

        # Whitespace alone is not a change.
        web.page("/news", news("Author   talks\n announced", "Parking opens at 8"))
        quiet, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert quiet["changed"] == []

        web.page("/news", news("Author talks announced", "Parking opens at 9"))
        result, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        (change,) = result["changed"]
        assert change["kind"] == "page"
        assert change["diff"] == ["- - Parking opens at 8", "+ - Parking opens at 9"]
        assert change["diff_truncated"] is False
        again, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert again["changed"] == []

    def test_check_now_shows_the_change_without_moving_the_reading(self, agents, web):
        url = web.page("/news", news("Author talks announced"))
        ex, provider = make()
        added, _ = invoke(agents, ex, "web_watch.watches.add", {"url": url, "kind": "page"})
        web.page("/news", news("Author talks postponed"))
        shown, status = invoke(agents, ex, "web_watch.watches.check_now",
                               {"watch_ref": added["watch_ref"]})
        assert status == "success", shown
        assert shown["changed"] is True
        assert "- - Author talks announced" in shown["diff"]
        shown_again, _ = invoke(agents, ex, "web_watch.watches.check_now",
                                {"watch_ref": added["watch_ref"]})
        assert shown_again["changed"] is True
        # The scheduled check still hears about it, once.
        result, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert len(result["changed"]) == 1


class TestFailures:
    def test_a_failed_fetch_is_counted_and_never_wakes(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        added, _ = invoke(agents, ex, "web_watch.watches.add", {
            "url": url, "kind": "pattern", "pattern": r"USD\s+([\d.]+)"})
        ref = added["watch_ref"]

        web.fail("/lamp", 503)
        first, status = invoke(agents, ex, "web_watch.watches.changed", {})
        assert status == "success", first
        assert first["changed"] == []
        assert [(f["watch_ref"], f["failures"]) for f in first["failed"]] == [(ref, 1)]
        assert "503" in first["failed"][0]["error"]
        second, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert second["changed"] == [] and second["failed"][0]["failures"] == 2
        # The stored reading is untouched by a failure.
        assert records(provider)[ref]["keys"]["value"] == "129.00"

        # Back, unchanged: not a change, and the count starts again.
        web.page("/lamp", product("129.00"))
        back, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert back == {"checked": 1, "changed": [], "failed": [], "more": False}
        assert records(provider)[ref]["keys"]["failures"] == 0

        listed, _ = invoke(agents, ex, "web_watch.watches.list", {})
        assert listed["total"] == 1 and listed["watches"][0]["failures"] == 0

    def test_a_watch_is_listed_and_removed(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        added, _ = invoke(agents, ex, "web_watch.watches.add", {"url": url, "kind": "page"})
        listed, _ = invoke(agents, ex, "web_watch.watches.list", {})
        assert [w["watch_ref"] for w in listed["watches"]] == [added["watch_ref"]]
        removed, status = invoke(agents, ex, "web_watch.watches.remove",
                                 {"watch_ref": added["watch_ref"]})
        assert status == "success" and removed["removed"] is True
        assert records(provider) == {}
        quiet, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert quiet["checked"] == 0


class TestAddressSafety:
    def test_a_private_address_is_refused_even_with_the_test_variable(self, agents, web):
        ex, provider = make()
        result, status = invoke(agents, ex, "web_watch.watches.add",
                                {"url": "http://169.254.169.254/latest/meta-data/", "kind": "page"})
        assert status == "error" and result["kind"] == "forbidden"
        assert "link-local" in result["error"]
        assert records(provider) == {}

    def test_loopback_is_refused_without_the_test_variable(self, agents, monkeypatch):
        monkeypatch.delenv(LOOPBACK_VARIABLE, raising=False)
        stub = WebStub().start()
        try:
            url = stub.page("/lamp", product("129.00"))
            ex, provider = make()
            result, status = invoke(agents, ex, "web_watch.watches.add",
                                    {"url": url, "kind": "page"})
            assert status == "error" and result["kind"] == "forbidden"
            assert "loopback" in result["error"] and stub.hits == {}
        finally:
            stub.stop()

    def test_a_redirect_to_a_private_address_is_a_failure_not_a_change(self, agents, web):
        url = web.page("/lamp", product("129.00"))
        ex, provider = make()
        added, _ = invoke(agents, ex, "web_watch.watches.add", {"url": url, "kind": "page"})
        web.redirect("/lamp", "http://10.0.0.1/lamp")
        result, _ = invoke(agents, ex, "web_watch.watches.changed", {})
        assert result["changed"] == []
        (failure,) = result["failed"]
        assert failure["watch_ref"] == added["watch_ref"] and "10.0.0.1" in failure["error"]
