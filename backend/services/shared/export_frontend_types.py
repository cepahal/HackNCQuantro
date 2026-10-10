"""Generate frontend wire types from the canonical Pydantic serialization schemas."""
import inspect
import json
import sys
from pathlib import Path

from . import types
from .errors import ErrorCode, Stage


def render(schema, name=None):
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
            # TranscriptSentence's wrap serializer omits empty word timings.
            return "{\n" + "".join(
                f"  {key}{'?' if name == 'TranscriptSentence' and key == 'words' else ''}: {render(value)};\n"
                for key, value in schema["properties"].items()
            ) + "}"
        return "Record<string, unknown>"
    if kind == "array":
        return f"Array<{render(schema['items'])}>"
    return {"string": "string", "integer": "number", "number": "number", "boolean": "boolean", "null": "null"}.get(kind, "unknown")


def generate():
    definitions = {}
    for _, model in inspect.getmembers(types, inspect.isclass):
        if not issubclass(model, types.ContractModel) or model is types.ContractModel:
            continue
        schema = model.model_json_schema(mode="serialization")
        definitions.update(schema.pop("$defs", {}))
        definitions[model.__name__] = schema
    error_types = "\n".join(
        f"export type {enum.__name__} = {' | '.join(json.dumps(item.value) for item in enum)};"
        for enum in (ErrorCode, Stage)
    )
    return (
        "// Generated from backend/services/shared/types.py and errors.py; do not edit by hand.\n"
        "// Nullable fields are always serialized; empty TranscriptSentence.words is omitted.\n"
        + error_types + "\n"
        + "export type QuantroError = { code: ErrorCode; stage: Stage; message: string; retryable: boolean };\n"
        + "export type QuantroErrorEnvelope = { error: QuantroError };\n"
        + "export type AssessmentConfidence = ConfidenceObject;\n\n"
        + "\n".join(f"export type {name} = {render(schema, name)};\n" for name, schema in sorted(definitions.items()))
    )


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[3] / "frontend" / "lib" / "api-types.ts"
    generated = generate()
    if "--check" in sys.argv:
        if target.read_text(encoding="utf-8") != generated:
            raise SystemExit("frontend/lib/api-types.ts differs from the canonical backend types. Regenerate it.")
        print("Frontend wire types match the backend.")
    else:
        target.write_text(generated, encoding="utf-8")
        print("Generated frontend/lib/api-types.ts")
