"""Agreements: read from a document by the model and checked by
code, recorded with their clauses, and moved between states only
with evidence."""

import json

from decentai_sdk.base import ToolBase

from .agreement_rows import agreement_row, close_deadlines, deadline_row, derive, keys_of, sync_deadlines
from .renewal_dates import iso, notice_deadline, notice_period, resolve, today

DISCLAIMER = "A reading of the text as supplied, not legal advice."

EXTRACT_SYSTEM = (
    "You read an agreement, policy, licence or other document that renews or expires, and report its dates and terms. Answer ONLY "
    "with a JSON object: {\"counterparty\": <the other party's name as written, or "
    "\"\">, \"kind\": one of subscription|agreement|insurance|warranty|maintenance|lease|licence|identity|other, "
    "\"start_text\": <the start or effective date exactly as written, or \"\">, "
    "\"renewal_text\": <the renewal, expiry or end date exactly as written, or \"\">, "
    "\"term_text\": <the term or duration exactly as written, or \"\">, "
    "\"notice_text\": <the notice period for cancellation or non-renewal exactly as "
    "written, or \"\">, \"auto_renews\": \"yes\" | \"no\" | \"unknown\", "
    "\"clauses\": [{\"topic\": renewal|notice|term|price|auto_renewal|other, "
    "\"quote\": <the sentence or clause copied verbatim>, \"reading\": <one plain "
    "sentence on what it means>}]}. Copy text exactly; never complete a date the "
    "text does not give; never decide whether days are calendar or business days "
    "unless the text says so."
)


class AgreementsTool(ToolBase):
    id = "agreements"

    async def extract(self, call):
        text = str(call.inputs["text"])
        reference = str(call.inputs.get("reference") or "")
        await call.progress("Reading the agreement for its dates and terms")
        try:
            answer = await call.llm(f"AGREEMENT TEXT:\n{text}\n\nReport its dates and terms as JSON.",
                                    system=EXTRACT_SYSTEM)
        except Exception as exc:
            return {"error": f"The model could not be asked: {exc}"}, "error"
        parsed = self._json(answer)
        if not isinstance(parsed, dict):
            return {"error": "The model's answer was not a JSON object; try again."}, "error"

        flat = " ".join(text.split()).lower()
        clauses, ambiguities, asks = [], [], []
        for item in (parsed.get("clauses") or [])[:20]:
            if not isinstance(item, dict):
                continue
            quote = " ".join(str(item.get("quote") or "").split())
            if not quote:
                continue
            topic = str(item.get("topic") or "other")
            if topic not in ("renewal", "notice", "term", "price", "auto_renewal", "other"):
                topic = "other"
            verified = len(quote) >= 8 and quote.lower() in flat
            ambiguous = False
            if topic == "notice":
                _, basis = notice_period(quote)
                ambiguous = basis not in ("calendar", "business")
            if topic == "renewal":
                _, how = resolve(quote)
                ambiguous = how != "iso" and how != "written"
            clauses.append({"topic": topic, "quote": quote[:1000],
                            "reading": str(item.get("reading") or "")[:500],
                            "verified": verified, "ambiguous": ambiguous})

        proposal = {"counterparty": str(parsed.get("counterparty") or "")[:200],
                    "kind": str(parsed.get("kind") or "other"),
                    "start_text": str(parsed.get("start_text") or "")[:200],
                    "renewal_text": str(parsed.get("renewal_text") or "")[:200],
                    "term_text": str(parsed.get("term_text") or "")[:200],
                    "notice_text": str(parsed.get("notice_text") or "")[:200],
                    "auto_renews": str(parsed.get("auto_renews") or "unknown")}
        if proposal["kind"] not in ("subscription", "agreement", "insurance", "warranty", "maintenance", "lease", "licence", "identity", "other"):
            proposal["kind"] = "other"
        if proposal["auto_renews"] not in ("yes", "no"):
            proposal["auto_renews"] = "unknown"
            asks.append("whether the agreement renews automatically (the text does not say)")
        if not proposal["counterparty"] or proposal["counterparty"].lower() not in flat:
            proposal["counterparty"] = ""
            asks.append("the counterparty's name (not found in the text as reported)")

        start, _ = resolve(proposal["start_text"])
        proposal["start_date"] = start.isoformat() if start else ""
        renewal, how = resolve(proposal["renewal_text"])
        proposal["renewal_date"] = renewal.isoformat() if renewal else ""
        if renewal is None:
            if how == "month":
                ambiguities.append(f"the renewal date is given as a month only: '{proposal['renewal_text']}'")
            asks.append("the exact renewal date (YYYY-MM-DD)")

        days, basis = notice_period(proposal["notice_text"])
        proposal["notice_basis"] = basis if basis in ("calendar", "business") else "unknown"
        if days is not None:
            proposal["notice_days"] = days
            if basis == "unknown":
                ambiguities.append(f"the notice period '{proposal['notice_text']}' does not say "
                                   "whether the days are calendar or business days")
                asks.append(f"whether the {days} days' notice are calendar or business days")
        else:
            if basis == "months":
                ambiguities.append(f"the notice period '{proposal['notice_text']}' is in months, "
                                   "which is not a fixed number of days")
            asks.append("the notice period in days" if proposal["notice_text"] else
                        "the notice period (the text gives none)")
        deadline, _ = notice_deadline(renewal, days, proposal["notice_basis"])
        proposal["notice_deadline"] = deadline.isoformat() if deadline else ""
        if reference:
            proposal["reference"] = reference
        return {"proposal": proposal, "clauses": clauses, "ambiguities": ambiguities,
                "asks": asks, "disclaimer": DISCLAIMER}, "success"

    async def create(self, call):
        inputs = call.inputs
        for name in ("start_date", "renewal_date"):
            if inputs.get(name) and iso(inputs[name]) is None:
                return {"error": f"{name} must be YYYY-MM-DD."}, "error"
        fields = {"name": str(inputs["name"]).strip(), "counterparty": str(inputs["counterparty"]).strip(),
                  "kind": str(inputs["kind"]), "owner": str(inputs.get("owner") or ""),
                  "reference": str(inputs.get("reference") or ""),
                  "start_date": str(inputs.get("start_date") or ""),
                  "renewal_date": str(inputs.get("renewal_date") or ""),
                  "notice_basis": str(inputs.get("notice_basis") or "unknown"),
                  "auto_renews": str(inputs.get("auto_renews") or "unknown"),
                  "currency": str(inputs.get("currency") or "USD").upper(),
                  "status": "active", "decision": "undecided", "note": str(inputs.get("note") or "")}
        if inputs.get("notice_days") is not None:
            fields["notice_days"] = int(inputs["notice_days"])
        if inputs.get("value") is not None:
            fields["value"] = float(inputs["value"])
        asks = derive(fields)
        record = await call.resources.create_data("agreement", fields)
        ref = record["resource_ref"]
        for clause in inputs.get("clauses") or []:
            await call.resources.create_data("clause", {
                "agreement_ref": ref, "topic": str(clause["topic"]), "quote": str(clause["quote"]).strip(),
                "reference": str(clause.get("reference") or fields["reference"]),
                "reading": str(clause.get("reading") or ""),
                "ambiguous": "yes" if clause.get("ambiguous") else "no",
                "verified": "yes" if clause.get("verified") else "no"})
        deadlines = await sync_deadlines(call, ref, fields)
        return {"agreement_ref": ref, "name": fields["name"], "status": "active",
                "renewal_date": fields["renewal_date"], "notice_deadline": fields["notice_deadline"],
                "notice_basis": fields["notice_basis"], "deadlines": deadlines, "asks": asks}, "success"

    async def get(self, call):
        ref = str(call.inputs["agreement_ref"])
        row = agreement_row(await call.resources.read_data("agreement", ref))
        row = {k: v for k, v in row.items() if v is not None}
        row["clauses"] = [{"clause_ref": r["resource_ref"], **{k: str(keys_of(r).get(k) or "") for k in
                          ("topic", "quote", "reference", "reading", "ambiguous", "verified")}}
                          for r in await call.resources.list_data("clause", {"agreement_ref": ref})]
        deadlines = [deadline_row(r) for r in await call.resources.list_data("deadline", {"agreement_ref": ref})]
        deadlines.sort(key=lambda d: (d["status"] != "open", d["date"]))
        row["deadlines"] = [{k: d[k] for k in ("deadline_ref", "kind", "title", "date", "basis", "status", "done_on", "evidence")}
                            for d in deadlines]
        row["decisions"] = [{"decision_ref": r["resource_ref"], **{k: str(keys_of(r).get(k) or "") for k in
                            ("decision", "by", "decided_on", "note")}}
                            for r in await call.resources.list_data("decision", {"agreement_ref": ref})]
        return row, "success"

    async def update(self, call):
        inputs = call.inputs
        ref = str(inputs["agreement_ref"])
        current = agreement_row(await call.resources.read_data("agreement", ref))
        if current["status"] != "active":
            return {"error": f"The agreement is {current['status']}; only active agreements change."}, "error"
        for name in ("start_date", "renewal_date"):
            if inputs.get(name) and iso(inputs[name]) is None:
                return {"error": f"{name} must be YYYY-MM-DD."}, "error"
        fields = {k: current[k] for k in ("name", "owner", "start_date", "renewal_date", "notice_basis",
                                          "auto_renews", "note")}
        fields["notice_days"] = current["notice_days"]
        for name in ("owner", "start_date", "renewal_date", "notice_basis", "auto_renews", "note"):
            if inputs.get(name) is not None:
                fields[name] = str(inputs[name])
        if inputs.get("notice_days") is not None:
            fields["notice_days"] = int(inputs["notice_days"])
        if inputs.get("value") is not None:
            fields["value"] = float(inputs["value"])
        asks = derive(fields)
        if fields["notice_days"] is None:
            fields.pop("notice_days")
        await call.resources.update_data("agreement", ref, fields)
        await sync_deadlines(call, ref, fields)
        return {"agreement_ref": ref, "status": "active", "renewal_date": fields["renewal_date"],
                "notice_deadline": fields["notice_deadline"], "notice_basis": fields["notice_basis"],
                "asks": asks}, "success"

    async def set_status(self, call):
        inputs = call.inputs
        ref = str(inputs["agreement_ref"])
        status = str(inputs["status"])
        current = agreement_row(await call.resources.read_data("agreement", ref))
        if current["status"] != "active":
            return {"error": f"The agreement is already {current['status']}."}, "error"
        evidence = str(inputs.get("evidence") or "").strip()
        by = str(inputs.get("by") or "").strip()
        if status in ("renewed", "cancelled") and (not evidence or not by):
            return {"error": f"Marking an agreement {status} needs the confirmation's reference as "
                             "evidence and who confirmed it. A reminder, a completed notice deadline "
                             f"or a recorded decision does not make it {status}."}, "error"
        fields = {"status": status, "evidence": evidence, "decision_by": by or current["decision_by"]}
        result = {"agreement_ref": ref, "status": status}
        if status == "renewed":
            new_date = iso(inputs.get("renewal_date"))
            if new_date is None:
                return {"error": "renewed needs the new renewal_date (YYYY-MM-DD)."}, "error"
            if current["renewal_date"] and new_date <= iso(current["renewal_date"]):
                return {"error": "The new renewal date must be after the current one."}, "error"
            merged = {**current, "renewal_date": new_date.isoformat(), "status": "active", "decision": "undecided"}
            derive(merged)
            next_fields = {k: merged[k] for k in ("renewal_date", "notice_deadline", "status", "decision")}
            await call.resources.update_data("agreement", ref, {**fields, **next_fields, "status": "active",
                                                                "note": f"Renewed ({evidence}). {current['note']}".strip()})
            await sync_deadlines(call, ref, merged)
            result.update({"status": "active", "renewal_date": merged["renewal_date"],
                           "notice_deadline": merged["notice_deadline"], "deadlines_closed": 0})
            return result, "success"
        await call.resources.update_data("agreement", ref, fields)
        result["deadlines_closed"] = await close_deadlines(call, ref)
        return result, "success"

    @staticmethod
    def _json(answer):
        text = str(answer or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
