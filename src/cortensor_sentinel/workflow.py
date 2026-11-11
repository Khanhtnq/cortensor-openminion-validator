"""Multi-step OpenMinion triage coordinator exercising all 9 SDK building blocks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openminion import (
    Agent,
    AgentOutputValidationError,
    APIRuntime,
    Handoff,
    MemoryBundle,
    OpenMinionConfig,
)
from openminion.api.handoff import DelegatedMemoryReadRequest
from openminion.base.config import AgentProfileConfig, save_config
from openminion.modules.identity import load_identity_bundle
from openminion.modules.memory.portability import (
    export_bundle,
    import_bundle,
    load_bundle,
    save_bundle,
)
from sophiagraph.models import MemoryNamespace

from cortensor_sentinel.models import (
    CortensorDiagnosticReport,
    CortensorPoIPoUWReport,
    DiffQualityReport,
    ReleaseReadinessVerdict,
    SentinelWorkflowResult,
    SkipBudgetSummary,
)
from cortensor_sentinel.sdk_compat import (
    coerce_structured_output_safely,
    safe_subagent,
)
from cortensor_sentinel.tools import (
    diagnose_cortensor_session_posture,
    inspect_diff_code_quality,
    stage_release_gate_tag,
    validate_cortensor_poi_pouw_quorum,
    verify_test_skip_and_complexity_budget,
)

HANDOFF_TOOL_NAME = "transfer_to_security_auditor"

SAMPLE_TRIAGE_DIFF = """\
diff --git a/src/router/utils.py b/src/router/utils.py
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/src/router/utils.py
@@ -0,0 +1,6 @@
+def parse_router_header(raw: str) -> str:
+    try:
+        return raw.strip()
+    except:
+        return ""
diff --git a/contracts/CortensorEscrowGate.sol b/contracts/CortensorEscrowGate.sol
new file mode 100644
index 0000000..a11b22c
--- /dev/null
+++ b/contracts/CortensorEscrowGate.sol
@@ -0,0 +1,9 @@
+// SPDX-License-Identifier: MIT
+pragma solidity ^0.8.24;
+contract CortensorEscrowGate {
+    address public owner;
+    function sweep(address payable recipient, uint256 amount) external {
+        require(tx.origin == owner, "unauthorized");
+        recipient.call{value: amount}("");
+    }
+}
"""

SAMPLE_POI_MINER_SAMPLES: list[dict[str, Any]] = [
    {
        "miner_id": "cortensor-miner-01",
        "session_id": 101,
        "latency_ms": 580,
        "output": (
            '{"decision":"block","summary":"Critical tx.origin authorization and '
            'unchecked low-level call in CortensorEscrowGate.sol plus bare except in utils.py"}'
        ),
    },
    {
        "miner_id": "cortensor-miner-02",
        "session_id": 102,
        "latency_ms": 640,
        "output": (
            '{"decision":"block","summary":"CortensorEscrowGate.sol uses tx.origin and '
            'unchecked call return value; utils.py adds bare except hazard"}'
        ),
    },
    {
        "miner_id": "cortensor-miner-03",
        "session_id": 103,
        "latency_ms": 710,
        "output": (
            '{"decision":"block","summary":"Blocking findings: tx.origin auth check and '
            'unchecked call in CortensorEscrowGate.sol and bare except in utils.py"}'
        ),
    },
    {
        "miner_id": "cortensor-miner-04-byzantine",
        "session_id": 104,
        "latency_ms": 19200,
        "output": '{"decision":"approve","summary":"looks good"}',
    },
]


@dataclass(frozen=True)
class SentinelRuntimeSettings:
    provider: Literal["ollama", "cortensor", "echo"] = "ollama"
    model: str = "qwen2.5:7b-instruct"
    ollama_base_url: str = "http://127.0.0.1:11434"
    cortensor_base_url: str = "http://127.0.0.1:8080/api/v2/completions"
    cortensor_api_mode: str = "auto"
    cortensor_session_id: int = 1
    agent_id: str = "cortensor-sentinel"
    approval_mode: Literal["allow_once", "deny", "pause"] = "allow_once"
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(
        cls,
        *,
        provider: str | None = None,
        model: str | None = None,
        approval_mode: str = "allow_once",
    ) -> SentinelRuntimeSettings:
        resolved_provider = (
            (provider or os.getenv("SENTINEL_PROVIDER", "") or "ollama").strip().lower()
        )
        if resolved_provider not in {"ollama", "cortensor", "echo"}:
            raise ValueError(f"Unsupported provider: {resolved_provider!r}")

        default_model = (
            "gpt-oss-20b"
            if resolved_provider == "cortensor"
            else ("echo" if resolved_provider == "echo" else "qwen2.5:7b-instruct")
        )
        resolved_model = (model or os.getenv("SENTINEL_MODEL", "") or default_model).strip()
        ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
        cortensor_url = os.getenv(
            "CORTENSOR_API_URL", "http://127.0.0.1:8080/api/v2/completions"
        ).strip()
        cortensor_mode = os.getenv("CORTENSOR_API_MODE", "auto").strip() or "auto"
        raw_session = os.getenv("CORTENSOR_SESSION_ID", "1").strip()
        try:
            session_id = max(1, int(raw_session))
        except ValueError:
            session_id = 1
        return cls(
            provider=resolved_provider,  # type: ignore[arg-type]
            model=resolved_model,
            ollama_base_url=ollama_host,
            cortensor_base_url=cortensor_url,
            cortensor_api_mode=cortensor_mode,
            cortensor_session_id=session_id,
            approval_mode=approval_mode,  # type: ignore[arg-type]
        )


def resolve_repo_identity_root() -> Path:
    """Locate the bundled `agents/` directory containing `cortensor-sentinel`."""
    package_repo_root = Path(__file__).resolve().parents[2]
    candidate = package_repo_root / "agents"
    if (candidate / "cortensor-sentinel" / "AGENT.md").is_file():
        return candidate
    return Path.cwd() / "agents"


def build_sentinel_config(
    settings: SentinelRuntimeSettings,
    *,
    runtime_dir: Path,
) -> tuple[OpenMinionConfig, Path]:
    """Create a deterministic OpenMinionConfig supporting ollama, cortensor, or echo."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    cfg = OpenMinionConfig()
    cfg.runtime.process_mode = "single-process"
    cfg.runtime.daemon_auto_start = False
    cfg.runtime.log_level = "ERROR"
    cfg.storage.path = str(runtime_dir / ".openminion" / "state.db")

    cfg.providers.ollama.model = settings.model
    cfg.providers.ollama.base_url = settings.ollama_base_url
    cfg.providers.ollama.tool_call_strategy = "native"
    cfg.providers.ollama.timeout_seconds = int(settings.timeout_seconds)

    cfg.providers.cortensor.model = settings.model
    cfg.providers.cortensor.base_url = settings.cortensor_base_url
    cfg.providers.cortensor.api_mode = settings.cortensor_api_mode
    cfg.providers.cortensor.session_id = settings.cortensor_session_id
    cfg.providers.cortensor.tool_call_strategy = "hybrid"

    profile = AgentProfileConfig(
        name=settings.agent_id,
        role="Cortensor PR, Smart-Contract & Router Diagnostic Coordinator",
        default_channel="console",
        provider=settings.provider,
        thinking="off",
        system_prompt=(
            "You are Cortensor Sentinel, an evidence-led release and code-quality "
            "triage coordinator for Cortensor and OpenMinion builders."
        ),
    )
    cfg.agents = {settings.agent_id: profile}
    cfg.default_agent = settings.agent_id

    config_path = runtime_dir / "agents.json"
    save_config(cfg, str(config_path))
    return cfg, config_path


def make_operator_approval_gate(mode: Literal["allow_once", "deny", "pause"]):
    """Construct an operator approval callback for Handoff and dangerous=True tools."""
    if mode == "pause":
        return None
    consumed_tools: set[str] = set()

    def _decide(tool_name: str, _args: dict[str, Any], _approval_id: str) -> bool:
        if mode == "deny":
            return False
        if tool_name in consumed_tools:
            return False
        consumed_tools.add(tool_name)
        return True

    return _decide


def _extract_runtime_posture_summary(runtime: APIRuntime, agent_id: str) -> dict[str, Any]:
    posture = runtime.runtime_posture(agent_id=agent_id)
    capability = runtime.capability_report(agent_id=agent_id)
    inventory = runtime.tool_inventory_report(readiness=True)
    ready_count = sum(
        1 for item in inventory if item.get("ready") is True or item.get("available") is True
    )
    return {
        "canonical_turn_path_steps": len(posture.get("canonical_turn_path", ())),
        "agent_id": capability.get("agent_id", agent_id),
        "provider": capability.get("provider") or posture.get("provider"),
        "registered_tool_count": len(inventory),
        "ready_tool_count": ready_count,
    }


def _fallback_diff_report_from_tool(diff_text: str) -> DiffQualityReport:
    payload = inspect_diff_code_quality(diff_text)
    clean = {k: v for k, v in payload.items() if k != "ok"}
    return DiffQualityReport.model_validate(clean)


def _deterministic_verdict_from_evidence(
    diff_report: DiffQualityReport,
    skip_budget: SkipBudgetSummary,
    cortensor_diag: CortensorDiagnosticReport,
    handoff_status: Literal["approved", "denied", "paused", "not_required"],
) -> ReleaseReadinessVerdict:
    actions: list[str] = []
    for item in diff_report.findings:
        actions.append(f"{item.file_path}:{item.line} [{item.rule_id}] {item.remediation}")
    if not skip_budget.passed:
        actions.append(
            f"Resolve {skip_budget.unconditional_markers} unconditional skip/xfail marker(s) "
            f"and {len(skip_budget.high_complexity_functions)} high-complexity function(s)."
        )
    if cortensor_diag.remediation:
        actions.append(cortensor_diag.remediation)

    cortensor_ready = cortensor_diag.runtime_aware_status in {"ok", "warn"}
    if (
        diff_report.status == "block"
        or not skip_budget.passed
        or handoff_status in {"denied", "paused"}
    ):
        decision: Literal["approve", "warn", "block"] = "block"
        risk_score = min(100, 65 + 15 * diff_report.blocking_count)
    elif diff_report.status == "warn" or cortensor_diag.doctor_drift_detected:
        decision = "warn"
        risk_score = 35
    else:
        decision = "approve"
        risk_score = 10

    return ReleaseReadinessVerdict(
        decision=decision,
        risk_score=risk_score,
        diff_status=diff_report.status,
        cortensor_ready=cortensor_ready,
        security_handoff_status=handoff_status,
        rationale=(
            f"Diff status={diff_report.status} ({diff_report.blocking_count} blocking), "
            f"skip_budget_passed={skip_budget.passed}, "
            f"cortensor_mode={cortensor_diag.provider_mode}, "
            f"security_handoff={handoff_status}."
        ),
        required_actions=actions,
    )


def _strip_ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k != "ok"}


def _run_diff_subagent_step(
    base_coordinator: Agent[Any, Any],
    diff_text: str,
    cfg_settings: SentinelRuntimeSettings,
    memory_scope: DelegatedMemoryReadRequest,
) -> DiffQualityReport:
    forced = ["inspect_diff_code_quality"] if cfg_settings.provider != "echo" else []
    out_type = DiffQualityReport if cfg_settings.provider != "echo" else str
    diff_subagent = safe_subagent(
        base_coordinator,
        name="diff_quality_subagent",
        instructions=(
            "Call inspect_diff_code_quality on the supplied diff, then return ONLY a valid "
            "JSON object matching DiffQualityReport (status, files_inspected, finding_count, "
            "blocking_count, findings, summary)."
        ),
        tools=[inspect_diff_code_quality],
        forced_tools=forced,
        output_type=out_type,
        timeout_seconds=int(cfg_settings.timeout_seconds),
        memory=memory_scope,
    )
    try:
        sub_res = diff_subagent.run(
            f"Audit this unified diff:\n{diff_text}",
            timeout_seconds=cfg_settings.timeout_seconds,
        )
        if isinstance(sub_res.output, DiffQualityReport):
            return sub_res.output
        return _fallback_diff_report_from_tool(diff_text)
    except AgentOutputValidationError as exc:
        try:
            return coerce_structured_output_safely(exc.raw_text, DiffQualityReport)
        except AgentOutputValidationError:
            return _fallback_diff_report_from_tool(diff_text)


def _run_security_handoff_step(
    runtime: APIRuntime,
    agent_model: str | None,
    cfg_settings: SentinelRuntimeSettings,
    diff_report: DiffQualityReport,
    candidate_ref: str,
) -> tuple[Literal["approved", "denied", "paused", "not_required"], str]:
    security_specialist = Agent[str, str](
        name="security_auditor",
        agent_id=cfg_settings.agent_id,
        model=agent_model,
        runtime=runtime,
        tools=[],
        instructions=(
            "You are the smart-contract and security policy specialist. "
            "Give a two-sentence remediation verdict for the escalated findings."
        ),
    )
    handoff_coordinator = Agent[str, str](
        name="escalation_router",
        agent_id=cfg_settings.agent_id,
        model=agent_model,
        runtime=runtime,
        instructions=(
            f"Delegate security review to {HANDOFF_TOOL_NAME} and return its concise findings."
        ),
        handoffs=[Handoff(target=security_specialist, name=HANDOFF_TOOL_NAME)],
        forced_tools=[HANDOFF_TOOL_NAME] if cfg_settings.provider != "echo" else [],
    )
    handoff_turn = handoff_coordinator.run(
        f"Escalate {diff_report.blocking_count} blocking findings from {candidate_ref}: "
        f"{diff_report.summary}",
        approval_callback=make_operator_approval_gate(cfg_settings.approval_mode),
        timeout_seconds=cfg_settings.timeout_seconds,
    )
    if handoff_turn.metadata.get("respond_kind") == "policy_confirmation_prompt":
        status: Literal["approved", "denied", "paused", "not_required"] = "paused"
    elif cfg_settings.approval_mode == "deny":
        status = "denied"
    else:
        status = "approved"
    return status, handoff_turn.text.strip()


def _run_verdict_step(
    runtime: APIRuntime,
    agent_model: str | None,
    cfg_settings: SentinelRuntimeSettings,
    candidate_ref: str,
    diff_report: DiffQualityReport,
    skip_summary: SkipBudgetSummary,
    cortensor_report: CortensorDiagnosticReport,
    handoff_status: Literal["approved", "denied", "paused", "not_required"],
) -> ReleaseReadinessVerdict:
    evidence_digest = {
        "candidate_ref": candidate_ref,
        "diff_status": diff_report.status,
        "blocking_findings": diff_report.blocking_count,
        "skip_budget_passed": skip_summary.passed,
        "cortensor_mode": cortensor_report.provider_mode,
        "cortensor_status": cortensor_report.runtime_aware_status,
        "security_handoff_status": handoff_status,
    }
    verdict_agent = Agent(
        name="verdict_synthesizer",
        agent_id=cfg_settings.agent_id,
        model=agent_model,
        runtime=runtime,
        tools=[],
        instructions=(
            "Synthesize the release readiness verdict as a JSON object with exact fields: "
            "decision ('approve'|'warn'|'block'), risk_score (0..100), "
            "diff_status ('clean'|'warn'|'block'), cortensor_ready (bool), "
            "security_handoff_status ('approved'|'denied'|'paused'|'not_required'), "
            "rationale (str), required_actions (list[str]). "
            "If blocking_findings > 0 or security_handoff_status in ('denied','paused'), "
            "decision MUST be 'block'."
        ),
        output_type=ReleaseReadinessVerdict if cfg_settings.provider != "echo" else str,
    )
    try:
        verdict_turn = verdict_agent.run(
            f"Produce ReleaseReadinessVerdict JSON for evidence: {evidence_digest}",
            timeout_seconds=cfg_settings.timeout_seconds,
        )
        if isinstance(verdict_turn.output, ReleaseReadinessVerdict):
            return verdict_turn.output
    except AgentOutputValidationError as exc:
        try:
            return coerce_structured_output_safely(exc.raw_text, ReleaseReadinessVerdict)
        except AgentOutputValidationError:
            pass
    return _deterministic_verdict_from_evidence(
        diff_report, skip_summary, cortensor_report, handoff_status
    )


def _maybe_stage_release_receipt(
    runtime: APIRuntime,
    agent_model: str | None,
    cfg_settings: SentinelRuntimeSettings,
    workspace_dir: Path,
    candidate_ref: str,
    decision: str,
) -> dict[str, Any] | None:
    if cfg_settings.approval_mode != "allow_once":
        return None
    receipt_file = workspace_dir / "release_gate_receipt.json"
    gate_agent = Agent[str, str](
        name="release_stager",
        agent_id=cfg_settings.agent_id,
        model=agent_model,
        runtime=runtime,
        instructions=(
            "Call stage_release_gate_tag once with the provided candidate_ref, "
            "decision, and receipt_path, then confirm in one short sentence."
        ),
        tools=[stage_release_gate_tag],
        forced_tools=["stage_release_gate_tag"] if cfg_settings.provider != "echo" else [],
    )
    gate_agent.run(
        f"Stage release receipt for candidate_ref={candidate_ref!r}, "
        f"decision={decision!r}, receipt_path={str(receipt_file)!r}",
        approval_callback=make_operator_approval_gate("allow_once"),
        timeout_seconds=cfg_settings.timeout_seconds,
    )
    if not receipt_file.is_file() and cfg_settings.provider == "echo":
        stage_release_gate_tag(candidate_ref, decision, str(receipt_file))
    if receipt_file.is_file():
        return json.loads(receipt_file.read_text(encoding="utf-8"))
    return None


def run_sentinel_triage_workflow(
    *,
    diff_text: str = SAMPLE_TRIAGE_DIFF,
    target_scan_path: str | Path | None = None,
    settings: SentinelRuntimeSettings | None = None,
    workspace_dir: Path,
    candidate_ref: str = "pr-42-cortensor-escrow",
    poi_samples: list[dict[str, Any]] | None = None,
) -> SentinelWorkflowResult:
    """Execute the full 9-building-block OpenMinion triage & diagnostics workflow."""
    cfg_settings = settings or SentinelRuntimeSettings.from_env()
    scan_target = (
        Path(target_scan_path) if target_scan_path is not None else Path(__file__).resolve().parent
    )

    identity_root = resolve_repo_identity_root()
    identity_bundle = load_identity_bundle("cortensor-sentinel", root=identity_root)

    _, config_path = build_sentinel_config(cfg_settings, runtime_dir=workspace_dir)
    runtime = APIRuntime.from_config_path(
        str(config_path),
        home_root=workspace_dir,
        data_root=workspace_dir / ".openminion",
    )

    try:
        posture_summary = _extract_runtime_posture_summary(runtime, cfg_settings.agent_id)

        # Deterministic tool snapshots paired with live Agent tool/subagent/handoff turns
        skip_summary = SkipBudgetSummary.model_validate(
            _strip_ok(verify_test_skip_and_complexity_budget(str(scan_target)))
        )
        cortensor_report = CortensorDiagnosticReport.model_validate(
            _strip_ok(diagnose_cortensor_session_posture(str(config_path), False))
        )
        poi_payload = validate_cortensor_poi_pouw_quorum(
            json.dumps(poi_samples if poi_samples is not None else SAMPLE_POI_MINER_SAMPLES)
        )
        poi_report = CortensorPoIPoUWReport.model_validate(_strip_ok(poi_payload))

        memory_scope = DelegatedMemoryReadRequest(
            namespaces=(MemoryNamespace(agent_id=cfg_settings.agent_id),),
            record_types=("fact", "episode"),
            max_results=10,
            max_context_tokens=1024,
        )
        agent_model = None if cfg_settings.provider == "echo" else cfg_settings.model
        base_coordinator = Agent(
            name="triage_coordinator",
            agent_id=cfg_settings.agent_id,
            model=agent_model,
            runtime=runtime,
            instructions="Coordinate PR, smart-contract, and Cortensor readiness reviews.",
            tools=[validate_cortensor_poi_pouw_quorum],
            session_id="sentinel-primary-session",
        )

        diff_report = _run_diff_subagent_step(
            base_coordinator, diff_text, cfg_settings, memory_scope
        )
        handoff_status, handoff_summary = _run_security_handoff_step(
            runtime, agent_model, cfg_settings, diff_report, candidate_ref
        )
        verdict = _run_verdict_step(
            runtime,
            agent_model,
            cfg_settings,
            candidate_ref,
            diff_report,
            skip_summary,
            cortensor_report,
            handoff_status,
        )
        release_receipt = _maybe_stage_release_receipt(
            runtime, agent_model, cfg_settings, workspace_dir, candidate_ref, verdict.decision
        )

        # Building Block #6: Durable session_id + thread_id continuity vs isolated session
        continuity_agent = Agent[str, str](
            name="session_ledger_agent",
            agent_id=cfg_settings.agent_id,
            model=agent_model,
            runtime=runtime,
            tools=[],
            session_id="sentinel-continuity-session",
            instructions=(
                "Answer concisely in one sentence using prior conversation turns when available."
            ),
        )
        first_turn = continuity_agent.run(
            f"Record release gate code SENTINEL-{verdict.decision.upper()}-{verdict.risk_score} "
            f"for {candidate_ref}.",
            timeout_seconds=cfg_settings.timeout_seconds,
        )
        followup_turn = continuity_agent.run(
            "What release gate code did we just record?",
            thread_id=first_turn.thread_id,
            timeout_seconds=cfg_settings.timeout_seconds,
        )
        isolated_turn = continuity_agent.run(
            "What release gate code did we just record? "
            "Say UNKNOWN if none was given in this session.",
            session_id="sentinel-isolated-control-session",
            timeout_seconds=cfg_settings.timeout_seconds,
        )

        # Building Block #7: Portable MemoryBundle export, disk round-trip, and candidate import
        bundle: MemoryBundle = export_bundle(
            items=[
                {
                    "kind": "triage_verdict",
                    "candidate_ref": candidate_ref,
                    "decision": verdict.decision,
                    "risk_score": verdict.risk_score,
                    "blocking_findings": diff_report.blocking_count,
                },
                {
                    "kind": "cortensor_posture",
                    "provider_mode": cortensor_report.provider_mode,
                    "runtime_aware_status": cortensor_report.runtime_aware_status,
                    "doctor_drift_detected": cortensor_report.doctor_drift_detected,
                    "poi_quorum_reached": poi_report.quorum_reached,
                    "poi_consensus_score": poi_report.poi_consensus_score,
                },
            ],
            metadata={
                "agent_id": cfg_settings.agent_id,
                "provider": cfg_settings.provider,
                "session_id": followup_turn.session_id or "sentinel-continuity-session",
            },
        )
        bundle_path = workspace_dir / "sentinel_memory_bundle.json"
        save_bundle(bundle, bundle_path)
        reloaded_bundle = load_bundle(bundle_path)
        import_receipt = import_bundle(reloaded_bundle, trust_mode="candidate")

        return SentinelWorkflowResult(
            provider=cfg_settings.provider,
            model=cfg_settings.model,
            verdict=verdict,
            diff_report=diff_report,
            skip_budget=skip_summary,
            cortensor_diagnostic=cortensor_report,
            poi_pouw_report=poi_report,
            handoff_summary=handoff_summary,
            followup_session_id=followup_turn.session_id or "sentinel-continuity-session",
            followup_thread_id=followup_turn.thread_id,
            followup_reply=followup_turn.text.strip(),
            isolated_session_reply=isolated_turn.text.strip(),
            memory_bundle_json=reloaded_bundle.to_json(),
            memory_import_receipt=import_receipt,
            identity_bundle_ok=identity_bundle.ok,
            identity_fingerprint=identity_bundle.fingerprint,
            runtime_posture_summary=posture_summary,
            release_receipt=release_receipt,
        )
    finally:
        runtime.close()
