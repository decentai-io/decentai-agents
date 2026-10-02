"""Finding workbooks, and seeing what is inside one.

A workbook is found by drive search and kept only if it is an .xlsx:
the workbook API opens nothing else (not .xls, not .csv). What is
inside is named the way a person would point at it — worksheets with
the block of cells in use, tables with their header row and how many
rows they hold — so the next call can address a range or a table
without guessing.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, no_client
from .graph_workbook import GraphError, is_workbook, split_address, workbook_row

#: Search matches names and contents alike, so more are asked for than
#: are shown, and the ones that are not workbooks are dropped.
SEARCH_WINDOW = 50
MAX_LISTED = 25


class WorkbooksTool(ToolBase):
    id = "workbooks"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        query = str(call.inputs.get("query") or "").strip() or "xlsx"
        most = int(call.inputs.get("max_results") or 10)
        try:
            items = client.search(query, SEARCH_WINDOW)
        except GraphError as exc:
            return failure(exc)
        rows = [workbook_row(i) for i in items if is_workbook(i)]
        return {"workbooks": rows[:most], "more": len(rows) > most}, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        item_id = str(call.inputs["workbook_id"])
        try:
            item = client.item(item_id)
            if not is_workbook(item):
                return {"error": f"'{item.get('name')}' is not an .xlsx workbook; "
                                 f"only .xlsx files open here.",
                        "kind": "invalid"}, "error"
            sheets = []
            for sheet in client.worksheets(item_id)[:MAX_LISTED]:
                name = str(sheet.get("name") or "")
                used = client.used_range(item_id, name)
                sheets.append({
                    "name": name,
                    "used_range": split_address(used.get("address"))[1],
                    "rows": int(used.get("rowCount") or 0),
                    "columns": int(used.get("columnCount") or 0),
                    "visible": str(sheet.get("visibility") or "Visible") == "Visible",
                })
            tables = []
            for table in client.tables(item_id)[:MAX_LISTED]:
                region = client.table_region(item_id, str(table.get("name") or ""))
                tables.append({"name": region.name, "worksheet": region.worksheet,
                               "headers": region.headers, "rows": region.rows})
        except GraphError as exc:
            return failure(exc)
        return {"workbook": workbook_row(item), "worksheets": sheets,
                "tables": tables}, "success"
