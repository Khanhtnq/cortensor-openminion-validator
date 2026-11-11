"""Reproducible verification of all 9 OpenMinion building blocks and 6 v0.1.20 SDK findings."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from openminion import (
    Agent,
    AgentOutputValidationError,
    Handoff,
    MemoryBundle,
    OpenMinionConfig,
    subagent,
    tool,
)
from openminion.api.handoff import DelegatedMemoryReadRequest, build_delegate_tool
from openminion.modules.brain.loop.tools.messages import action_result_to_tool_message
from openminion.modules.identity import load_identity_bundle
from openminion.modules.llm.providers.cortensor.adapter import CortensorProvider
from openminion.modules.llm.providers.cortensor.config import (
    resolve_cortensor_runtime_config,
)
from openminion.modules.memory.portability import (
    export_bundle,
    import_bundle,
    load_bundle,
    save_bundle,
)
from openminion.modules.policy.models import PolicyConfig
from openminion.modules.policy.runtime.service import PolicyCtl
from openminion.modules.tool.base import ToolExecutionContext
from openminion.modules.tool.registry import ToolExecutionBatch, ToolRegistry
from openminion.modules.tool.runtime.registry_toolspec import (
    _tool_result_from_payload,
    _validated_tool_arguments,
)
from openminion.services.health.probes import probe_provider_session
from pydantic import BaseModel
from sophiagraph.models import MemoryNamespace

from cortensor_sentinel.sdk_compat import (
    coerce_structured_output_safely,
    probe_cortensor_session_runtime_aware,
    safe_handoff,
    safe_subagent,
    safe_tool_envelope,
)
from cortensor_sentinel.workflow import resolve_repo_identity_root


class _SampleVerdict(BaseModel):
    severity: str
    summary: str


class _StubTurnRuntime:
    def __init__(
        self,
        reply_body: str = "ok",
        *,
        tools: ToolRegistry | None = None,
        action_policy: PolicyCtl | None = None,
    ) -> None:
        self.reply_body = reply_body
        self.tools = tools
        self.action_policy = action_policy
        self.last_payload: dict[str, Any] = {}
        self.last_kwargs: dict[str, Any] = {}
        self.active_grant_seen: Any = None

    def run_turn(self, *, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.last_payload = dict(payload)
        self.last_kwargs = dict(kwargs)
        ctx = kwargs.get("trusted_subagent_context")
        if ctx is not None and ctx.memory_grant_id and self.action_policy is not None:
            self.active_grant_seen = self.action_policy.resolve_active_grant_for_use(
                ctx.memory_grant_id,
                subject_id=ctx.child_agent_id,
                tool="memory",
                method="delegated_read",
            )
        return {
            "body": self.reply_body,
            "session_id": payload["session_id"],
            "metadata": {"thread_id": f"{payload['session_id']}:thread-01"},
            "stats": {"tool_calls": 0, "tool_errors": 0},
        }

    def close(self) -> None:
        return None


def test_finding_01_handoff_to_typed_agent_crashes_upstream_and_safe_handoff_fixes() -> None:
    specialist = Agent(
        name="contract_auditor",
        output_type=_SampleVerdict,
        runtime=_StubTurnRuntime('{"severity":"high","summary":"tx.origin auth"}'),  # type: ignore[arg-type]
    )

    upstream_decl = build_delegate_tool(Handoff(target=specialist))
    upstream_raw = upstream_decl.handler(upstream_decl.args_model(message="audit contract"))
    upstream_result = _tool_result_from_payload(tool_name=upstream_decl.name, payload=upstream_raw)

    with pytest.raises(TypeError, match="not JSON serializable"):
        ToolExecutionBatch(results=[upstream_result]).to_metadata_payload()

    fixed_decl = build_delegate_tool(safe_handoff(specialist))
    fixed_raw = fixed_decl.handler(fixed_decl.args_model(message="audit contract"))
    fixed_result = _tool_result_from_payload(tool_name=fixed_decl.name, payload=fixed_raw)
    serialized = ToolExecutionBatch(results=[fixed_result]).to_metadata_payload()
    decoded = json.loads(serialized)
    assert decoded[0]["data"]["output"] == {"severity": "high", "summary": "tx.origin auth"}


def test_finding_02_tool_returning_basemodel_crashes_upstream_and_safe_envelope_fixes() -> None:
    @tool(min_scope="READ_ONLY", idempotent=True)
    def raw_model_tool(pr_id: int) -> _SampleVerdict:
        """Return unserialized Pydantic model."""
        return _SampleVerdict(severity="medium", summary=f"PR #{pr_id}")

    @tool(min_scope="READ_ONLY", idempotent=True)
    def safe_model_tool(pr_id: int) -> dict[str, Any]:
        """Return JSON-safe tool envelope."""
        return safe_tool_envelope(_SampleVerdict(severity="medium", summary=f"PR #{pr_id}"))

    raw_res = _tool_result_from_payload(
        tool_name="raw_model_tool",
        payload=raw_model_tool.tool_decl.handler({"pr_id": 7}),
    )
    assert "severity='medium'" in raw_res.content
    with pytest.raises(TypeError, match="not JSON serializable"):
        ToolExecutionBatch(results=[raw_res]).to_metadata_payload()

    safe_res = _tool_result_from_payload(
        tool_name="safe_model_tool",
        payload=safe_model_tool.tool_decl.handler({"pr_id": 7}),
    )
    batch_json = json.loads(ToolExecutionBatch(results=[safe_res]).to_metadata_payload())
    assert batch_json[0]["ok"] is True
    assert batch_json[0]["data"]["summary"] == "PR #7"
    assert callable(action_result_to_tool_message)


def test_finding_03_validated_tool_arguments_clobbers_explicit_none_default() -> None:
    @tool(min_scope="READ_ONLY", idempotent=True)
    def list_sessions(router: str, state: str | None = "open") -> str:
        """Observe nullable parameter with non-None default."""
        return f"{router}:{state!r}"

    decl = list_sessions.tool_decl
    validated = _validated_tool_arguments(
        tool_name=decl.name,
        args_model=decl.args_model,
        arguments={"router": "local", "state": None},
        context=ToolExecutionContext(channel="console", target="cli", session_id="test-03"),
    )
    assert isinstance(validated, dict)
    assert "state" not in validated
    assert decl.handler(validated) == "local:'open'"


def test_finding_04_subagent_drops_parent_agent_id_and_model_fixed_by_safe_subagent() -> None:
    rt = _StubTurnRuntime("ok", tools=ToolRegistry())
    parent = Agent(
        name="coordinator",
        agent_id="cortensor-reviewer",
        model="qwen2.5:7b-instruct",
        runtime=rt,  # type: ignore[arg-type]
    )

    upstream_child = subagent(parent, name="raw_child", instructions="Check diff.")
    assert upstream_child.agent_id is None
    assert upstream_child.model is None

    fixed_child = safe_subagent(parent, name="safe_child", instructions="Check diff.")
    assert fixed_child.agent_id == "cortensor-reviewer"
    assert fixed_child.model == "qwen2.5:7b-instruct"
    fixed_child.run("Run bounded check")
    assert rt.last_payload["agent_id"] == "cortensor-reviewer"
    assert rt.last_payload["override_model"] == "qwen2.5:7b-instruct"


def test_finding_05_coerce_output_first_json_trap_and_multi_span_recovery() -> None:
    prose = (
        'Checked diff metadata {"changed_files": 2}. '
        'Final verdict: {"severity": "low", "summary": "all gates green"}'
    )
    agent = Agent(output_type=_SampleVerdict, runtime=_StubTurnRuntime(prose))  # type: ignore[arg-type]
    with pytest.raises(AgentOutputValidationError):
        agent.run("evaluate")

    recovered = coerce_structured_output_safely(prose, _SampleVerdict)
    assert recovered == _SampleVerdict(severity="low", summary="all gates green")


def test_finding_06_cortensor_doctor_ignores_env_overrides_and_chat_pool() -> None:
    cfg = OpenMinionConfig()
    cfg.providers.cortensor.session_id = 0

    router_env = {"CORTENSOR_SESSION_ID": "77"}
    with patch.dict(os.environ, router_env, clear=False):
        resolved = resolve_cortensor_runtime_config(cfg)
        upstream_probe = probe_provider_session(cfg, "cortensor")
        fixed_probe = probe_cortensor_session_runtime_aware(cfg)

    assert resolved.session_id == 77
    assert upstream_probe is not None and upstream_probe.status == "fail"
    assert fixed_probe is not None and fixed_probe.status == "ok"

    portal_env = {
        "CORTENSOR_API_MODE": "openai_chat",
        "CORTENSOR_API_URL": "https://api.cortensor.app/v1",
    }
    with patch.dict(os.environ, portal_env, clear=False):
        upstream_portal = probe_provider_session(cfg, "cortensor")
        fixed_portal = probe_cortensor_session_runtime_aware(cfg)

    assert upstream_portal is not None and upstream_portal.status == "fail"
    assert fixed_portal is None
    assert "http_client=" not in inspect.getsource(CortensorProvider._complete_via_openai_chat_mode)


def test_delegated_memory_grant_materializes_and_revokes_cleanly(tmp_path: Path) -> None:
    policy_ctl = PolicyCtl.with_sqlite(
        tmp_path / "policy.db",
        config=PolicyConfig(mode="enforce"),
    )
    try:
        rt = _StubTurnRuntime("diff ok", tools=ToolRegistry(), action_policy=policy_ctl)
        parent = Agent(name="parent", agent_id="sentinel", runtime=rt)  # type: ignore[arg-type]
        child = safe_subagent(
            parent,
            name="bounded_reader",
            instructions="Read project memory.",
            timeout_seconds=60,
            memory=DelegatedMemoryReadRequest(
                namespaces=(MemoryNamespace(agent_id="sentinel"),),
                record_types=("fact",),
                max_results=5,
                max_context_tokens=512,
            ),
        )
        child.run("check")
        assert rt.active_grant_seen is not None
        assert all(grant.revoked_at is not None for grant in policy_ctl.list_grants())
    finally:
        policy_ctl.close()


def test_identity_bundle_and_portable_memory_bundle_building_blocks(tmp_path: Path) -> None:
    identity_bundle = load_identity_bundle(
        "cortensor-sentinel",
        root=resolve_repo_identity_root(),
    )
    assert identity_bundle.ok is True
    assert identity_bundle.agent is not None
    assert identity_bundle.soul is not None
    assert len(identity_bundle.skills) >= 1

    bundle: MemoryBundle = export_bundle(
        items=[{"kind": "gate", "decision": "block"}],
        metadata={"agent_id": "cortensor-sentinel"},
    )
    out = tmp_path / "bundle.json"
    save_bundle(bundle, out)
    loaded = load_bundle(out)
    receipt = import_bundle(loaded, trust_mode="candidate")
    assert receipt["item_count"] == 1
    assert receipt["trust_mode"] == "candidate"


def test_all_six_standalone_reproducer_scripts_exit_zero() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    scripts = sorted((repo_root / "reproducers").glob("repro_*.py"))
    assert len(scripts) == 6
    for script in scripts:
        completed = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"{script.name} failed:\n{completed.stderr}"


def test_all_six_upstream_patches_present_and_well_formed() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    patch_files = sorted((repo_root / "patches").glob("0*.patch"))
    assert [p.name for p in patch_files] == [
        "01_handoff_basemodel_output_serialization.patch",
        "02_tool_basemodel_return_serialization.patch",
        "03_preserve_explicit_none_tool_arguments.patch",
        "04_subagent_inherit_agent_id_and_model.patch",
        "05_agent_coerce_output_multi_json_spans.patch",
        "06_cortensor_doctor_env_and_http_pool_parity.patch",
    ]
    combined = ""
    for patch_file in patch_files:
        raw = patch_file.read_bytes()
        assert b"\r\n" not in raw, f"{patch_file.name} must use LF line endings"
        text = raw.decode("utf-8")
        assert "diff --git a/src/openminion/" in text
        assert "diff --git a/tests/" in text
        combined += text

    for expected_owner in (
        "src/openminion/api/handoff.py",
        "src/openminion/modules/tool/runtime/registry_toolspec.py",
        "src/openminion/api/agent.py",
        "src/openminion/services/health/probes.py",
        "src/openminion/modules/llm/providers/cortensor/adapter.py",
    ):
        assert expected_owner in combined
