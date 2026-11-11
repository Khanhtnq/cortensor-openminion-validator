"""Repro 04: subagent(parent, ...) drops parent.agent_id and parent.model when omitted."""

from __future__ import annotations

from typing import Any

from openminion.api.agent import Agent
from openminion.api.handoff import subagent


class _CapturingRuntime:
    def __init__(self) -> None:
        self.last_payload: dict[str, Any] = {}

    def run_turn(self, *, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.last_payload = dict(payload)
        return {"body": "reviewed", "session_id": payload["session_id"]}

    def close(self) -> None:
        return None


def main() -> int:
    runtime = _CapturingRuntime()
    parent = Agent(
        name="coordinator",
        agent_id="cortensor-reviewer",
        model="qwen2.5:7b-instruct",
        runtime=runtime,  # type: ignore[arg-type]
    )
    child = subagent(parent, name="diff-auditor", instructions="Audit diff.")
    child.run("Check PR #12")

    print(
        "EXPECTED: child.agent_id == 'cortensor-reviewer' and child.model == 'qwen2.5:7b-instruct'"
    )
    print(
        f"ACTUAL:   child.agent_id={child.agent_id!r}, child.model={child.model!r}, "
        f"payload.agent_id={runtime.last_payload.get('agent_id')!r}"
    )
    assert child.agent_id is None and child.model is None
    assert "agent_id" not in runtime.last_payload
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
