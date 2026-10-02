from decentai_sdk.base import ToolBase

from .common import keys_of, load, parse_json

RULE_NAMES = ("meal_max_per_day", "hotel_max_per_night", "taxi_max_per_trip",
              "receipt_required_above", "claim_within_days", "alcohol_allowed")

LOAD_SYSTEM = (
    "You read a company expense policy and answer ONLY with a JSON object "
    "keyed by rule name. Rules: meal_max_per_day (a number, the most that "
    "may be claimed for meals per person per day), hotel_max_per_night (a "
    "number), taxi_max_per_trip (a number), receipt_required_above (a "
    "number: receipts are required for amounts above it), claim_within_days "
    "(a number of days within which a claim must be made), alcohol_allowed "
    "(yes or no). For each rule the policy states: {\"value\": <as stated, "
    "digits only for numbers>, \"quote\": <the sentence, copied verbatim>}. "
    "For a rule the policy does not state: {\"found\": false}. Never infer a "
    "number the policy does not give."
)


class PoliciesTool(ToolBase):
    id = "policies"

    async def _set(self, call, name, value, quote="", source=""):
        existing = [r for r in await call.resources.list_data("rule", {"name": name})]
        fields = {"name": name, "value": str(value), "quote": quote[:300], "source": source}
        if existing:
            ref = existing[-1]["resource_ref"]
            await call.resources.update_data("rule", ref, fields)
            return ref
        return (await call.resources.create_data("rule", fields))["resource_ref"]

    async def load(self, call):
        try:
            loaded = await load(call, "policy_file", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The policy could not be read: {exc}"}, "error"
        if not loaded.text:
            return {"error": loaded.problem or "The policy has no readable text."}, "error"
        prompt = f"POLICY ({loaded.filename}):\n{loaded.text[:30000]}\n\nAnswer with the JSON object."
        await call.progress(f"Reading {loaded.filename} for the known rules")
        try:
            answer = await call.llm(prompt, system=LOAD_SYSTEM)
        except Exception as exc:
            return {"error": f"The model could not be asked: {exc}"}, "error"
        parsed = parse_json(answer)
        if not isinstance(parsed, dict):
            return {"error": "The model's answer was not a JSON object; try again."}, "error"
        flat = " ".join(loaded.text.split()).lower()
        rules, not_found, unverified = [], [], []
        for name in RULE_NAMES:
            item = parsed.get(name)
            if not isinstance(item, dict) or item.get("found") is False or item.get("value") in (None, ""):
                not_found.append(name)
                continue
            value = str(item.get("value")).strip()
            quote = " ".join(str(item.get("quote") or "").split())
            verified = len(quote) >= 8 and quote.lower() in flat
            if not verified:
                unverified.append(name)
            ref = await self._set(call, name, value, quote, loaded.file_ref)
            rules.append({"rule_ref": ref, "name": name, "value": value, "quote": quote,
                          "verified": verified})
        return {"rules": rules, "not_found": not_found, "unverified": unverified}, "success"

    async def set_rule(self, call):
        name = str(call.inputs["name"])
        value = str(call.inputs["value"]).strip()
        ref = await self._set(call, name, value, quote="set by a person")
        return {"rule_ref": ref, "name": name, "value": value}, "success"

    async def get(self, call):
        rows = []
        for record in await call.resources.list_data("rule", {}):
            keys = keys_of(record)
            rows.append({"rule_ref": record["resource_ref"], "name": str(keys.get("name") or ""),
                         "value": str(keys.get("value") or ""), "quote": str(keys.get("quote") or "")})
        rows.sort(key=lambda r: r["name"])
        return {"rules": rows}, "success"
