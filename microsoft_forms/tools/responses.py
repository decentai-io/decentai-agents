"""What a Forms response table looks like, and one response as the
assistant reads it.

A form whose responses sync to Excel keeps them in a table (Microsoft
names it "Form1") whose header starts with the columns Forms itself
fills — ID, Start time, Completion time, Email, Name — followed by one
column per question, titled with the question. A table carrying those
five columns is taken to be a response table; nothing else about a
workbook says so.
"""

from .graph_workbook import clip

#: The columns Forms fills for every response, lower-cased.
REQUIRED = ("id", "start time", "completion time", "email", "name")
#: Also filled by Forms rather than answered, when present.
BOOKKEEPING = REQUIRED + ("last modified time",)


def is_response_table(headers) -> bool:
    names = {str(h).strip().lower() for h in headers}
    return all(column in names for column in REQUIRED)


def questions(headers):
    return [h for h in headers if h.strip().lower() not in BOOKKEEPING]


def response_table(client, item_id: str, table: str = ""):
    """The response table of a workbook — the one named, or the first
    that looks like one — as (region, None), or (None, the refusal)."""
    if table:
        region = client.table_region(item_id, table)
        if not is_response_table(region.headers):
            return None, ({"error": f"Table {region.name} is not a Forms response "
                                    f"table: it lacks the ID, Start time, Completion "
                                    f"time, Email and Name columns.",
                           "kind": "invalid"}, "error")
        return region, None
    for found in client.tables(item_id):
        region = client.table_region(item_id, str(found.get("name") or ""))
        if is_response_table(region.headers):
            return region, None
    return None, ({"error": "This workbook has no Forms response table (one with ID, "
                            "Start time, Completion time, Email and Name columns). "
                            "Only a form whose responses sync to an Excel workbook "
                            "can be read here.", "kind": "not_found"}, "error")


def response_row(region, line, index):
    """One row as a response, or None when its ID is not a number — a
    row a person typed into the table, not one Forms wrote."""
    position = {h.strip().lower(): i for i, h in enumerate(region.headers)}

    def cell(name):
        i = position.get(name)
        return str(line[i]).strip() if i is not None and i < len(line) else ""

    try:
        response_id = int(float(cell("id")))
    except ValueError:
        return None
    return {
        "response_id": response_id,
        "start_time": cell("start time"),
        "completion_time": cell("completion time"),
        "email": cell("email"),
        "name": cell("name"),
        "answers": {h: clip(line[i] if i < len(line) else "")
                    for i, h in enumerate(region.headers)
                    if h.strip().lower() not in BOOKKEEPING},
        "row_number": region.row_number(index),
    }

