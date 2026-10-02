"""A table's rows, read by header, and rows appended to it.

A table is the part of a workbook that knows its own shape: a header
row and the rows under it, growing as rows are added. Rows come back
as objects keyed by header, so the assistant reads "Customer" rather
than column C. Appending is level 3 in the manifest; it goes through a
workbook session and is recorded with what it wrote.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, no_client
from .graph_workbook import GraphError, keyed


class TableTool(ToolBase):
    id = "table"

    async def rows(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        item_id = str(call.inputs["workbook_id"])
        most = int(call.inputs.get("max_results") or 20)
        token = str(call.inputs.get("page_token") or "0").strip()
        if not token.isdigit():
            return {"error": "page_token is the next_page_token of an earlier "
                             "answer; pass it on unchanged.", "kind": "invalid"}, "error"
        start = int(token)
        try:
            region = client.table_region(item_id, str(call.inputs["table"]))
            lines = client.read_rows(item_id, region, start, start + most)
        except GraphError as exc:
            return failure(exc)
        result = {"table": region.name, "worksheet": region.worksheet,
                  "headers": region.headers, "total": region.rows,
                  "rows": [keyed(region.headers, line) for line in lines]}
        if start + most < region.rows:
            result["next_page_token"] = str(start + most)
        return result, "success"

    async def add_rows(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        item_id = str(call.inputs["workbook_id"])
        try:
            item = client.item(item_id)
            region = client.table_region(item_id, str(call.inputs["table"]))
        except GraphError as exc:
            return failure(exc)
        if region.columns_cut:
            return {"error": f"Table {region.name} is wider than this agent reads; "
                             f"no rows were added.", "kind": "invalid"}, "error"
        # Each row is matched to the header by name, case aside; a column
        # not given is left empty, and a name the table does not have is
        # refused rather than dropped, so nothing the user said is lost.
        position = {h.strip().lower(): i for i, h in enumerate(region.headers)}
        values, unknown = [], []
        for row in call.inputs["rows"]:
            line = [""] * len(region.headers)
            for name, value in row.items():
                i = position.get(str(name).strip().lower())
                if i is None:
                    if name not in unknown:
                        unknown.append(name)
                    continue
                line[i] = value
            values.append(line)
        if unknown:
            return {"error": f"Table {region.name} has no column named "
                             f"{', '.join(repr(u) for u in unknown)}; its columns are "
                             f"{', '.join(region.headers)}. No rows were added.",
                    "kind": "invalid"}, "error"
        try:
            session = client.create_session(item_id)
        except GraphError as exc:
            return failure(exc)
        keys = {"kind": "add_rows", "workbook_id": item_id,
                "workbook": str(item.get("name") or ""), "target": region.name,
                "rows": len(values)}
        written = {"written": {"headers": region.headers, "rows": values}}
        try:
            client.add_table_rows(item_id, region.name, values, session)
        except GraphError as exc:
            if exc.kind == "unknown":
                record = await call.resources.create_data(
                    "write", {**keys, "status": "unknown", **written})
                return {"error": exc.message + " Read the table before adding "
                                               "these rows again.",
                        "kind": "unknown", "write_ref": record["resource_ref"]}, "error"
            return failure(exc)
        finally:
            client.close_session(item_id, session)
        record = await call.resources.create_data(
            "write", {**keys, "status": "written", **written})
        return {"write_ref": record["resource_ref"], "table": region.name,
                "added": len(values), "total_rows": region.rows + len(values)}, "success"
