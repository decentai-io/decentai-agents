"""The reference agent, run the way production runs it: in its own
worker process, over the worker protocol, against the platform's
simulated resources. Each test pins one feature the manifest declares.
"""

import asyncio
from pathlib import Path

import yaml

from ai_runtime.execution.executor import FunctionExecutor
from ai_runtime.sinks import ChatSinks
from sim.resources import InMemoryResourceProvider

ROOT = Path(__file__).resolve().parents[1]


def run(awaitable):
    return asyncio.run(awaitable)


def manifest():
    return yaml.safe_load(
        (ROOT / "notebook" / "manifest.yaml").read_text(encoding="utf-8"))


def function(doc, name):
    tool_id, function_id = name.split(".")
    tool = next(t for t in doc["tools"] if t["id"] == tool_id)
    return next(f for f in tool["functions"] if f["id"] == function_id)


def test_the_catalog_offers_one_agent_that_implements_its_manifest(agents):
    """Verified the way installation verifies: a worker spawned from the
    agent's own environment imports the code and checks every declared
    function has a method."""
    from ai_runtime.agents.worker_handle import WorkerHandle

    assert set(agents) == {"notebook", "gmail", "google_calendar", "google_drive", "google_sheets", "google_forms", "google_tasks", "microsoft_calendar", "microsoft_todo", "outlook", "onedrive", "excel_online", "microsoft_forms", "dropbox", "box", "teams", "slack", "notion", "documents", "tasks", "todoist", "timesheets", "sheets", "expenses", "places", "google_docs", "word_online", "web_reader", "web_watch", "browser", "feeds", "applications", "slides", "json_data", "connection_check", "mail", "code_runner"}
    agent = agents["notebook"]
    errors = WorkerHandle.probe(
        agent.environment.python, agent.folder, agent.manifest.document)
    assert errors == [], errors


def test_the_manifest_declares_every_feature_the_platform_enforces():
    doc = manifest()
    levels = {f"{t['id']}.{f['id']}": f["permission_level"]
              for t in doc["tools"] for f in t["functions"]}
    assert set(levels.values()) == {0, 1, 2, 3}
    assert function(doc, "note.find")["schedulable"] is True
    assert function(doc, "note.summarize")["llm"] is True
    resources = doc["resources"]
    note = next(r for r in resources["data"] if r["id"] == "note")
    assert note["user_access"] == ["create", "update"]
    document = next(r for r in resources["files"] if r["id"] == "document")
    assert document["user_access"] == ["create"]
    assert len(doc["implementation"]["dependencies"]) == 2
    assert "notebook" in doc["authorization"]["scopes"]


def test_dependencies_live_in_the_agents_own_environment(agents):
    """titlecase and humanize are installed into the agent's venv and
    used by the code — a lowercase title comes back cased."""
    provider = InMemoryResourceProvider()
    executor = FunctionExecutor(provider=provider)

    async def scenario():
        saved, status = await executor.invoke(
            agents["notebook"], "notebook.note.save",
            {"notebook": "work", "title": "handoff notes"})
        assert status == "success", saved
        note, status = await executor.invoke(
            agents["notebook"], "notebook.note.get",
            {"note_ref": saved["note_ref"]})
        assert status == "success", note
        assert note["title"] == "Handoff Notes"

    run(scenario())


def test_json_round_trip_preserves_priority_and_content(agents):
    provider = InMemoryResourceProvider()
    executor = FunctionExecutor(provider=provider)

    async def scenario():
        saved, status = await executor.invoke(
            agents["notebook"], "notebook.note.save", {
                "notebook": "Work", "title": "Handoff", "priority": 2,
                "content": {"text": "Call the customer"},
            })
        assert status == "success", saved
        exported, status = await executor.invoke(
            agents["notebook"], "notebook.archive.export",
            {"notebook": "work", "format": "json"}, chat_level=2,
        )
        assert status == "success", exported
        assert exported["filename"] == "work-notes.json"

        imported, status = await executor.invoke(
            agents["notebook"], "notebook.archive.import",
            {"notebook": "copy", "file_ref": exported["file_ref"]},
            chat_level=2,
        )
        assert status == "success", imported
        copied = provider.data["notebook__note"][imported["note_refs"][0]]["keys"]
        assert copied["priority"] == 2

    run(scenario())


def test_a_bad_import_creates_no_notes(agents):
    provider = InMemoryResourceProvider()
    executor = FunctionExecutor(provider=provider)

    async def scenario():
        uploaded = await provider.create_file(
            "notebook__document", "bad.csv",
            "title,notebook,priority,content\nGood,x,2,{}\nBad,x,nope,{}\n",
        )
        result, status = await executor.invoke(
            agents["notebook"], "notebook.archive.import",
            {"notebook": "copy", "file_ref": uploaded["resource_ref"]},
            chat_level=2,
        )
        assert status == "error"
        assert "Invalid priority" in result["error"]
        assert provider.data.get("notebook__note", {}) == {}

    run(scenario())


def test_the_secret_reaches_only_the_functions_that_declared_it(agents):
    provider = InMemoryResourceProvider(secrets={
        "notebook__connection": {
            "base_url": "https://demo.invalid",
            "api_token": "test-token",
        }
    })
    executor = FunctionExecutor(provider=provider)

    async def scenario():
        connected, status = await executor.invoke(
            agents["notebook"], "notebook.sync.status", {})
        assert status == "success"
        assert connected == {"connected": True, "remote": "simulated://notebook"}

        await executor.invoke(agents["notebook"], "notebook.note.save", {
            "notebook": "Work", "title": "Secret-backed demo"})
        pushed, status = await executor.invoke(
            agents["notebook"], "notebook.sync.push", {"notebook": "work"},
            chat_level=3,
        )
        assert status == "success", pushed
        assert pushed["pushed"] == 1
        assert len(pushed["digest"]) == 64

    run(scenario())


def test_the_level_three_push_needs_the_chats_trust(agents):
    """Below its level and with nobody to approve, the executor refuses —
    the approval card is the chat's, never the agent's."""
    provider = InMemoryResourceProvider(secrets={
        "notebook__connection": {"base_url": "u", "api_token": "t"}})
    executor = FunctionExecutor(provider=provider)
    result, status = run(executor.invoke(
        agents["notebook"], "notebook.sync.push", {"notebook": "work"},
        chat_level=1))
    assert status == "error"
    assert "approv" in result["error"].lower()


def test_summarize_thinks_with_the_chats_model(agents):
    """The model is the platform's to hand over: the executor gives the
    invocation a completion seam because the manifest declared llm."""
    provider = InMemoryResourceProvider()
    asked = []

    async def model(messages, max_tokens=None):
        asked.append((messages, max_tokens))
        return "Two notes about the handoff, one urgent."

    executor = FunctionExecutor(provider=provider, sinks=ChatSinks(llm=model))

    async def scenario():
        for title, priority in (("Handoff", 1), ("Follow up", 3)):
            await executor.invoke(agents["notebook"], "notebook.note.save",
                                  {"notebook": "work", "title": title,
                                   "priority": priority})
        summary, status = await executor.invoke(
            agents["notebook"], "notebook.note.summarize",
            {"notebook": "work", "max_words": 40})
        assert status == "success", summary
        assert summary == {"summary": "Two notes about the handoff, one urgent.",
                           "note_count": 2}

    run(scenario())
    (messages, max_tokens), = asked
    assert max_tokens == 160
    assert any("Handoff" in m.get("content", "") for m in messages)


def test_a_function_without_llm_cannot_ask_for_the_model(agents):
    """find never declared llm, so even with a model wired in the
    executor gives it none — and it does not need one."""
    executor = FunctionExecutor(
            provider=InMemoryResourceProvider(),
            sinks=ChatSinks(llm=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not be called"))))
    found, status = run(executor.invoke(
        agents["notebook"], "notebook.note.find", {"notebook": "work"}))
    assert status == "success"
    assert found == {"notes": [], "total": 0}


def test_every_agent_explains_itself_by_example():
    """Three prompts a person can send as they are, with a title each,
    in every manifest — what the agent's page shows first."""
    catalog = yaml.safe_load((ROOT / "decentai-agents.yaml").read_text(encoding="utf-8"))
    for entry in catalog["agents"]:
        doc = yaml.safe_load((ROOT / entry["path"] / "manifest.yaml").read_text(encoding="utf-8"))
        examples = doc["agent"].get("examples") or []
        assert len(examples) >= 3, f"{entry['id']} offers {len(examples)} examples"
        for example in examples:
            assert example["title"].strip() and example["prompt"].strip(), entry["id"]
            assert len(example["prompt"]) <= 500, entry["id"]
