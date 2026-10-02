from decimal import Decimal

from decentai_sdk.base import ToolBase

from .common import (CATEGORIES, amounts_in, currencies_in, iso_date, keys_of, load,
                     money, normalize_merchant, out, parse_date, parse_json, receipt_row)

READ_SYSTEM = (
    "You read one receipt and answer ONLY with a JSON object: {\"merchant\": "
    "<the business name as printed>, \"date\": <the transaction date as "
    "printed>, \"currency\": <the ISO code shown or implied by a symbol, or "
    "\"\">, \"amount\": <the total paid, as printed, digits only>, "
    "\"category\": <one of meals, travel, accommodation, transport, office, "
    "other>, \"quote_amount\": <the line, copied verbatim, that shows the "
    "total>}. Copy what is printed; never guess a value the receipt does not "
    "show — use \"\" instead."
)


class ReceiptsTool(ToolBase):
    id = "receipts"

    async def read(self, call):
        claim_ref = str(call.inputs["claim_ref"])
        claim = keys_of(await call.resources.read_data("claim", claim_ref))
        if claim.get("status") not in ("draft", None, ""):
            return {"error": f"The claim is {claim.get('status')}; receipts go into a draft."}, "error"
        try:
            loaded = await load(call, "receipt_file", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"

        if loaded.kind in ("image", "unsupported") or not loaded.text:
            status = "manual" if loaded.kind in ("image", "pdf") else "needs_review"
            record = await call.resources.create_data("receipt", {
                "claim_ref": claim_ref, "file_ref": loaded.file_ref,
                "filename": loaded.filename, "category": "unknown",
                "status": status, "verified": "", "assumed": "",
                "note": loaded.problem})
            return {"receipt_ref": record["resource_ref"], "status": status,
                    "category": "unknown", "verified": [], "assumed": [],
                    "note": loaded.problem}, "success"

        prompt = f"RECEIPT ({loaded.filename}):\n{loaded.text[:12000]}\n\nAnswer with the JSON object."
        await call.progress(f"Reading {loaded.filename}")
        try:
            answer = await call.llm(prompt, system=READ_SYSTEM)
        except Exception as exc:
            return {"error": f"The model could not be asked: {exc}"}, "error"
        parsed = parse_json(answer)
        if not isinstance(parsed, dict):
            return {"error": "The model's answer was not a JSON object; try again."}, "error"

        verified, assumed, notes = [], [], []
        text = loaded.text
        flat = " ".join(text.split()).lower()

        merchant = str(parsed.get("merchant") or "").strip()[:120]
        if merchant and normalize_merchant(merchant) and normalize_merchant(merchant) in normalize_merchant(text):
            verified.append("merchant")
        elif merchant:
            assumed.append("merchant")
        else:
            notes.append("no merchant found")

        date_text = str(parsed.get("date") or "").strip()
        found = parse_date(date_text) if date_text else None
        if found is None and date_text:
            found = parse_date(text)          # the model paraphrased; try the receipt itself
        date_iso = found.isoformat() if found else ""
        if found and (date_text.lower() in flat or found.isoformat() in text or date_text == ""):
            verified.append("date")
        elif found:
            assumed.append("date")
        else:
            notes.append("date not readable" if date_text else "no date found")

        amount = money(parsed.get("amount"))
        on_receipt = amounts_in(text)
        if amount is not None and amount in on_receipt:
            verified.append("amount")
            if on_receipt and amount < max(on_receipt):
                notes.append(f"a larger figure ({out(max(on_receipt))}) appears on the receipt")
        elif amount is not None:
            assumed.append("amount")
        else:
            notes.append("no amount found")

        currency = str(parsed.get("currency") or "").strip().upper()[:3]
        shown = currencies_in(text)
        if currency and currency in shown:
            verified.append("currency")
        elif currency:
            assumed.append("currency")
        elif len(shown) == 1:
            currency = shown[0]
            verified.append("currency")
        else:
            notes.append("currency not shown")

        category = str(parsed.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            category = "other"
            assumed.append("category")
        else:
            assumed.append("category")         # always the model's judgement

        # A duplicate: same merchant, date and amount anywhere already recorded.
        duplicate_of = ""
        if merchant and date_iso and amount is not None:
            for other in await call.resources.list_data("receipt", {"date": date_iso}):
                row = receipt_row(other)
                if (row["status"] != "duplicate" and normalize_merchant(row["merchant"]) == normalize_merchant(merchant)
                        and row.get("amount") == out(amount)):
                    duplicate_of = row["receipt_ref"]
                    break

        needs_review = bool(notes) or any(f in assumed for f in ("merchant", "date", "amount", "currency"))
        status = "duplicate" if duplicate_of else ("needs_review" if needs_review else "extracted")
        fields = {"claim_ref": claim_ref, "file_ref": loaded.file_ref, "filename": loaded.filename,
                  "merchant": merchant, "date": date_iso, "currency": currency,
                  "category": category, "status": status,
                  "verified": ", ".join(verified), "assumed": ", ".join(assumed),
                  "duplicate_of": duplicate_of, "note": "; ".join(notes)[:300]}
        if amount is not None:
            fields["amount"] = out(amount)
        record = await call.resources.create_data("receipt", fields)
        result = {"receipt_ref": record["resource_ref"], "status": status, "merchant": merchant,
                  "date": date_iso, "currency": currency, "category": category,
                  "verified": verified, "assumed": assumed}
        if amount is not None:
            result["amount"] = out(amount)
        if duplicate_of:
            result["duplicate_of"] = duplicate_of
        if notes:
            result["note"] = "; ".join(notes)
        return result, "success"

    async def confirm(self, call):
        ref = str(call.inputs["receipt_ref"])
        current = receipt_row(await call.resources.read_data("receipt", ref))
        changes = {}
        if call.inputs.get("date"):
            found = iso_date(call.inputs["date"])
            if found is None:
                return {"error": "date must be YYYY-MM-DD."}, "error"
            changes["date"] = found.isoformat()
        for name in ("merchant", "currency", "category", "note"):
            if call.inputs.get(name):
                changes[name] = str(call.inputs[name]).strip()
        if "currency" in changes:
            changes["currency"] = changes["currency"].upper()
        if call.inputs.get("amount") is not None:
            changes["amount"] = out(money(call.inputs["amount"]) or Decimal("0"))
        merged = {**current, **changes}
        missing = [f for f in ("merchant", "date", "currency") if not merged.get(f)] + \
                  (["amount"] if merged.get("amount") is None else [])
        if missing:
            return {"error": f"A confirmed receipt needs {', '.join(missing)}."}, "error"
        if merged.get("category") in ("", "unknown"):
            return {"error": "A confirmed receipt needs a category."}, "error"
        changes.update({"status": "confirmed",
                        "verified": "merchant, date, currency, amount, category (confirmed by a person)",
                        "assumed": ""})
        await call.resources.update_data("receipt", ref, changes)
        after = receipt_row(await call.resources.read_data("receipt", ref))
        return {k: after[k] for k in ("receipt_ref", "status", "merchant", "date", "currency",
                                      "amount", "category")}, "success"
