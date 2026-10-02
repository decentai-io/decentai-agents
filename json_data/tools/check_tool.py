"""Checking a document against a JSON Schema, and saying exactly what
failed and where. The verdict is the validator's, never a judgement: a
file is valid only when nothing came back."""

import json

from decentai_sdk.base import ToolBase

from .jsondoc import load, preview, read_text


class CheckTool(ToolBase):
    id = "check"

    async def validate(self, call):
        try:
            doc = await load(call, "source", str(call.inputs["file_ref"]))
        except Exception as exc:
            return {"error": f"The file could not be read: {exc}"}, "error"
        if not doc.valid:
            return {"error": doc.problem}, "error"

        schema, problem = await self._schema(call)
        if problem:
            return {"error": problem}, "error"

        try:
            from jsonschema import Draft202012Validator
            from jsonschema.exceptions import SchemaError
        except ImportError as exc:                    # pragma: no cover
            return {"error": f"The validator is unavailable: {exc}"}, "error"

        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            return {"error": f"The schema itself is not valid: {exc.message}"}, "error"
        validator = Draft202012Validator(schema)

        cap = int(call.inputs.get("max_errors") or 50)
        each = bool(call.inputs.get("each_record"))
        errors, checked, failures, truncated = [], 0, 0, False

        if each:
            for index, record in enumerate(doc.records):
                checked += 1
                found = sorted(validator.iter_errors(record),
                               key=lambda e: list(e.absolute_path))
                if found:
                    failures += 1
                for error in found:
                    if len(errors) >= cap:
                        truncated = True
                        break
                    errors.append(self._error(error, index))
        else:
            checked = 1
            found = sorted(validator.iter_errors(doc.data),
                           key=lambda e: list(e.absolute_path))
            failures = 1 if found else 0
            for error in found:
                if len(errors) >= cap:
                    truncated = True
                    break
                errors.append(self._error(error, None))

        valid = failures == 0
        record = await call.resources.create_data("validation", {
            "file_ref": doc.file_ref, "filename": doc.filename,
            "valid": "yes" if valid else "no", "checked": checked,
            "failures": failures, "errors": {"errors": errors},
        })
        return {"validation_ref": record["resource_ref"],
                "file_ref": doc.file_ref, "valid": valid, "checked": checked,
                "failures": failures, "truncated": truncated,
                "errors": errors}, "success"

    # ------------------------------------------------------------------
    async def _schema(self, call):
        inline = call.inputs.get("schema")
        ref = str(call.inputs.get("schema_ref") or "")
        if isinstance(inline, dict) and inline:
            return inline, ""
        if not ref:
            return None, ("No schema was given: pass schema as an object, or "
                          "schema_ref naming an attached schema file.")
        try:
            _, _, text = await read_text(call, "source", ref)
        except Exception as exc:
            return None, f"The schema file could not be read: {exc}"
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            return None, f"The schema file is not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return None, "A JSON Schema must be an object."
        return parsed, ""

    @staticmethod
    def _error(error, index):
        where = "$"
        for part in error.absolute_path:
            where = (f"{where}[{part}]" if isinstance(part, int)
                     else (str(part) if where == "$" else f"{where}.{part}"))
        entry = {"path": where, "rule": str(error.validator),
                 "message": preview(error.message, 300)}
        if index is not None:
            entry["record"] = index
        return entry
