"""Finding a spreadsheet, and seeing what is in it.

Sheets has no list of spreadsheets; Drive does, so find() asks Drive for
files of the spreadsheet type, newest first. get() is the map the
assistant reads before anything else: the tabs, by title, and how big
each tab's grid is.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .sheets_api import GoogleError, tab_row

#: A workbook may hold two hundred tabs; the model is shown the first
#: hundred and told how many there are.
MAX_TABS = 100


class SpreadsheetsTool(ToolBase):
    id = "spreadsheets"

    async def find(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            answer = client.find(str(call.inputs.get("name") or "").strip(),
                                 int(call.inputs.get("max_results") or 10),
                                 str(call.inputs.get("page_token") or ""))
        except GoogleError as exc:
            return failure(exc)
        rows = [{"spreadsheet_id": str(f.get("id") or ""),
                 "name": str(f.get("name") or ""),
                 "modified": str(f.get("modifiedTime") or ""),
                 "owner": str(((f.get("owners") or [{}])[0]).get("emailAddress") or ""),
                 "link": str(f.get("webViewLink") or "")}
                for f in answer.get("files") or []]
        result = {"spreadsheets": rows}
        if answer.get("nextPageToken"):
            result["next_page_token"] = str(answer["nextPageToken"])
        return result, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            meta = client.spreadsheet(str(call.inputs["spreadsheet_id"]))
        except GoogleError as exc:
            return failure(exc)
        tabs = [tab_row(s) for s in meta.get("sheets") or []]
        return {"spreadsheet_id": str(meta.get("spreadsheetId") or ""),
                "title": str((meta.get("properties") or {}).get("title") or ""),
                "link": str(meta.get("spreadsheetUrl") or ""),
                "tab_count": len(tabs),
                "tabs": tabs[:MAX_TABS]}, "success"
