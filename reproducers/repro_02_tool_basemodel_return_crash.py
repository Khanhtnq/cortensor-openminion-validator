"""Repro 02: @tool returning BaseModel stringifies as repr and crashes ToolExecutionBatch."""

from __future__ import annotations

from openminion import tool
from openminion.modules.tool.registry import ToolExecutionBatch
from openminion.modules.tool.runtime.registry_toolspec import _tool_result_from_payload
from pydantic import BaseModel


class DiffSummary(BaseModel):
    severity: str
    files: int


@tool(min_scope="READ_ONLY", idempotent=True)
def summarize_pr(pr_number: int) -> DiffSummary:
    """Return typed summary for a pull request."""
    return DiffSummary(severity="medium", files=pr_number)


def main() -> int:
    decl = summarize_pr.tool_decl
    raw_output = decl.handler({"pr_number": 42})
    result = _tool_result_from_payload(tool_name=decl.name, payload=raw_output)
    print(f"CONTENT:  {result.content}")
    assert "severity='medium'" in result.content

    print("EXPECTED: ToolExecutionBatch.to_metadata_payload() serializes BaseModel result cleanly.")
    try:
        ToolExecutionBatch(results=[result]).to_metadata_payload()
    except TypeError as exc:
        print(f"ACTUAL:   TypeError: {exc}")
        return 0
    raise AssertionError("Expected TypeError from raw BaseModel in ToolExecutionResult.data")


if __name__ == "__main__":
    raise SystemExit(main())
