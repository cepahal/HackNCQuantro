"""Generate frontend wire types from the canonical Pydantic serialization schemas."""
import json
from pathlib import Path

from .types import AnalysisResult, DisplayModel, ReportV2


def render(schema):
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(render(item) for item in schema["anyOf"])
    kind = schema.get("type")
    if kind == "object":
        if "properties" in schema:
            return "{\n" + "".join(f"  {key}: {render(value)};\n" for key, value in schema["properties"].items()) + "}"
        return "Record<string, unknown>"
    if kind == "array":
        return f"Array<{render(schema['items'])}>"
    return {"string": "string", "integer": "number", "number": "number", "boolean": "boolean", "null": "null"}.get(kind, "unknown")


def generate():
    definitions = {}
    for model in (ReportV2, AnalysisResult, DisplayModel):
        schema = model.model_json_schema(mode="serialization")
        definitions.update(schema.pop("$defs", {}))
        definitions[model.__name__] = schema
    return "// Generated from backend/services/shared/types.py; do not edit by hand.\n" + "\n".join(
        f"export type {name} = {render(schema)};\n" for name, schema in sorted(definitions.items())
    )


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[3] / "frontend" / "lib" / "contracts.ts"
    target.write_text(generate(), encoding="utf-8")
    print("Generated frontend/lib/contracts.ts")
