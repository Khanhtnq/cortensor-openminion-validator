"""Typed Pydantic output contracts for Cortensor Sentinel agents and workflows."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CodeQualityFinding(BaseModel):
    rule_id: str = Field(
        description="Deterministic rule identifier (e.g. CQ-TOP-DOC, SOL-TX-ORIGIN)."
    )
    severity: Literal["low", "medium", "high", "critical"]
    file_path: str
    line: int = Field(default=1, ge=1)
    summary: str
    remediation: str


class SkipBudgetSummary(BaseModel):
    scanned_files: int = Field(ge=0)
    total_markers: int = Field(ge=0)
    unconditional_markers: int = Field(ge=0)
    conditional_markers: int = Field(ge=0)
    high_complexity_functions: list[str] = Field(default_factory=list)
    passed: bool


class DiffQualityReport(BaseModel):
    status: Literal["clean", "warn", "block"]
    files_inspected: int = Field(ge=0)
    finding_count: int = Field(ge=0)
    blocking_count: int = Field(ge=0)
    findings: list[CodeQualityFinding] = Field(default_factory=list)
    summary: str


class CortensorDiagnosticReport(BaseModel):
    provider_mode: Literal["openai_chat", "cortensor_completion"]
    base_url: str
    model: str
    api_key_configured: bool
    effective_session_ids: list[int] = Field(default_factory=list)
    upstream_doctor_status: Literal["ok", "warn", "fail", "none"]
    runtime_aware_status: Literal["ok", "warn", "fail"]
    doctor_drift_detected: bool = False
    endpoint_reachable: bool | None = None
    remediation: str = ""


class MinerScorecard(BaseModel):
    miner_id: str
    session_id: int = Field(default=1, ge=1)
    schema_valid: bool
    poi_similarity: float = Field(ge=0.0, le=1.0)
    pouw_quality_score: float = Field(ge=0.0, le=1.0)
    latency_ms: int = Field(default=0, ge=0)
    decision_value: str = ""
    is_outlier: bool = False


class CortensorPoIPoUWReport(BaseModel):
    quorum_reached: bool
    total_miners: int = Field(ge=0)
    valid_miners: int = Field(ge=0)
    agreeing_miners: int = Field(ge=0)
    required_quorum: int = Field(ge=1)
    poi_consensus_score: float = Field(ge=0.0, le=1.0)
    pouw_mean_quality: float = Field(ge=0.0, le=1.0)
    consensus_decision: str = ""
    outlier_miner_ids: list[str] = Field(default_factory=list)
    miner_scorecards: list[MinerScorecard] = Field(default_factory=list)
    summary: str


class ReleaseReadinessVerdict(BaseModel):
    decision: Literal["approve", "warn", "block"]
    risk_score: int = Field(ge=0, le=100)
    diff_status: Literal["clean", "warn", "block"]
    cortensor_ready: bool
    security_handoff_status: Literal["approved", "denied", "paused", "not_required"]
    rationale: str
    required_actions: list[str] = Field(default_factory=list)


class SentinelWorkflowResult(BaseModel):
    provider: str
    model: str
    verdict: ReleaseReadinessVerdict
    diff_report: DiffQualityReport
    skip_budget: SkipBudgetSummary
    cortensor_diagnostic: CortensorDiagnosticReport
    poi_pouw_report: CortensorPoIPoUWReport
    handoff_summary: str
    followup_session_id: str
    followup_thread_id: str | None = None
    followup_reply: str
    isolated_session_reply: str
    memory_bundle_json: str
    memory_import_receipt: dict[str, Any]
    identity_bundle_ok: bool
    identity_fingerprint: str
    runtime_posture_summary: dict[str, Any]
    release_receipt: dict[str, Any] | None = None
