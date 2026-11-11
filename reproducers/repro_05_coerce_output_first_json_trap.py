"""Repro 05: Agent._coerce_output fails when prose has an inline JSON object before final JSON."""

from __future__ import annotations

from typing import Any

from openminion import Agent, AgentOutputValidationError
from pydantic import BaseModel


class GateVerdict(BaseModel):
    decision: str
    summary: str


class _ProseRuntime:
    def run_turn(self, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        body = (
            'Inspected diff stats {"files": 2}. '
            'Final verdict: {"decision": "approve", "summary": "all checks clean"}'
        )
        return {"body": body, "session_id": payload["session_id"]}

    def close(self) -> None:
        return None


def main() -> int:
    agent = Agent(output_type=GateVerdict, runtime=_ProseRuntime())  # type: ignore[arg-type]
    print("EXPECTED: Agent.run() extracts the valid GateVerdict JSON object from prose.")
    try:
        agent.run("Evaluate PR")
    except AgentOutputValidationError as exc:
        first_line = str(exc).splitlines()[0]
        print(f"ACTUAL:   AgentOutputValidationError: {first_line}")
        return 0
    raise AssertionError("Expected AgentOutputValidationError from first-span-only extraction")


if __name__ == "__main__":
    raise SystemExit(main())
