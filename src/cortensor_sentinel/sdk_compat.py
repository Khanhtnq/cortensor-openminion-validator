"""Runtime compatibility adapters for OpenMinion v0.1.20 SDK edge cases."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, TypeVar

from openminion import Agent, AgentOutputValidationError, Handoff, subagent
from openminion.api.handoff import DelegatedMemoryReadRequest
from openminion.modules.llm.providers.cortensor.config import (
    resolve_cortensor_runtime_config,
)
from openminion.services.health.probes import ProbeResult
from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)


def safe_tool_payload(value: Any) -> Any:
    """Recursively normalize BaseModel and dataclass values into JSON-safe structures."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return safe_tool_payload(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): safe_tool_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_tool_payload(item) for item in value]
    return value


def safe_tool_envelope(value: Any, *, summary: str | None = None) -> dict[str, Any]:
    """Wrap tool returns in ok=True Mapping so domain 'status' keys pass run_tool_spec."""
    normalized = safe_tool_payload(value)
    if isinstance(normalized, Mapping):
        envelope: dict[str, Any] = {"ok": True, **dict(normalized)}
    else:
        envelope = {"ok": True, "result": normalized}
    if summary:
        envelope.setdefault("summary", summary)
    return envelope


class _JsonSafeAgentTarget:
    """Adapter ensuring Handoff target runs serialize BaseModel outputs into dicts."""

    def __init__(self, target: Agent[Any, Any]) -> None:
        self._target = target
        self.name = target.name
        self.instructions = target.instructions

    def run(self, message: str) -> Any:
        result = self._target.run(message)
        if isinstance(result.output, BaseModel):
            result.output = result.output.model_dump(mode="json")  # type: ignore[assignment]
        else:
            result.output = safe_tool_payload(result.output)
        return result


def safe_handoff(
    target: Agent[Any, Any],
    *,
    name: str | None = None,
    description: str | None = None,
) -> Handoff:
    """Build a Handoff whose target output is guaranteed JSON-serializable."""
    wrapped = _JsonSafeAgentTarget(target)
    return Handoff(target=wrapped, name=name, description=description)  # type: ignore[arg-type]


def safe_subagent(
    parent: Agent[Any, Any],
    *,
    instructions: str | None = None,
    model: str | None = None,
    tools: list[Any] | None = None,
    forced_tools: list[str] | None = None,
    output_type: type | None = None,
    name: str | None = None,
    timeout_seconds: int | None = None,
    deadline_iso: str = "",
    memory: DelegatedMemoryReadRequest | None = None,
) -> Agent[Any, Any]:
    """Create a bounded subagent that inherits parent.agent_id and parent.model."""
    effective_model = model if model is not None else parent.model
    child = subagent(
        parent,
        instructions=instructions,
        model=effective_model,
        tools=tools,
        output_type=output_type,
        name=name,
        timeout_seconds=timeout_seconds,
        deadline_iso=deadline_iso,
        memory=memory,
    )
    if child.agent_id is None and parent.agent_id is not None:
        child.agent_id = parent.agent_id
    if forced_tools:
        child.forced_tools = list(forced_tools)
    return child


def iter_json_object_spans(text: str) -> Iterator[str]:
    """Yield all balanced JSON object spans from model text in appearance order."""
    decoder = json.JSONDecoder()
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "{":
            index += 1
            continue
        try:
            parsed, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(parsed, dict):
            yield text[index : index + end]
            index += end
        else:
            index += 1


def coerce_structured_output_safely(text: str, model_type: type[ModelT]) -> ModelT:
    """Validate direct JSON or scan all balanced JSON object spans against model_type."""
    try:
        return model_type.model_validate_json(text)
    except ValidationError as direct_exc:
        last_schema_exc: ValidationError = direct_exc
        candidates = list(iter_json_object_spans(text))
        for candidate in reversed(candidates):
            try:
                return model_type.model_validate_json(candidate)
            except ValidationError as candidate_exc:
                last_schema_exc = candidate_exc
        raise AgentOutputValidationError(
            f"Reply did not validate against {model_type.__name__}: {last_schema_exc}",
            raw_text=text,
            validation_error=last_schema_exc,
        ) from last_schema_exc


def _is_cortensor_completion_mode(*, api_mode: str, base_url: str) -> bool:
    normalized = (api_mode or "").strip().lower()
    if normalized in {"cortensor_completion", "openai_chat"}:
        return normalized == "cortensor_completion"
    return base_url.strip().rstrip("/").lower().endswith("/completions")


def _positive_unique_sessions(resolved_provider: Any) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for raw_item in [
        resolved_provider.session_id,
        *list(resolved_provider.session_ids),
        *list(resolved_provider.dedicated_session_ids),
        *list(resolved_provider.ephemeral_session_ids),
    ]:
        try:
            parsed = int(raw_item)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in seen:
            seen.add(parsed)
            ordered.append(parsed)
    return ordered


def probe_cortensor_session_runtime_aware(
    config: Any,
    *,
    env: Mapping[str, str] | None = None,
) -> ProbeResult | None:
    """Runtime-consistent Cortensor session probe honoring CORTENSOR_* env overrides."""
    resolved = resolve_cortensor_runtime_config(config, env=env)
    if not _is_cortensor_completion_mode(
        api_mode=resolved.api_mode,
        base_url=resolved.base_url,
    ):
        return None
    session_ids = _positive_unique_sessions(resolved)
    if not session_ids:
        return ProbeResult(
            id="provider.cortensor.session_id",
            status="fail",
            message=(
                "Cortensor completion mode requires at least one positive session id "
                "via config or CORTENSOR_SESSION_ID / CORTENSOR_SESSION_IDS."
            ),
            remediation=(
                "Configure `providers.cortensor.session_id` or export "
                "`CORTENSOR_SESSION_ID=<id>` before starting Router completion turns."
            ),
        )
    return ProbeResult(
        id="provider.cortensor.session_id",
        status="ok",
        message=(
            "Cortensor completion session ids are configured "
            f"({', '.join(str(item) for item in session_ids)})"
        ),
    )
