"""Reading the calendar, finding a time, and booking on authorization.

The same contract as the Google Calendar agent's events tool. Graph is
asked for UTC and every time shown is converted here, in the zone the
user named or else the calendar's own.
"""

from datetime import date, datetime, timedelta, timezone

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .graph_calendar import GraphError, from_graph, utc_text
from .slots import find_slots, parse_clock, parse_when
from .zones import resolve_zone

# What free/busy counts as unavailable. "workingElsewhere" is available,
# only somewhere else; "free" is not busy at all.
BUSY = {"busy", "tentative", "oof"}


def _address(entry) -> str:
    return str(((entry or {}).get("emailAddress") or {}).get("address") or "")


def _row(event, zone):
    start, end = from_graph(event.get("start")), from_graph(event.get("end"))
    all_day = bool(event.get("isAllDay"))

    def shown(moment):
        if moment is None:
            return ""
        # An all-day event is a date, not a moment; it floats with the
        # reader, so it is not converted.
        return moment.date().isoformat() if all_day else moment.astimezone(zone).isoformat()

    return {
        "event_id": str(event.get("id") or ""),
        "summary": str(event.get("subject") or "(no title)"),
        "start": shown(start), "end": shown(end), "all_day": all_day,
        "location": str((event.get("location") or {}).get("displayName") or ""),
        "organizer": _address(event.get("organizer")),
        "attendees": ", ".join(a for a in (_address(x) for x in event.get("attendees") or []) if a),
        "status": "cancelled" if event.get("isCancelled") else "confirmed",
        "link": str(event.get("webLink") or ""),
    }


class EventsTool(ToolBase):
    id = "events"

    @staticmethod
    def _zone(client, wanted: str):
        """The zone to reckon in: the one asked for, else the calendar's.
        (zone, name, problem) — problem is a result to return as is."""
        name = str(wanted or "").strip() or client.mailbox_timezone()
        zone, iana, problem = resolve_zone(name)
        if problem:
            return None, iana, {"error": problem}
        return zone, iana, None

    # -- reads -----------------------------------------------------------
    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            zone, name, problem = self._zone(client, call.inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            page = client.calendar_view(
                parse_when(call.inputs["time_min"], zone),
                parse_when(call.inputs["time_max"], zone),
                int(call.inputs.get("max_results") or 25),
                str(call.inputs.get("page_token") or ""))
        except GraphError as exc:
            return failure(exc)
        except ValueError as exc:
            return {"error": f"Bad date: {exc}"}, "error"
        result = {"timezone": name,
                  "events": [_row(e, zone) for e in page.get("value") or []
                             if not e.get("isCancelled")]}
        if page.get("@odata.nextLink"):
            result["next_page_token"] = str(page["@odata.nextLink"])
        return result, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            zone, _, problem = self._zone(client, call.inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            event = client.get_event(str(call.inputs["event_id"]))
        except GraphError as exc:
            return failure(exc)
        row = _row(event, zone)
        return {
            "event_id": row["event_id"], "summary": row["summary"],
            "start": row["start"], "end": row["end"], "status": row["status"],
            "link": row["link"],
            "responses": [{"email": _address(a),
                           "response": str((a.get("status") or {}).get("response") or "none")}
                          for a in event.get("attendees") or []],
        }, "success"

    def _busy(self, client, zone, time_min, time_max, attendees):
        """Busy periods of the account and each attendee, and who could
        not be checked. Graph answers per address; an error on one means
        that calendar is not readable by this account."""
        wanted = [client.email] + [a for a in attendees
                                   if a and a.lower() != client.email.lower()]
        answer = client.get_schedule(wanted, time_min, time_max)
        by_address = {str(item.get("scheduleId") or "").lower(): item
                      for item in answer.get("value") or []}
        busy, unchecked = [], []
        for email in wanted:
            item = by_address.get(email.lower())
            if item is None or item.get("error"):
                reason = str(((item or {}).get("error") or {}).get("message") or "not readable")
                unchecked.append({"email": email, "reason": reason})
                continue
            for entry in item.get("scheduleItems") or []:
                if str(entry.get("status") or "") not in BUSY:
                    continue
                start, end = from_graph(entry.get("start")), from_graph(entry.get("end"))
                if start and end:
                    busy.append({"email": email, "start": start.astimezone(zone).isoformat(),
                                 "end": end.astimezone(zone).isoformat()})
        return busy, unchecked

    async def availability(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            zone, _, problem = self._zone(client, call.inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            busy, unchecked = self._busy(
                client, zone, parse_when(call.inputs["time_min"], zone),
                parse_when(call.inputs["time_max"], zone),
                [str(a) for a in call.inputs.get("attendees") or []])
        except GraphError as exc:
            return failure(exc)
        except ValueError as exc:
            return {"error": f"Bad date: {exc}"}, "error"
        return {"busy": busy, "unchecked": unchecked}, "success"

    async def find_times(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        try:
            zone, name, problem = self._zone(client, inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            from_date = date.fromisoformat(str(inputs["from_date"]))
            to_date = date.fromisoformat(str(inputs["to_date"]))
            hours = inputs.get("working_hours") or {}
            working_start = parse_clock(hours.get("start") or "09:00")
            working_end = parse_clock(hours.get("end") or "17:00")
            avoid = [(parse_clock(a["start"]), parse_clock(a["end"]))
                     for a in inputs.get("avoid") or []]
            not_before = (parse_when(inputs["not_before"], zone)
                          if inputs.get("not_before") else datetime.now(timezone.utc))
        except GraphError as exc:
            return failure(exc)
        except (ValueError, KeyError) as exc:
            return {"error": f"Bad date or time: {exc}"}, "error"
        if to_date < from_date:
            return {"error": "to_date is before from_date."}, "error"
        if (to_date - from_date).days > 31:
            return {"error": "The window is longer than a month; narrow it."}, "error"
        if working_end <= working_start:
            return {"error": "working_hours end before they start."}, "error"

        attendees = [str(a).strip() for a in inputs.get("attendees") or [] if str(a).strip()]
        window_start = datetime.combine(from_date, working_start, zone)
        window_end = datetime.combine(to_date, working_end, zone)
        try:
            busy_rows, unchecked = self._busy(client, zone, window_start, window_end, attendees)
        except GraphError as exc:
            return failure(exc)
        if any(u["email"].lower() == client.email.lower() for u in unchecked):
            return {"error": "The account's own calendar could not be read; "
                             "nothing can be proposed."}, "error"

        busy = [(parse_when(b["start"], zone), parse_when(b["end"], zone)) for b in busy_rows]
        slots = find_slots(
            duration=timedelta(minutes=int(inputs["duration_minutes"])),
            from_date=from_date, to_date=to_date, zone=zone,
            working_start=working_start, working_end=working_end,
            avoid=avoid, buffer=timedelta(minutes=int(inputs.get("buffer_minutes") or 0)),
            busy=busy, not_before=not_before,
            limit=int(inputs.get("max_candidates") or 5))

        who = ", ".join([client.email] + [a for a in attendees if a.lower() != client.email.lower()])
        candidates = []
        for slot in slots:
            record = await call.resources.create_data("proposal", {
                "start": slot["start"], "end": slot["end"], "timezone": name,
                "attendees": who, "status": "offered"})
            candidates.append({"proposal_ref": record["resource_ref"], **slot})
        if unchecked:
            await call.progress(
                "Could not check: " + ", ".join(u["email"] for u in unchecked))
        return {"timezone": name, "candidates": candidates,
                "unchecked": unchecked}, "success"

    # -- writes ----------------------------------------------------------
    async def create(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        attendees = [str(a).strip() for a in inputs.get("attendees") or [] if str(a).strip()]
        proposal_ref = str(inputs.get("proposal_ref") or "")
        try:
            zone, name, problem = self._zone(client, inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            if proposal_ref:
                proposal = (await call.resources.read_data("proposal", proposal_ref)).get("keys") or {}
                if proposal.get("status") == "booked":
                    return {"error": "That proposal was already booked "
                                     f"(event {proposal.get('event_id')})."}, "error"
                start, end = parse_when(proposal["start"], zone), parse_when(proposal["end"], zone)
                if not attendees:
                    attendees = [a.strip() for a in str(proposal.get("attendees") or "").split(",")
                                 if a.strip() and a.strip().lower() != client.email.lower()]
            else:
                if not (inputs.get("start") and inputs.get("end")):
                    return {"error": "Give a proposal_ref, or both start and end."}, "error"
                start, end = parse_when(inputs["start"], zone), parse_when(inputs["end"], zone)
        except GraphError as exc:
            return failure(exc)
        except (ValueError, KeyError) as exc:
            return {"error": f"Bad date: {exc}"}, "error"
        if end <= start:
            return {"error": "The event ends before it starts."}, "error"

        event = {
            "subject": str(inputs["summary"]),
            "start": {"dateTime": utc_text(start), "timeZone": "UTC"},
            "end": {"dateTime": utc_text(end), "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": a}, "type": "required"}
                          for a in attendees],
        }
        if inputs.get("description"):
            event["body"] = {"contentType": "text", "content": str(inputs["description"])}
        if inputs.get("location"):
            event["location"] = {"displayName": str(inputs["location"])}
        await call.progress(f"Creating '{event['subject']}' at {start.astimezone(zone).isoformat()}")
        try:
            created = client.create_event(event)
        except GraphError as exc:
            return failure(exc)
        event_id = str(created.get("id") or "")
        if not event_id:
            return {"error": "Microsoft accepted the event but returned no id; "
                             "the outcome is unknown.", "kind": "unknown"}, "error"
        shown_start, shown_end = start.astimezone(zone).isoformat(), end.astimezone(zone).isoformat()
        booking = await call.resources.create_data("booking", {
            "event_id": event_id, "summary": event["subject"],
            "start": shown_start, "end": shown_end,
            "attendees": ", ".join(attendees), "status": "created",
            "link": str(created.get("webLink") or "")})
        if proposal_ref:
            await call.resources.update_data("proposal", proposal_ref,
                                             {"status": "booked", "event_id": event_id})
        return {"booking_ref": booking["resource_ref"], "event_id": event_id,
                "start": shown_start, "end": shown_end,
                "link": str(created.get("webLink") or ""),
                "invited": attendees}, "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        patch = {}
        if inputs.get("summary"):
            patch["subject"] = str(inputs["summary"])
        if inputs.get("description"):
            patch["body"] = {"contentType": "text", "content": str(inputs["description"])}
        if inputs.get("location"):
            patch["location"] = {"displayName": str(inputs["location"])}
        try:
            zone, _, problem = self._zone(client, inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            if inputs.get("start"):
                patch["start"] = {"dateTime": utc_text(parse_when(inputs["start"], zone)),
                                  "timeZone": "UTC"}
            if inputs.get("end"):
                patch["end"] = {"dateTime": utc_text(parse_when(inputs["end"], zone)),
                                "timeZone": "UTC"}
        except GraphError as exc:
            return failure(exc)
        except ValueError as exc:
            return {"error": f"Bad date: {exc}"}, "error"
        if not patch:
            return {"error": "Nothing to change: give a summary, start, end, "
                             "description or location."}, "error"
        try:
            updated = client.update_event(str(inputs["event_id"]), patch)
        except GraphError as exc:
            return failure(exc)
        row = _row(updated, zone)
        booking = await call.resources.create_data("booking", {
            "event_id": row["event_id"], "summary": row["summary"],
            "start": row["start"], "end": row["end"],
            "attendees": row["attendees"], "status": "updated", "link": row["link"]})
        return {"booking_ref": booking["resource_ref"], "event_id": row["event_id"],
                "start": row["start"], "end": row["end"], "link": row["link"]}, "success"

    async def cancel(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        event_id = str(call.inputs["event_id"])
        try:
            zone, _, problem = self._zone(client, call.inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            before = client.get_event(event_id)
            client.delete_event(event_id)
        except GraphError as exc:
            return failure(exc)
        row = _row(before, zone)
        booking = await call.resources.create_data("booking", {
            "event_id": event_id, "summary": row["summary"],
            "start": row["start"], "end": row["end"],
            "attendees": row["attendees"], "status": "cancelled", "link": row["link"]})
        return {"cancelled": True, "event_id": event_id,
                "booking_ref": booking["resource_ref"]}, "success"
