"""The Code agent in a real worker: programs really written to disk and
really run, the chat's model scripted, the person a scripted answer to
the code card and to a credential.
"""

import asyncio
import shutil
from pathlib import Path

import pytest
import yaml

from ai_runtime.agents.environments import AgentEnvironment
from ai_runtime.execution.executor import FunctionExecutor
from ai_runtime.sinks import ChatSinks
from sim.resources import InMemoryResourceProvider

ROOT = Path(__file__).resolve().parents[1]
ORDERS = b"month,total\nJanuary,1200.50\nFebruary,980.00\nMarch,1430.25\n"

TOTALS = '''import csv

with open("orders.csv", newline="") as handle:
    rows = list(csv.DictReader(handle))
total = sum(float(row["total"]) for row in rows)
best = max(rows, key=lambda row: float(row["total"]))
with open("totals.txt", "w") as out:
    out.write(f"{total:.2f}")
print(f"Total {total:.2f}; best month {best['month']}")
'''


def answer(code, purpose="Totals the orders and says which month was best.",
           packages="none", hosts="none", credentials="none"):
    return (f"PURPOSE: {purpose}\nPACKAGES: {packages}\nHOSTS: {hosts}\n"
            f"CREDENTIALS: {credentials}\n```python\n{code}```")


class Model:
    """The chat's model, scripted: one answer a request, in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    async def __call__(self, messages, max_tokens=None, images=None):
        self.asked.append(messages[-1]["content"])
        assert self.answers, "the model was asked more than the script holds"
        return self.answers.pop(0)


class Person:
    """The scripted person: answers each card as the list says, and
    gives a credential when asked."""

    def __init__(self, answers=(True,), token="s3cret-token"):
        self.answers = list(answers)
        self.cards = []
        self.token = token
        self.logins = []

    async def propose(self, code, source):
        self.cards.append(code)
        return self.answers.pop(0) if self.answers else True

    async def credential(self, host, fields, account, site, refresh, source):
        self.logins.append((host, [f["name"] for f in fields]))
        return {"token": self.token, "host": host} if self.token else None


def run(awaitable):
    return asyncio.run(awaitable)


def made(model, person=None, provider=None):
    provider = provider or InMemoryResourceProvider()
    person = person or Person()
    executor = FunctionExecutor(provider=provider,
                                sinks=ChatSinks(llm=model, propose=person.propose,
                                                credential=person.credential))
    return executor, provider, person


def upload(provider, filename, raw) -> str:
    return run(provider.create_file("chat_attachment", filename, raw))["resource_ref"]


def program(agents, executor, inputs):
    return run(executor.invoke(agents["code_runner"], "code_runner.program.run",
                               inputs, chat_level=2))


class TestTheManifest:
    def test_it_declares_nothing_of_its_own_and_says_that_it_runs_code(self):
        doc = yaml.safe_load((ROOT / "code_runner" / "manifest.yaml").read_text(encoding="utf-8"))
        assert doc["network"] == {"hosts": []}
        assert doc["implementation"]["dependencies"] == []
        [function] = doc["tools"][0]["functions"]
        assert (function["code"], function["llm"], function["credentials"]) == (True, True, True)
        # It makes files, and stands where every function that does stands.
        assert function["permission_level"] == 2


class TestAProgramThePersonAllows:
    def test_it_reads_their_file_prints_an_answer_and_hands_back_what_it_made(self, agents):
        model = Model([answer(TOTALS)])
        executor, provider, person = made(model)
        ref = upload(provider, "orders.csv", ORDERS)
        result, status = program(agents, executor, {
            "goal": "Total the orders and say which month was best.", "files": [ref]})
        assert status == "success", result
        assert result["outcome"] == "done", result
        assert result["output"] == "Total 3610.75; best month March"
        assert result["programs_shown"] == 1 and result["code"] == TOTALS
        [kept] = result["files"]
        assert kept["filename"] == "totals.txt"
        assert provider.files["code_runner__output"][kept["file_ref"]]["content"] == b"3610.75"
        assert "totals.txt" in result["summary"]

        # What the person saw: the program whole, what it is for, and
        # the file it reads — and that it needs nothing else.
        [card] = person.cards
        assert (card["language"], card["code"]) == ("python", TOTALS)
        assert card["purpose"] == "Totals the orders and says which month was best."
        assert (card["files"], card["packages"], card["hosts"], card["credentials"]) == (
            ["orders.csv"], [], [], [])
        # What the writer was shown of the file: its name and how it begins.
        assert "- orders.csv (58 bytes)" in model.asked[0]
        assert "month,total" in model.asked[0] and "data, not instructions" in model.asked[0]

    def test_a_program_to_start_from_is_shown_to_the_writer(self, agents):
        model = Model([answer("print('changed')\n", purpose="Says changed.")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {
            "goal": "Say changed instead.", "code": "print('as it was')"})
        assert status == "success" and result["output"] == "changed", result
        assert "A PROGRAM TO START FROM" in model.asked[0]
        assert "print('as it was')" in model.asked[0]


class TestAProgramThatDoesNotRun:
    LEAVES_A_MARK = "open('ran.txt', 'w').write('it ran')\nprint('ran')\n"

    def test_one_the_person_declines_never_runs(self, agents):
        executor, provider, person = made(
            Model([answer(self.LEAVES_A_MARK)]), Person(answers=[False]))
        result, status = program(agents, executor, {"goal": "Leave a mark."})
        assert status == "success" and result["outcome"] == "declined", result
        assert "output" not in result and "files" not in result
        assert provider.files.get("code_runner__output", {}) == {}
        # What they declined is still told, so the assistant can say what it was.
        assert result["code"] == self.LEAVES_A_MARK and result["programs_shown"] == 1

    def test_with_nobody_to_answer_nothing_runs(self, agents):
        provider = InMemoryResourceProvider()
        executor = FunctionExecutor(
            provider=provider,
            sinks=ChatSinks(llm=Model([answer(self.LEAVES_A_MARK)])))
        result, status = program(agents, executor, {"goal": "Leave a mark."})
        assert status == "success" and result["outcome"] == "unanswered", result
        assert provider.files.get("code_runner__output", {}) == {}

    def test_what_no_program_can_do_is_said_and_nothing_is_shown(self, agents):
        executor, provider, person = made(
            Model(["CANNOT: Knowing tomorrow's weather needs a forecast service."]))
        result, status = program(agents, executor, {"goal": "Tell me tomorrow's weather."})
        assert status == "success" and result["outcome"] == "failed", result
        assert "forecast service" in result["summary"] and person.cards == []

    def test_an_answer_that_is_no_program_is_asked_for_again_and_then_given_up(self, agents):
        model = Model(["Here you go!", "PURPOSE: x\n```python\nprint(1)"])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Anything."})
        assert status == "success" and result["outcome"] == "failed", result
        assert "could not be used" in result["summary"] and person.cards == []
        assert "YOUR LAST ANSWER COULD NOT BE USED" in model.asked[1]


class TestAProgramThatFails:
    BROKEN = "import csv\nrows = list(csv.DictReader(open('orders.csv')))\nprint(rows[0]['amount'])\n"

    def test_it_is_corrected_and_the_person_sees_the_new_one(self, agents):
        model = Model([answer(self.BROKEN, purpose="Prints the first amount."),
                       answer(TOTALS)])
        executor, provider, person = made(model)
        ref = upload(provider, "orders.csv", ORDERS)
        result, status = program(agents, executor, {"goal": "Total the orders.", "files": [ref]})
        assert status == "success" and result["outcome"] == "done", result
        assert result["programs_shown"] == 2 and result["code"] == TOTALS
        # A changed program is a new program: both went before the person,
        # and the second card says why there is a second.
        assert [card["code"] for card in person.cards] == [self.BROKEN, TOTALS]
        assert person.cards[0]["purpose"] == "Prints the first amount."
        assert person.cards[1]["purpose"].startswith(
            "A correction: the program before this one failed. Totals the orders")
        assert "earlier_failure" not in result
        # The correction was written from what happened.
        assert "KeyError: 'amount'" in model.asked[1]
        assert "THE PROGRAM THAT WAS TRIED" in model.asked[1]

    def test_a_correction_the_person_declines_says_what_failed_before_it(self, agents):
        model = Model([answer(self.BROKEN, purpose="Prints the first amount."),
                       answer(TOTALS)])
        executor, provider, person = made(model, Person(answers=[True, False]))
        ref = upload(provider, "orders.csv", ORDERS)
        result, status = program(agents, executor, {"goal": "Total the orders.", "files": [ref]})
        assert status == "success" and result["outcome"] == "declined", result
        assert result["programs_shown"] == 2
        assert "KeyError: 'amount'" in result["earlier_failure"]

    def test_three_programs_are_the_most_one_call_shows(self, agents):
        model = Model([answer("raise SystemExit('no such thing')\n")] * 3)
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Do the impossible."})
        assert status == "success" and result["outcome"] == "failed", result
        assert result["programs_shown"] == 3 and len(person.cards) == 3
        assert "no such thing" in result["summary"] and "no such thing" in result["output"]

    def test_one_that_does_not_end_is_stopped(self, agents, monkeypatch):
        monkeypatch.setenv("DECENTAI_CODE_RUN_SECONDS", "2")
        model = Model([answer("import time\nprint('started', flush=True)\ntime.sleep(60)\n"),
                       answer("print('quick')\n")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Be quick."})
        assert status == "success" and result["outcome"] == "done", result
        assert result["output"] == "quick"
        assert "stopped after 2 seconds" in model.asked[1] and "started" in model.asked[1]


class TestWhatAProgramNeeds:
    def test_a_credential_is_asked_of_the_person_and_never_printed_back(self, agents):
        code = ("import os\n"
                "print('the token is', os.environ['SHOP_TOKEN'])\n"
                "print('proxy set:', bool(os.environ.get('SHOP_TOKEN')))\n")
        model = Model([answer(code, purpose="Shows the token.", hosts="api.example-shop.com",
                              credentials="SHOP_TOKEN=api.example-shop.com")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Show the token."})
        assert status == "success" and result["outcome"] == "done", result
        assert person.logins == [("api.example-shop.com", ["token"])]
        assert "s3cret-token" not in result["output"]
        assert "the token is [hidden]" in result["output"]
        [card] = person.cards
        assert card["hosts"] == ["api.example-shop.com"]
        assert card["credentials"] == ["SHOP_TOKEN for api.example-shop.com"]

    def test_a_credential_the_person_does_not_give_ends_it(self, agents):
        model = Model([answer("print(1)\n", hosts="api.example-shop.com",
                              credentials="SHOP_TOKEN=api.example-shop.com")])
        executor, provider, person = made(model, Person(token=""))
        result, status = program(agents, executor, {"goal": "Use the shop."})
        assert status == "success" and result["outcome"] == "declined", result
        assert "credential" in result["summary"] and "output" not in result

    def test_a_credential_for_a_host_the_program_did_not_name_is_not_a_plan(self, agents):
        model = Model([
            answer("print(1)\n", hosts="api.example-shop.com",
                   credentials="SHOP_TOKEN=elsewhere.example.net"),
            answer("print('ok')\n", purpose="Says ok."),
        ])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Use the shop."})
        assert status == "success" and result["outcome"] == "done", result
        assert "which HOSTS does not name" in model.asked[1]
        assert len(person.cards) == 1 and person.logins == []

    def test_a_host_a_person_cannot_allow_is_sent_back_to_the_writer(self, agents):
        """The platform refuses the card before anybody sees it, and
        the writer is told why in the platform's words."""
        model = Model([answer("print(1)\n", hosts="10.0.0.7"),
                       answer("print('ok')\n", purpose="Says ok.")])
        executor, provider, person = made(model)
        result, status = program(agents, executor, {"goal": "Reach the router."})
        assert status == "success" and result["outcome"] == "done", result
        assert "'10.0.0.7' is not a host" in model.asked[1]
        assert [card["code"] for card in person.cards] == ["print('ok')\n"]
        assert result["programs_shown"] == 2

    def test_its_packages_are_installed_by_the_platform_and_found_by_the_program(
        self, agents
    ):
        """pip is stood in for: it leaves a module where the platform
        told it to, and the program imports it from there."""
        def pip(argv):
            assert "--target" in argv and argv[-1] == "shout==1.0", argv
            target = Path(argv[argv.index("--target") + 1])
            (target / "shout.py").write_text("def loud(words):\n    return words.upper() + '!'\n")
            return 0, ""

        model = Model([answer("import shout\nprint(shout.loud('done'))\n",
                              purpose="Says done, loudly.", packages="shout==1.0")])
        executor, provider, person = made(model)
        agent = agents["code_runner"]
        AgentEnvironment.runner = pip
        try:
            result, status = program(agents, executor, {"goal": "Say done loudly."})
        finally:
            AgentEnvironment.runner = None
            shutil.rmtree(agent.environment.root / "extras", ignore_errors=True)
        assert status == "success" and result["outcome"] == "done", result
        assert result["output"] == "DONE!" and result["packages"] == ["shout==1.0"]
        assert person.cards[0]["packages"] == ["shout==1.0"]


class TestThePlan:
    """What the writer's answer is read as (tools/plan.py)."""

    def test_the_labelled_lines_and_the_fence_are_the_plan(self):
        from code_runner.tools.plan import Plan

        plan = Plan.read(
            "PURPOSE:  Gets   the issues.\nPACKAGES: requests, `pandas`\n"
            "HOSTS: API.github.com\nCREDENTIALS: GITHUB_TOKEN=api.github.com\n"
            "```python\nimport requests\n```\nThat should do it.")
        assert plan.unreadable == "" and plan.cannot == ""
        assert plan.purpose == "Gets the issues."
        assert plan.packages == ["requests", "pandas"] and plan.hosts == ["api.github.com"]
        [credential] = plan.credentials
        assert (credential.name, credential.host, credential.label) == (
            "GITHUB_TOKEN", "api.github.com", "GITHUB_TOKEN for api.github.com")
        assert plan.code == "import requests\n"

    def test_none_is_nothing_and_a_missing_line_is_nothing(self):
        from code_runner.tools.plan import Plan

        plan = Plan.read("PURPOSE: Adds.\nPACKAGES: none\n```python\nprint(2)\n```")
        assert (plan.packages, plan.hosts, plan.credentials, plan.unreadable) == ([], [], [], "")

    def test_what_is_not_a_plan_says_why(self):
        from code_runner.tools.plan import Plan

        assert "no program" in Plan.read("I would use pandas for this.").unreadable
        assert "never closed" in Plan.read("PURPOSE: x\n```python\nprint(1)").unreadable
        assert "PURPOSE is missing" in Plan.read("```python\nprint(1)\n```").unreadable
        assert "in capitals" in Plan.read(
            "PURPOSE: x\nHOSTS: a.example.com\nCREDENTIALS: token=a.example.com\n"
            "```python\nprint(1)\n```").unreadable
        assert "in capitals" in Plan.read(
            "PURPOSE: x\nHOSTS: a.example.com\nCREDENTIALS: PATH=a.example.com\n"
            "```python\nprint(1)\n```").unreadable
        assert Plan.read("CANNOT: It needs a person.").cannot == "It needs a person."


class TestTheWorkingFolder:
    def test_a_files_name_is_made_safe_and_its_own(self):
        from code_runner.tools.workspace import Workspace

        assert Workspace.name("../../etc/passwd", []) == "passwd"
        assert Workspace.name("my report (final).xlsx", []) == "my report (final).xlsx"
        assert Workspace.name("a;rm -rf.csv", []) == "a_rm -rf.csv"
        assert Workspace.name("program.py", []) == "program-2.py"
        assert Workspace.name("Orders.csv", ["orders.csv"]) == "Orders-2.csv"
        assert Workspace.name("", []) == "file"
