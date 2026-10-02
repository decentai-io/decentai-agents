"""What the writer answers: a program, what it is for, and what it
needs.

The model is asked for a few labelled lines and then the program in a
fence, not for JSON: a program inside a JSON string has to be escaped
line by line, and a model that slips once has written neither.

    PURPOSE: Totals the orders by month and says which month was best.
    PACKAGES: pandas
    HOSTS: none
    CREDENTIALS: none
    ```python
    ...
    ```

or, when no program can do what was asked:

    CANNOT: why
"""

import re
from typing import List

HEADER_RE = re.compile(r"^\s*(PURPOSE|PACKAGES|HOSTS|CREDENTIALS|CANNOT)\s*:\s*(.*)$",
                       re.IGNORECASE)
FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)```", re.DOTALL)
#: What a credential is called inside the program: a name a shell
#: would take for an environment variable.
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
#: Names the program's own surroundings already use, and a credential
#: may not take over.
TAKEN = ("PATH", "HOME", "PYTHONPATH", "HTTP_PROXY", "HTTPS_PROXY", "TMPDIR",
         "TMP", "TEMP")
NOTHING = ("", "none", "nothing", "-", "n/a", "no")

PURPOSE_MAX = 500
CODE_MAX = 20_000
NEEDS_MAX = 20


class Credential:
    """A secret the program reads from its environment, and the host
    it is for."""

    def __init__(self, name: str, host: str):
        self.name = name
        self.host = host

    @property
    def label(self) -> str:
        return f"{self.name} for {self.host}"


class Plan:
    def __init__(self):
        self.purpose = ""
        self.code = ""
        self.packages: List[str] = []
        self.hosts: List[str] = []
        self.credentials: List[Credential] = []
        #: Why no program can do what was asked, when the writer says so.
        self.cannot = ""
        #: Why the reply is not a plan, when it is not.
        self.unreadable = ""

    @classmethod
    def read(cls, reply: str) -> "Plan":
        plan = cls()
        reply = str(reply or "")
        fence = FENCE_RE.search(reply)
        head = reply[: fence.start()] if fence else reply
        named = {}
        for line in head.splitlines():
            match = HEADER_RE.match(line)
            if match:
                named.setdefault(match.group(1).upper(), match.group(2).strip())
        if "CANNOT" in named and not fence:
            plan.cannot = named["CANNOT"][:PURPOSE_MAX] or "No reason was given."
            return plan
        if not fence:
            plan.unreadable = ("the program is missing, or its fence was "
                               "never closed" if "```" in reply else
                               "there is no program in a ``` fence")
            return plan
        plan.code = fence.group(1).strip("\r\n") + "\n"
        plan.purpose = " ".join(named.get("PURPOSE", "").split())[:PURPOSE_MAX]
        plan.packages = cls._listed(named.get("PACKAGES", ""))
        plan.hosts = [host.lower() for host in cls._listed(named.get("HOSTS", ""))]
        for written in cls._listed(named.get("CREDENTIALS", "")):
            name, _, host = written.partition("=")
            plan.credentials.append(Credential(name.strip(), host.strip().lower()))
        plan.unreadable = plan._problem()
        return plan

    @staticmethod
    def _listed(written: str) -> List[str]:
        if written.strip().lower() in NOTHING:
            return []
        found = []
        for item in written.split(","):
            item = item.strip().strip("`")
            if item and item.lower() not in NOTHING and item not in found:
                found.append(item)
        return found[:NEEDS_MAX]

    def _problem(self) -> str:
        if not self.code.strip():
            return "the program is empty"
        if len(self.code) > CODE_MAX:
            return f"the program is longer than {CODE_MAX} characters"
        if not self.purpose:
            return "PURPOSE is missing: one sentence a person can weigh"
        for credential in self.credentials:
            if not NAME_RE.match(credential.name) or credential.name in TAKEN:
                return (f"a credential is written NAME=host, the name in "
                        f"capitals: '{credential.name}' is not one")
            if credential.host not in self.hosts:
                return (f"the credential {credential.name} is for "
                        f"'{credential.host}', which HOSTS does not name")
        return ""
