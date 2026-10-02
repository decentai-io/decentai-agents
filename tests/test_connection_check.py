"""The Connection Check agent in a real worker.

The hosts it tries stand on this machine: a loopback web server is
every host that answers, and the platform's own proxy, started in this
process, is what refuses. A worker here is not confined, so a
connection made past the proxy arrives, and the check says so; the
verdict of an agent that is held all the way is proved where workers
are confined, in the runtime's image.
"""

import asyncio
import json
import socket

import pytest
import yaml

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.conftest import ROOT
from tests.web_stub import web  # noqa: F401

DECLARED = "example.com"


def run(awaitable):
    return asyncio.run(awaitable)


def invoke(agents, name, inputs=None):
    executor = FunctionExecutor(provider=InMemoryResourceProvider())
    return run(executor.invoke(
        agents["connection_check"], f"connection_check.check.{name}",
        inputs or {}, chat_level=0))


def port_of(stub) -> int:
    return int(stub.url.rsplit(":", 1)[1])


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def rows_of(result):
    return {row["attempt"]: row for row in result["rows"]}


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["connection_check"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_it_declares_one_host_and_the_check_is_measured_against_it(self, agents):
        manifest = yaml.safe_load(
            (ROOT / "connection_check" / "manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["network"] == {"hosts": [DECLARED]}
        source = (ROOT / "connection_check" / "tools" / "check_tool.py").read_text(
            encoding="utf-8")
        assert f'DECLARED = "{DECLARED}"' in source
        assert manifest["implementation"]["dependencies"] == []

    def test_both_functions_are_reads(self, agents):
        manifest = agents["connection_check"].manifest
        assert {name: manifest.function(f"connection_check.check.{name}")[1]["permission_level"]
                for name in ("report", "reach")} == {"report": 0, "reach": 0}


class TestWhereNothingHoldsIt:
    """No proxy: a worker connects as any program on the machine does."""

    @pytest.fixture
    def machine(self, web, monkeypatch):
        monkeypatch.delenv("DECENTAI_PROXY", raising=False)
        port = port_of(web)
        monkeypatch.setenv("DECENTAI_AGENT_CONNECTION_CHECK_TEST", json.dumps({
            "port": port, "direct": f"127.0.0.1:{port}", "name": "localhost",
            "resolve": {DECLARED: "127.0.0.1", "www.wikipedia.org": "127.0.0.1"}}))
        return port

    def test_the_check_says_nothing_holds_it(self, agents, machine):
        result, status = invoke(agents, "report")
        assert status == "success", result
        assert result["through_the_proxy"] is False and result["held"] is False
        assert "Nothing holds this agent" in result["verdict"]
        rows = rows_of(result)
        assert rows["The host this agent declared"]["outcome"] == "reached"
        # What an agent that is held is refused, this one reached.
        undeclared = rows["A host it did not declare"]
        assert undeclared["outcome"] == "reached" and undeclared["as_expected"] is False
        assert rows["Past the proxy: a connection made straight"]["outcome"] == "reached"

    def test_an_address_inside_is_not_knocked_on(self, agents, machine):
        inside = rows_of(invoke(agents, "report")[0])["An address inside the network"]
        assert inside["outcome"] == "not_tried"
        assert "does not try one" in inside["reason"]

    def test_one_address_is_tried_straight(self, agents, machine):
        result, status = invoke(agents, "reach", {"address": f"127.0.0.1:{machine}"})
        assert status == "success", result
        assert result == {"host": "127.0.0.1", "port": machine, "declared": False,
                          "outcome": "reached", "through_the_proxy": False}

    def test_a_private_address_is_not_tried(self, agents, machine):
        result, status = invoke(agents, "reach", {"address": "http://10.0.0.5/admin"})
        assert status == "success", result
        assert result["outcome"] == "not_tried" and result["port"] == 80
        assert "inside the network" in result["reason"]


class TestBehindThePlatformsProxy:
    """The proxy is the platform's own, and this agent's pass opens the
    one host its manifest declares."""

    @pytest.fixture
    def proxy(self, web, monkeypatch):
        from ai_runtime.agents.egress import EgressProxy

        port, closed = port_of(web), closed_port()
        monkeypatch.setattr(
            EgressProxy, "resolver",
            staticmethod(lambda host, _port: ["127.0.0.1"]))
        monkeypatch.setattr(
            EgressProxy, "PORTS_OF_A_NAMED_HOST",
            (*EgressProxy.PORTS_OF_A_NAMED_HOST, port, closed))
        found = EgressProxy(0, allow_loopback=True)
        assert found.start() == []
        manifest = yaml.safe_load(
            (ROOT / "connection_check" / "manifest.yaml").read_text(encoding="utf-8"))
        monkeypatch.setenv("DECENTAI_PROXY", found.address(found.admit(
            "Connection Check", {"declared": True, "any": False,
                                 "hosts": manifest["network"]["hosts"],
                                 "from_secrets": []})))
        monkeypatch.setenv("DECENTAI_AGENT_CONNECTION_CHECK_TEST", json.dumps({
            "port": port, "direct": f"127.0.0.1:{port}", "name": "localhost"}))
        found.port_of_the_site, found.closed_port = port, closed
        yield found
        found.stop()

    def test_the_host_it_declared_is_reached(self, agents, proxy):
        result, status = invoke(agents, "reach", {"address": DECLARED})
        assert status == "success", result
        assert result == {"host": DECLARED, "port": proxy.port_of_the_site,
                          "declared": True, "outcome": "reached",
                          "through_the_proxy": True}
        assert proxy.refusals == 0

    def test_a_host_it_did_not_declare_is_refused_and_told_why(self, agents, proxy):
        result, status = invoke(agents, "reach", {"address": "https://api.github.com/user"})
        assert status == "success", result
        assert (result["host"], result["declared"]) == ("api.github.com", False)
        assert result["outcome"] == "refused"
        assert "did not declare api.github.com" in result["reason"]
        assert proxy.refusals == 1

    def test_an_address_is_refused_as_one(self, agents, proxy):
        result, _ = invoke(agents, "reach", {"address": "10.0.0.5"})
        assert result["outcome"] == "refused"
        assert "is an address, not a name" in result["reason"]

    def test_a_declared_host_that_does_not_answer_is_not_called_refused(
            self, agents, proxy):
        result, _ = invoke(agents, "reach",
                           {"address": f"{DECLARED}:{proxy.closed_port}"})
        assert result["outcome"] == "unreachable"
        assert "could not be reached" in result["reason"]
        assert proxy.refusals == 0

    def test_the_check_tells_held_by_the_proxy_from_held_all_the_way(
            self, agents, proxy):
        """The proxy refuses what was not declared. Nothing on this
        machine makes it the only way out, and the check says that
        too."""
        result, status = invoke(agents, "report")
        assert status == "success", result
        rows = rows_of(result)
        assert [(rows[name]["outcome"], rows[name]["as_expected"]) for name in (
            "The host this agent declared", "A host it did not declare",
            "An address inside the network")] == [
            ("reached", True), ("refused", True), ("refused", True)]
        assert "did not declare www.wikipedia.org" in rows[
            "A host it did not declare"]["reason"]
        straight = rows["Past the proxy: a connection made straight"]
        assert (straight["outcome"], straight["as_expected"]) == ("reached", False)
        assert result["through_the_proxy"] is True and result["held"] is False
        assert result["verdict"].startswith("Partly held.")

    def test_a_pass_nobody_gave_opens_nothing(self, agents, proxy, monkeypatch):
        monkeypatch.setenv(
            "DECENTAI_PROXY", f"http://worker:not-a-pass@127.0.0.1:{proxy.port}")
        result, _ = invoke(agents, "reach", {"address": DECLARED})
        assert result["outcome"] == "refused"
        assert "does not know this worker" in result["reason"]


class TestWhatIsNotAnAddress:
    @pytest.mark.parametrize("address, says", [
        ("ftp://example.com/file", "is not tried"),
        ("https://sara:secret@example.com/", "a name and a password"),
        ("not a host", "names no host"),
        ("https://", "names no host"),
    ])
    def test_it_is_refused_before_anything_is_tried(self, agents, address, says):
        result, status = invoke(agents, "reach", {"address": address})
        assert status == "error", result
        assert result["kind"] == "invalid_address" and says in result["error"]
