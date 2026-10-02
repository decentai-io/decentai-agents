import os

from decentai_sdk.base import ToolBase

from .attempts import (
    NOT_TRIED, REACHED, REFUSED, Attempts, NotAnAddress, Outcome,
)

#: The one host this agent's manifest declares: what the check is
#: measured against. The tests hold the two to each other.
DECLARED = "example.com"
#: A host it never declared, and an address inside the network: the
#: one every cloud answers its own machines on.
UNDECLARED = "www.wikipedia.org"
INSIDE = "169.254.169.254"


class CheckTool(ToolBase):
    id = "check"

    async def reach(self, call):
        attempts = Attempts()
        try:
            host, port = attempts.where(call.inputs.get("address"))
        except NotAnAddress as exc:
            return {"error": str(exc), "kind": "invalid_address"}, "error"
        port = port or attempts.port
        found = attempts.reach(host, port)
        result = {
            "host": host, "port": port, "declared": host == DECLARED,
            "outcome": found.outcome,
            "through_the_proxy": attempts.through_the_proxy,
        }
        if found.reason:
            result["reason"] = found.reason
        return result, "success"

    async def report(self, call):
        attempts = Attempts()
        held = attempts.through_the_proxy
        direct_host, direct_port = attempts.direct

        rows = [
            self.row("The host this agent declared", DECLARED, REACHED,
                     attempts.reach(DECLARED, attempts.port)),
            self.row("A host it did not declare", UNDECLARED, REFUSED,
                     attempts.reach(UNDECLARED, attempts.port)),
            self.row("An address inside the network", INSIDE, REFUSED,
                     attempts.by_the_proxy(INSIDE, 80) if held else Outcome(
                         NOT_TRIED, "Nothing here would have refused it; this "
                                    "agent does not try one.")),
            self.row("Past the proxy: a connection made straight",
                     f"{direct_host}:{direct_port}", REFUSED,
                     attempts.straight(direct_host, direct_port)),
            self.row("Past the proxy: a name looked up", attempts.name, REFUSED,
                     attempts.looked_up(attempts.name)),
        ]
        as_expected = all(row["as_expected"] for row in rows)
        result = {
            "held": as_expected,
            "verdict": self.verdict(rows, held),
            "through_the_proxy": held,
            "rows": rows,
        }
        user = self.runs_as()
        if user:
            result["runs_as"] = user
        try:
            await call.show.table(
                rows, columns=["attempt", "target", "expected", "outcome", "reason"],
                title="What this agent could reach")
        except Exception:
            pass
        return result, "success"

    # ------------------------------------------------------------------
    @staticmethod
    def row(attempt, target, expected, found):
        row = {"attempt": attempt, "target": target, "expected": expected,
               "outcome": found.outcome,
               "as_expected": found.outcome == expected}
        if found.reason:
            row["reason"] = found.reason
        return row

    @staticmethod
    def verdict(rows, held):
        declared, undeclared, _inside, straight, looked_up = rows
        around = REACHED in (straight["outcome"], looked_up["outcome"])
        if not held:
            return ("Nothing holds this agent to the host it declared here: it was "
                    "pointed at no proxy, and connects as any program on this "
                    "machine does.")
        if undeclared["outcome"] == REACHED:
            return ("This agent is NOT held to the host it declared: it reached "
                    f"{undeclared['target']}, which it never declared.")
        if around:
            return ("Partly held. The platform's proxy refuses what this agent did "
                    "not declare, and a connection made past the proxy was not "
                    "stopped: nothing makes the proxy the only way out here.")
        if all(row["as_expected"] for row in rows):
            return ("Held. This agent reached the host it declared, and was "
                    "refused everything else it tried.")
        if declared["outcome"] != REACHED:
            return ("Held, and the host it declared did not answer: everything it "
                    f"did not declare was refused, and {declared['target']} was "
                    f"{declared['outcome']}.")
        return ("Held to the proxy, and one attempt did not go as expected: see "
                "the rows.")

    @staticmethod
    def runs_as():
        """The user this worker runs as, where the system has such a
        thing: where agents are confined, each has its own."""
        getuid = getattr(os, "getuid", None)
        return f"user {getuid()}" if getuid else ""
