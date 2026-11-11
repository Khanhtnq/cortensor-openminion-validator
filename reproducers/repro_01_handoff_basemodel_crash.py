"""Repro 01: Handoff targeting Agent(output_type=BaseModel) crashes JSON serialization."""

from __future__ import annotations

from typing import Any

from openminion.api.agent import Agent
from openminion.api.handoff import Handoff, build_delegate_tool
from openminion.modules.tool.registry import ToolExecutionBatch
from openminion.modules.tool.runtime.registry_toolspec import _tool_result_from_payload
from pydantic import BaseModel


class AuditVerdict(BaseModel):
    severity: str
    summary: str


class _StubRuntime:
    def run_turn(self, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {
            "body": '{"severity":"high","summary":"unchecked call"}',
            "session_id": payload["session_id"],
        }

    def close(self) -> None:
        return None


def main() -> int:
    specialist = Agent(name="auditor", output_type=AuditVerdict, runtime=_StubRuntime())  # type: ignore[arg-type]
    decl = build_delegate_tool(Handoff(target=specialist))
    raw_payload = decl.handler(decl.args_model(message="audit diff"))
    result = _tool_result_from_payload(tool_name=decl.name, payload=raw_payload)
    batch = ToolExecutionBatch(results=[result])

    print("EXPECTED: ToolExecutionBatch.to_metadata_payload() returns valid JSON string.")
    try:
        batch.to_metadata_payload()
    except TypeError as exc:
        print(f"ACTUAL:   TypeError: {exc}")
        return 0
    raise AssertionError("Expected TypeError from unserialized BaseModel in Handoff data['output']")


if __name__ == "__main__":
    raise SystemExit(main())
