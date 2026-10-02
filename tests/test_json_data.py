"""The JSON agent in a real worker. Every fixture is written here in
full, so what the agent reports can be checked against what the file
actually says.
"""

import asyncio
import json

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

ORDERS = {
    "exported": "2026-10-14",
    "orders": [
        {"id": "A-1001", "customer": {"name": "Harbourline", "city": "Muscat"},
         "total": 3700.0, "tags": ["repeat", "priority"],
         "lines": [{"sku": "ARIA-1", "qty": 20}]},
        {"id": "A-1002", "customer": {"name": "Cedar Office"},
         "total": 1720.5, "lines": [{"sku": "ARIA-1", "qty": 10}]},
        {"id": "A-1003", "customer": {"name": "Northlight", "city": "Salalah"},
         "lines": []},
    ],
}

ORDERS_V2 = {
    "exported": "2026-10-15",
    "orders": [
        {"id": "A-1001", "customer": {"name": "Harbourline", "city": "Muscat"},
         "total": 3580.0, "tags": ["repeat", "priority"],
         "lines": [{"sku": "ARIA-1", "qty": 20}]},
        {"id": "A-1002", "customer": {"name": "Cedar Office"},
         "total": 1720.5, "lines": [{"sku": "ARIA-1", "qty": 10}]},
        {"id": "A-1003", "customer": {"name": "Northlight", "city": "Salalah"},
         "lines": [], "note": "on hold"},
    ],
}

ORDER_SCHEMA = {
    "type": "object",
    "required": ["id", "total"],
    "properties": {
        "id": {"type": "string"},
        "total": {"type": "number", "minimum": 0},
    },
}

JSONL = b'{"id": 1, "ok": true}\n{"id": 2, "ok": false}\n{"id": 3}\n'


def run(awaitable):
    return asyncio.run(awaitable)


def make():
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=2):
    return run(ex.invoke(agents["json_data"], name, inputs, chat_level=chat_level))


def upload(provider, filename, raw) -> str:
    if not isinstance(raw, bytes):
        raw = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    record = run(provider.create_file("chat_attachment", filename, raw))
    return record["resource_ref"]


@pytest.fixture
def orders(agents):
    ex, provider = make()
    return ex, provider, upload(provider, "orders.json", ORDERS)


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["json_data"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []


class TestInspecting:
    def test_the_key_map_says_which_records_lack_a_field(self, agents):
        ex, provider = make()
        ref = upload(provider, "orders.json", ORDERS["orders"])
        info, status = invoke(agents, ex, "json_data.read.inspect", {"file_ref": ref})
        assert status == "success", info
        assert info["kind"] == "json" and info["valid"] is True
        assert info["top_level"] == "array" and info["records"] == 3
        keys = {row["path"]: row for row in info["keys"]}
        assert keys["id"]["present"] == 3
        assert keys["total"]["present"] == 2      # A-1003 has none
        assert keys["customer.city"]["present"] == 2
        assert keys["lines[].qty"]["present"] == 2
        assert keys["total"]["types"] == ["number"]
        assert keys["id"]["example"] == "A-1001"

    def test_json_lines_is_recognised_without_being_told(self, agents):
        ex, provider = make()
        ref = upload(provider, "events.log", JSONL)
        info, status = invoke(agents, ex, "json_data.read.inspect", {"file_ref": ref})
        assert status == "success", info
        assert info["kind"] == "jsonl" and info["records"] == 3
        keys = {row["path"]: row["present"] for row in info["keys"]}
        assert keys["id"] == 3 and keys["ok"] == 2

    def test_a_broken_file_names_the_line_and_column(self, agents):
        ex, provider = make()
        ref = upload(provider, "broken.json", b'{"a": 1,\n "b": }\n')
        info, status = invoke(agents, ex, "json_data.read.inspect", {"file_ref": ref})
        assert status == "success", info
        assert info["valid"] is False and info["kind"] == "unsupported"
        assert info["error_line"] == 2 and info["error_column"] > 0
        assert "not valid JSON" in info["problem"]

    def test_a_csv_is_told_apart_from_broken_json(self, agents):
        ex, provider = make()
        ref = upload(provider, "list.csv", b"id,total\nA-1,10\nA-2,20\n")
        info, _ = invoke(agents, ex, "json_data.read.inspect", {"file_ref": ref})
        assert info["kind"] == "csv" and "shape.to_json" in info["problem"]


class TestSelecting:
    def test_a_path_reaches_every_item(self, agents, orders):
        ex, _, ref = orders
        result, status = invoke(agents, ex, "json_data.read.select", {
            "file_ref": ref, "path": "orders[*].customer.name"})
        assert status == "success", result
        assert result["matches"] == 3
        assert [r["value"] for r in result["results"]] == [
            "Harbourline", "Cedar Office", "Northlight"]
        assert result["results"][0]["path"] == "orders[0].customer.name"

    def test_a_missing_key_matches_nothing_rather_than_guessing(self, agents, orders):
        ex, _, ref = orders
        result, _ = invoke(agents, ex, "json_data.read.select", {
            "file_ref": ref, "path": "orders[*].discount"})
        assert result["matches"] == 0 and result["results"] == []

    def test_one_item_and_the_whole_document_are_both_paths(self, agents, orders):
        ex, _, ref = orders
        one, _ = invoke(agents, ex, "json_data.read.select", {
            "file_ref": ref, "path": "orders[1].id"})
        assert one["results"][0]["value"] == "A-1002"
        root, _ = invoke(agents, ex, "json_data.read.select", {
            "file_ref": ref, "path": "$", "max_value_chars": 200})
        assert root["matches"] == 1
        assert root["results"][0]["truncated"] is True
        assert root["results"][0]["preview"].endswith("…")

    def test_a_nonsense_path_is_refused_with_a_reason(self, agents, orders):
        ex, _, ref = orders
        result, status = invoke(agents, ex, "json_data.read.select", {
            "file_ref": ref, "path": "orders[oops]"})
        assert status == "error" and "not an index" in result["error"]


class TestValidating:
    def test_each_record_reports_the_one_that_fails(self, agents):
        ex, provider = make()
        ref = upload(provider, "orders.json", ORDERS["orders"])
        result, status = invoke(agents, ex, "json_data.check.validate", {
            "file_ref": ref, "schema": ORDER_SCHEMA, "each_record": True})
        assert status == "success", result
        assert result["valid"] is False
        assert result["checked"] == 3 and result["failures"] == 1
        assert result["errors"][0]["record"] == 2
        assert result["errors"][0]["rule"] == "required"
        record = provider.data["json_data__validation"][result["validation_ref"]]
        assert record["keys"]["valid"] == "no"
        assert record["keys"]["failures"] == 1

    def test_a_file_that_matches_is_valid(self, agents):
        ex, provider = make()
        ref = upload(provider, "one.json", {"id": "A-1", "total": 5})
        result, status = invoke(agents, ex, "json_data.check.validate", {
            "file_ref": ref, "schema": ORDER_SCHEMA})
        assert status == "success", result
        assert result["valid"] is True and result["errors"] == []

    def test_a_schema_can_be_a_file(self, agents):
        ex, provider = make()
        ref = upload(provider, "one.json", {"id": "A-1"})
        schema_ref = upload(provider, "schema.json", ORDER_SCHEMA)
        result, status = invoke(agents, ex, "json_data.check.validate", {
            "file_ref": ref, "schema_ref": schema_ref})
        assert status == "success", result
        assert result["valid"] is False
        assert "total" in result["errors"][0]["message"]

    def test_no_schema_at_all_is_refused(self, agents, orders):
        ex, _, ref = orders
        result, status = invoke(agents, ex, "json_data.check.validate",
                                {"file_ref": ref})
        assert status == "error" and "No schema" in result["error"]

    def test_a_broken_schema_is_named_as_the_schemas_fault(self, agents, orders):
        ex, _, ref = orders
        result, status = invoke(agents, ex, "json_data.check.validate", {
            "file_ref": ref, "schema": {"type": "nonsense"}})
        assert status == "error" and "schema itself" in result["error"]

    def test_validating_needs_level_one(self, agents, orders):
        ex, _, ref = orders
        result, status = invoke(agents, ex, "json_data.check.validate",
                                {"file_ref": ref, "schema": ORDER_SCHEMA},
                                chat_level=0)
        assert status == "error" and result.get("denied") is True


class TestComparing:
    def test_changes_are_reported_by_path(self, agents):
        ex, provider = make()
        a = upload(provider, "v1.json", ORDERS)
        b = upload(provider, "v2.json", ORDERS_V2)
        result, status = invoke(agents, ex, "json_data.compare.versions", {
            "file_ref_a": a, "file_ref_b": b})
        assert status == "success", result
        assert result["identical"] is False
        assert result["changed"] == 2        # exported date, and one total
        assert result["added"] == 1          # the note on A-1003
        assert result["removed"] == 0
        by_path = {c["path"]: c for c in result["changes"]}
        assert by_path["orders[0].total"]["before"] == "3700.0"
        assert by_path["orders[0].total"]["after"] == "3580.0"
        assert by_path["orders[2].note"]["kind"] == "added"

    def test_the_same_file_twice_is_identical(self, agents):
        ex, provider = make()
        a = upload(provider, "v1.json", ORDERS)
        b = upload(provider, "same.json", ORDERS)
        result, _ = invoke(agents, ex, "json_data.compare.versions", {
            "file_ref_a": a, "file_ref_b": b})
        assert result["identical"] is True and result["changes"] == []


class TestShaping:
    def test_json_becomes_a_csv_with_dotted_columns(self, agents, orders):
        ex, provider, ref = orders
        result, status = invoke(agents, ex, "json_data.shape.to_table", {
            "file_ref": ref, "filename": "orders", "record_path": "orders[*]"})
        assert status == "success", result
        assert result["filename"] == "orders.csv" and result["rows"] == 3
        assert "customer.name" in result["columns"]
        text = provider.files["json_data__result"][result["file_ref"]]["content"]
        text = text.decode("utf-8") if isinstance(text, bytes) else text
        lines = text.strip().splitlines()
        assert lines[0].startswith("id,customer.name")
        assert "Harbourline" in lines[1]
        # A list of scalars keeps its JSON so nothing is lost.
        assert '["repeat", "priority"]' in text or '[""repeat"", ""priority""]' in text
        record = provider.data["json_data__output"][result["output_ref"]]
        assert record["keys"]["kind"] == "csv" and record["keys"]["rows"] == 3

    def test_chosen_columns_are_kept_in_order(self, agents, orders):
        ex, provider, ref = orders
        result, _ = invoke(agents, ex, "json_data.shape.to_table", {
            "file_ref": ref, "filename": "short", "record_path": "orders[*]",
            "columns": ["customer.name", "total"]})
        assert result["columns"] == ["customer.name", "total"]
        text = provider.files["json_data__result"][result["file_ref"]]["content"]
        text = text.decode("utf-8") if isinstance(text, bytes) else text
        assert text.splitlines()[0] == "customer.name,total"
        # The record with no total leaves an empty cell, not a guess.
        assert text.strip().splitlines()[-1] == "Northlight,"

    def test_a_csv_becomes_json_and_identifiers_stay_text(self, agents):
        ex, provider = make()
        ref = upload(provider, "in.csv",
                     b"id,qty,price,ok,note\n007,20,18.5,true,\n")
        result, status = invoke(agents, ex, "json_data.shape.to_json", {
            "file_ref": ref, "filename": "out"})
        assert status == "success", result
        assert result["records"] == 1 and result["filename"] == "out.json"
        raw = provider.files["json_data__result"][result["file_ref"]]["content"]
        record = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)[0]
        assert record["id"] == "007"          # a reference, not seven
        assert record["qty"] == 20 and record["price"] == 18.5
        assert record["ok"] is True and record["note"] is None

    def test_a_table_round_trips_back_into_nested_json(self, agents, orders):
        ex, provider, ref = orders
        table, _ = invoke(agents, ex, "json_data.shape.to_table", {
            "file_ref": ref, "filename": "orders", "record_path": "orders[*]",
            "columns": ["id", "customer.name", "customer.city"]})
        raw = provider.files["json_data__result"][table["file_ref"]]["content"]
        back = upload(provider, "orders.csv", raw if isinstance(raw, bytes)
                      else raw.encode("utf-8"))
        result, status = invoke(agents, ex, "json_data.shape.to_json", {
            "file_ref": back, "filename": "orders-again"})
        assert status == "success", result
        raw = provider.files["json_data__result"][result["file_ref"]]["content"]
        records = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        assert records[0]["customer"] == {"name": "Harbourline", "city": "Muscat"}
        assert records[2]["customer"]["city"] == "Salalah"

    def test_writing_a_file_needs_level_two(self, agents, orders):
        ex, _, ref = orders
        result, status = invoke(agents, ex, "json_data.shape.to_table",
                                {"file_ref": ref, "filename": "x"},
                                chat_level=1)
        assert status == "error" and result.get("denied") is True
