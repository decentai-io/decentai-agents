"""Finding the workbooks that hold a form's responses.

There is no list of forms to ask for: Microsoft Forms has no supported
API. What can be found is the drive's .xlsx workbooks, and inside each
one whether a table carries the columns Forms writes. Every workbook
the search turned up is reported, those that match first, so the
assistant can say plainly when the form the user means is not kept in
a workbook at all.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, no_client
from .graph_workbook import GraphError, is_workbook, workbook_row
from .responses import is_response_table, questions

#: Search matches names and contents alike, so more are asked for than
#: are opened, and the ones that are not workbooks are dropped.
SEARCH_WINDOW = 50
#: Tables opened per workbook while looking for the response table.
MAX_TABLES = 10
MAX_QUESTIONS = 50


class FormsTool(ToolBase):
    id = "forms"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return no_client(why)
        query = str(call.inputs.get("query") or "").strip() or "xlsx"
        most = int(call.inputs.get("max_results") or 10)
        try:
            items = [i for i in client.search(query, SEARCH_WINDOW) if is_workbook(i)]
        except GraphError as exc:
            return failure(exc)
        rows = []
        for item in items[:most]:
            row = {**workbook_row(item), "is_response_workbook": False}
            try:
                for table in client.tables(row["item_id"])[:MAX_TABLES]:
                    region = client.table_region(row["item_id"], str(table.get("name") or ""))
                    if is_response_table(region.headers):
                        row.update(is_response_workbook=True, table=region.name,
                                   questions=questions(region.headers)[:MAX_QUESTIONS],
                                   responses=region.rows)
                        break
            except GraphError as exc:
                if exc.kind == "auth":
                    return failure(exc)
                # One workbook Excel cannot open (locked, damaged) is
                # not a reason to hide the others.
                row["problem"] = exc.message
            rows.append(row)
        rows.sort(key=lambda r: not r["is_response_workbook"])
        return {"workbooks": rows, "more": len(items) > most}, "success"
