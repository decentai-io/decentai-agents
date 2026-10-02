from decentai_sdk.base import ToolBase

from .forms import FormError, is_required, load_forms


class FormsTool(ToolBase):
    id = "forms"

    async def list(self, call):
        try:
            forms = load_forms()
        except FormError as exc:
            return {"error": f"A form this agent carries is broken: {exc}",
                    "kind": "form"}, "error"
        return {"forms": [
            {"id": form["id"], "title": form["title"],
             "description": str(form.get("description") or "").strip(),
             "steps": [{"field": s["field"], "label": s["label"], "kind": s["kind"],
                        "required": is_required(s)} for s in form["steps"]]}
            for form in forms.values()]}, "success"
