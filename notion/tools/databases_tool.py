"""A database's schema, and its rows.

The schema is what every row write is checked against and what a
filter is built from, so it is the first thing to read about a
database: property names, their types, and the options a select,
multi-select or status property allows.
"""

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure, invalid
from .notion_api import NotionError
from .schema import DatabaseSchema, PageRow, SchemaProblem


class DatabasesTool(ToolBase):
    id = "databases"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            schema = DatabaseSchema(client.database(str(call.inputs["database_id"])))
        except NotionError as exc:
            return failure(exc)
        return {"database_id": schema.database_id, "title": schema.title,
                "url": schema.url, "properties": schema.rows()}, "success"

    async def query(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        database_id = str(inputs["database_id"]).strip()
        most = int(inputs.get("max_results") or 20)
        try:
            schema = DatabaseSchema(client.database(database_id))
            try:
                filter_ = None
                if inputs.get("filter_property"):
                    if "filter_operator" not in inputs:
                        return invalid("filter_property needs filter_operator "
                                       "(equals, contains, checkbox or on_or_after).")
                    filter_ = schema.filter(str(inputs["filter_property"]),
                                            str(inputs["filter_operator"]),
                                            inputs.get("filter_value"))
                sorts = None
                if inputs.get("sort_property"):
                    sorts = [schema.sort(str(inputs["sort_property"]),
                                         str(inputs.get("sort_direction") or "descending"))]
            except SchemaProblem as problem:
                return invalid(str(problem))
            answer = client.query(database_id, most, str(inputs.get("start_cursor") or ""),
                                  filter_, sorts)
        except NotionError as exc:
            return failure(exc)
        rows = [PageRow.row(page) for page in (answer.get("results") or [])[:most]]
        result = {"database": schema.title, "rows": rows}
        if answer.get("has_more") and answer.get("next_cursor"):
            result["next_cursor"] = str(answer["next_cursor"])
        return result, "success"
