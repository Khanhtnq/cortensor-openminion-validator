"""Repro 03: _validated_tool_arguments overwrites explicit None with non-None default."""

from __future__ import annotations

from openminion import tool
from openminion.modules.tool.base import ToolExecutionContext
from openminion.modules.tool.runtime.registry_toolspec import _validated_tool_arguments


@tool(min_scope="READ_ONLY", idempotent=True)
def list_pull_requests(repo: str, state: str | None = "open") -> str:
    """Return state filter seen by the tool body."""
    return f"repo={repo} state={state!r}"


def main() -> int:
    decl = list_pull_requests.tool_decl
    validated = _validated_tool_arguments(
        tool_name=decl.name,
        args_model=decl.args_model,
        arguments={"repo": "cortensor/openminion", "state": None},
        context=ToolExecutionContext(channel="console", target="cli", session_id="repro-03"),
    )
    assert isinstance(validated, dict)
    observed = decl.handler(validated)
    print("EXPECTED: explicit state=None preserved -> repo=cortensor/openminion state=None")
    print(f"ACTUAL:   {observed} (validated dict={validated})")
    assert observed == "repo=cortensor/openminion state='open'"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
