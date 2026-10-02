from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from decentai_sdk.base import ToolBase

from .account_tool import client_for, failure
from .google_api import GoogleError
from .slots import find_slots, parse_clock, parse_when


def _when(value):
    """Google's {dateTime} or {date} as one string, and whether all-day."""
    if not isinstance(value, dict):
        return "", False
    if value.get("dateTime"):
        return str(value["dateTime"]), False
    return str(value.get("date") or ""), True


def _attendees(event):
    return ", ".join(str(a.get("email") or "") for a in event.get("attendees") or [])


def _row(event):
    start, all_day = _when(event.get("start"))
    end, _ = _when(event.get("end"))
    return {
        "event_id": str(event.get("id") or ""),
        "summary": str(event.get("summary") or "(no title)"),
        "start": start, "end": end, "all_day": all_day,
        "location": str(event.get("location") or ""),
        "organizer": str((event.get("organizer") or {}).get("email") or ""),
        "attendees": _attendees(event),
        "status": str(event.get("status") or ""),
        "link": str(event.get("htmlLink") or ""),
    }


class EventsTool(ToolBase):
    id = "events"

    async def _zone(self, client, wanted: str):
        """The zone to reckon in: the one asked for, else the calendar's."""
        name = str(wanted or "").strip()
        if not name:
            name = str(client.primary().get("timeZone") or "UTC")
        try:
            return ZoneInfo(name), name, None
        except (ZoneInfoNotFoundError, ValueError):
            return None, name, {"error": f"'{name}' is not an IANA time zone "
                                         f"(try Asia/Dubai, Europe/London)."}

    # -- reads -----------------------------------------------------------
    async def list(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            zone, name, problem = await self._zone(client, "")
            if problem:
                return problem, "error"
            page = client.list_events(
                parse_when(call.inputs["time_min"], zone).isoformat(),
                parse_when(call.inputs["time_max"], zone).isoformat(),
                str(call.inputs.get("query") or ""),
                int(call.inputs.get("max_results") or 25),
                str(call.inputs.get("page_token") or ""))
        except GoogleError as exc:
            return failure(exc)
        except ValueError as exc:
            return {"error": f"Bad date: {exc}"}, "error"
        result = {"timezone": name,
                  "events": [_row(e) for e in page.get("items") or []]}
        if page.get("nextPageToken"):
            result["next_page_token"] = str(page["nextPageToken"])
        return result, "success"

    async def get(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            event = client.get_event(str(call.inputs["event_id"]))
        except GoogleError as exc:
            return failure(exc)
        row = _row(event)
        return {
            "event_id": row["event_id"], "summary": row["summary"],
            "start": row["start"], "end": row["end"], "status": row["status"],
            "link": row["link"],
            "responses": [{"email": str(a.get("email") or ""),
                           "response": str(a.get("responseStatus") or "needsAction")}
                          for a in event.get("attendees") or []],
        }, "success"

    async def _busy(self, client, time_min, time_max, attendees):
        """Busy periods of the account and each attendee, and who could
        not be checked. Google answers per calendar; an error entry means
        that calendar is not readable by this account."""
        wanted = [client.email] + [a for a in attendees if a and a != client.email]
        answer = client.free_busy(time_min.isoformat(), time_max.isoformat(), wanted)
        calendars = answer.get("calendars") or {}
        busy, unchecked = [], []
        for email in wanted:
            entry = calendars.get(email)
            if entry is None or entry.get("errors"):
                reasons = [str(e.get("reason") or "") for e in (entry or {}).get("errors") or []]
                unchecked.append({"email": email,
                                  "reason": ", ".join(r for r in reasons if r) or "not readable"})
                continue
            for period in entry.get("busy") or []:
                busy.append({"email": email, "start": str(period.get("start") or ""),
                             "end": str(period.get("end") or "")})
        # The account itself must be readable; if not, nothing can be proposed.
        return busy, unchecked

    async def availability(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        try:
            zone, _, problem = await self._zone(client, "")
            if problem:
                return problem, "error"
            busy, unchecked = await self._busy(
                client, parse_when(call.inputs["time_min"], zone),
                parse_when(call.inputs["time_max"], zone),
                [str(a) for a in call.inputs.get("attendees") or []])
        except GoogleError as exc:
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
            zone, name, problem = await self._zone(client, inputs.get("timezone") or "")
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
            busy_rows, unchecked = await self._busy(client, window_start, window_end, attendees)
        except GoogleError as exc:
            return failure(exc)
        if any(u["email"] == client.email for u in unchecked):
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

        who = ", ".join([client.email] + [a for a in attendees if a != client.email])
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
            zone, name, problem = await self._zone(client, inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            if proposal_ref:
                proposal = (await call.resources.read_data("proposal", proposal_ref)).get("keys") or {}
                if proposal.get("status") == "booked":
                    return {"error": "That proposal was already booked "
                                     f"(event {proposal.get('event_id')})."}, "error"
                start, end = parse_when(proposal["start"], zone), parse_when(proposal["end"], zone)
                name = str(proposal.get("timezone") or name)
                if not attendees:
                    attendees = [a.strip() for a in str(proposal.get("attendees") or "").split(",")
                                 if a.strip() and a.strip() != client.email]
            else:
                if not (inputs.get("start") and inputs.get("end")):
                    return {"error": "Give a proposal_ref, or both start and end."}, "error"
                start, end = parse_when(inputs["start"], zone), parse_when(inputs["end"], zone)
        except (ValueError, KeyError) as exc:
            return {"error": f"Bad date: {exc}"}, "error"
        if end <= start:
            return {"error": "The event ends before it starts."}, "error"

        event = {
            "summary": str(inputs["summary"]),
            "start": {"dateTime": start.isoformat(), "timeZone": name},
            "end": {"dateTime": end.isoformat(), "timeZone": name},
            "attendees": [{"email": a} for a in attendees],
        }
        if inputs.get("description"):
            event["description"] = str(inputs["description"])
        if inputs.get("location"):
            event["location"] = str(inputs["location"])
        await call.progress(f"Creating '{event['summary']}' at {start.isoformat()}")
        try:
            created = client.insert_event(event)
        except GoogleError as exc:
            return failure(exc)
        event_id = str(created.get("id") or "")
        if not event_id:
            return {"error": "Google accepted the event but returned no id; "
                             "the outcome is unknown.", "kind": "unknown"}, "error"
        booking = await call.resources.create_data("booking", {
            "event_id": event_id, "summary": event["summary"],
            "start": start.isoformat(), "end": end.isoformat(),
            "attendees": ", ".join(attendees), "status": "created",
            "link": str(created.get("htmlLink") or "")})
        if proposal_ref:
            await call.resources.update_data("proposal", proposal_ref,
                                             {"status": "booked", "event_id": event_id})
        return {"booking_ref": booking["resource_ref"], "event_id": event_id,
                "start": start.isoformat(), "end": end.isoformat(),
                "link": str(created.get("htmlLink") or ""),
                "invited": attendees}, "success"

    async def update(self, call):
        client, why = await client_for(call)
        if client is None:
            return {"error": why, "kind": "auth"}, "error"
        inputs = call.inputs
        patch = {}
        for field in ("summary", "description", "location"):
            if inputs.get(field):
                patch[field] = str(inputs[field])
        try:
            zone, name, problem = await self._zone(client, inputs.get("timezone") or "")
            if problem:
                return problem, "error"
            if inputs.get("start"):
                patch["start"] = {"dateTime": parse_when(inputs["start"], zone).isoformat(),
                                  "timeZone": name}
            if inputs.get("end"):
                patch["end"] = {"dateTime": parse_when(inputs["end"], zone).isoformat(),
                                "timeZone": name}
        except ValueError as exc:
            return {"error": f"Bad date: {exc}"}, "error"
        if not patch:
            return {"error": "Nothing to change: give a summary, start, end, "
                             "description or location."}, "error"
        try:
            updated = client.patch_event(str(inputs["event_id"]), patch)
        except GoogleError as exc:
            return failure(exc)
        row = _row(updated)
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
            before = client.get_event(event_id)
            client.delete_event(event_id)
        except GoogleError as exc:
            return failure(exc)
        row = _row(before)
        booking = await call.resources.create_data("booking", {
            "event_id": event_id, "summary": row["summary"],
            "start": row["start"], "end": row["end"],
            "attendees": row["attendees"], "status": "cancelled", "link": row["link"]})
        return {"cancelled": True, "event_id": event_id,
                "booking_ref": booking["resource_ref"]}, "success"
