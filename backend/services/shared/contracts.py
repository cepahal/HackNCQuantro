"""Runtime validation at the existing JSON-native function boundaries."""
from typing import Any, Literal, TypeVar, overload

from pydantic import BaseModel

from .errors import Stage
from .types import parse_stage_output

ModelT = TypeVar("ModelT", bound=BaseModel)


def contract_input(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


@overload
def validate_contract_result(model: type[ModelT], value: Any, stage: Stage, *, json_native: Literal[False] = False) -> ModelT: ...


@overload
def validate_contract_result(model: type[ModelT], value: Any, stage: Stage, *, json_native: Literal[True]) -> dict[str, Any]: ...


@overload
def validate_contract_result(model: type[ModelT], value: Any, stage: Stage, *, json_native: bool) -> ModelT | dict[str, Any]: ...


def validate_contract_result(model: type[ModelT], value: Any, stage: Stage, *, json_native: bool = False) -> ModelT | dict[str, Any]:
    # Revalidate instances too: a caller may have mutated a model after creation.
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    result = parse_stage_output(model, value, stage)
    if not json_native:
        return result
    payload = result.model_dump(mode="json")
    if isinstance(value, dict):
        # Preserve the existing storage/downstream object handoff after validation.
        value.clear()
        value.update(payload)
        return value
    return payload
