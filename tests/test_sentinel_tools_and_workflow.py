"""Unit and end-to-end workflow tests for Cortensor Sentinel tools, CLI, and runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from cortensor_sentinel.cli import main as cli_main
from cortensor_sentinel.tools import (
    diagnose_cortensor_session_posture,
    inspect_diff_code_quality,
    stage_release_gate_tag,
    validate_cortensor_poi_pouw_quorum,
    verify_test_skip_and_complexity_budget,
)
from cortensor_sentinel.workflow import (
    SAMPLE_POI_MINER_SAMPLES,
    SAMPLE_TRIAGE_DIFF,
    SentinelRuntimeSettings,
    make_operator_approval_gate,
    run_sentinel_triage_workflow,
)


def test_inspect_diff_code_quality_flags_python_and_solidity_hazards() -> None:
    res = inspect_diff_code_quality(SAMPLE_TRIAGE_DIFF)
    assert res["ok"] is True
    assert res["status"] == "block"
    assert res["files_inspected"] == 2
    rule_ids = {item["rule_id"] for item in res["findings"]}
    assert "CQ-JUNK-DRAWER" in rule_ids
    assert "CQ-TOP-EXPLAINER" in rule_ids
    assert "CQ-BLANKET-EXCEPT" in rule_ids
    assert "SOL-TX-ORIGIN" in rule_ids
    assert "SOL-UNCHECKED-CALL" in rule_ids


def test_inspect_diff_code_quality_passes_clean_documented_module() -> None:
    clean_diff = """\
diff --git a/src/cortensor_sentinel/clean_owner.py b/src/cortensor_sentinel/clean_owner.py
new file mode 100644
--- /dev/null
+++ b/src/cortensor_sentinel/clean_owner.py
@@ -0,0 +1,5 @@
+\"\"\"Clean domain module with top-of-file explainer.\"\"\"
+
+def compute_quorum(total_nodes: int) -> int:
+    return (total_nodes * 2) // 3 + 1
"""
    res = inspect_diff_code_quality(clean_diff)
    assert res["ok"] is True
    assert res["status"] == "clean"
    assert res["finding_count"] == 0
    assert res["blocking_count"] == 0


def test_verify_test_skip_and_complexity_budget_detects_unconditional_skip(
    tmp_path: Path,
) -> None:
    sample_test = tmp_path / "test_sample.py"
    sample_test.write_text(
        'import pytest\n\n@pytest.mark.skip(reason="broken")\ndef test_a():\n    pass\n\n'
        "@pytest.mark.skipif(\"sys.platform == 'win32'\")\ndef test_b():\n    pass\n",
        encoding="utf-8",
    )
    res = verify_test_skip_and_complexity_budget(str(tmp_path))
    assert res["ok"] is True
    assert res["scanned_files"] == 1
    assert res["total_markers"] == 2
    assert res["unconditional_markers"] == 1
    assert res["conditional_markers"] == 1
    assert res["passed"] is False


def test_sentinel_own_test_suite_has_zero_skip_markers() -> None:
    tests_dir = Path(__file__).resolve().parent
    res = verify_test_skip_and_complexity_budget(str(tests_dir))
    assert res["total_markers"] == 0
    assert res["unconditional_markers"] == 0
    assert res["passed"] is True


def test_diagnose_cortensor_session_posture_detects_portal_vs_router_drift() -> None:
    env = {
        "CORTENSOR_API_MODE": "openai_chat",
        "CORTENSOR_API_URL": "https://api.cortensor.app/v1",
        "CORTENSOR_API_KEY": "portal-test-token",
    }
    with patch.dict(os.environ, env, clear=False):
        res = diagnose_cortensor_session_posture("", False)
    assert res["ok"] is True
    assert res["provider_mode"] == "openai_chat"
    assert res["api_key_configured"] is True
    assert res["runtime_aware_status"] == "ok"
    assert res["doctor_drift_detected"] is True


def test_stage_release_gate_tag_writes_deterministic_receipt(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipts" / "gate.json"
    res = stage_release_gate_tag("pr-99", "approve", str(receipt_path))
    assert res["ok"] is True
    assert res["staged"] is True
    saved = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert saved["candidate_ref"] == "pr-99"
    assert saved["decision"] == "approve"


def test_operator_approval_gate_allow_once_and_deny() -> None:
    allow_once = make_operator_approval_gate("allow_once")
    assert allow_once is not None
    assert allow_once("transfer_to_security_auditor", {}, "ap-1") is True
    assert allow_once("transfer_to_security_auditor", {}, "ap-2") is False
    assert allow_once("stage_release_gate_tag", {}, "ap-3") is True

    deny_gate = make_operator_approval_gate("deny")
    assert deny_gate is not None
    assert deny_gate("transfer_to_security_auditor", {}, "ap-4") is False
    assert make_operator_approval_gate("pause") is None


def test_full_9_building_block_workflow_runs_with_echo_provider(tmp_path: Path) -> None:
    settings = SentinelRuntimeSettings(
        provider="echo",
        model="echo",
        approval_mode="allow_once",
    )
    result = run_sentinel_triage_workflow(
        settings=settings,
        workspace_dir=tmp_path / "run_allow",
        candidate_ref="pr-42-cortensor-escrow",
    )
    assert result.provider == "echo"
    assert result.verdict.decision == "block"
    assert result.diff_report.blocking_count >= 3
    assert result.identity_bundle_ok is True
    assert result.identity_fingerprint
    assert result.runtime_posture_summary["registered_tool_count"] > 0
    assert result.memory_import_receipt["item_count"] == 2
    assert result.release_receipt is not None
    assert result.release_receipt["decision"] == "block"


def test_full_workflow_respects_operator_deny_without_staging_receipt(
    tmp_path: Path,
) -> None:
    settings = SentinelRuntimeSettings(
        provider="echo",
        model="echo",
        approval_mode="deny",
    )
    result = run_sentinel_triage_workflow(
        settings=settings,
        workspace_dir=tmp_path / "run_deny",
        candidate_ref="pr-43-denied",
    )
    assert result.verdict.security_handoff_status == "denied"
    assert result.verdict.decision == "block"
    assert result.release_receipt is None


def test_cli_subcommands_triage_doctor_and_demo(tmp_path: Path, capsys) -> None:
    assert cli_main(["triage"]) == 0
    triage_out = json.loads(capsys.readouterr().out)
    assert triage_out["diff_quality"]["status"] == "block"

    assert cli_main(["doctor-cortensor"]) == 0
    doc_out = json.loads(capsys.readouterr().out)
    assert doc_out["provider_mode"] in {"cortensor_completion", "openai_chat"}

    out_json = tmp_path / "demo_out.json"
    assert cli_main(["demo", "--provider", "echo", "--output", str(out_json)]) == 0
    capsys.readouterr()
    saved = json.loads(out_json.read_text(encoding="utf-8"))
    assert saved["verdict"]["decision"] == "block"
    assert saved["poi_pouw_report"]["quorum_reached"] is True

    assert cli_main(["validate-poi"]) == 0
    poi_out = json.loads(capsys.readouterr().out)
    assert poi_out["quorum_reached"] is True
    assert poi_out["agreeing_miners"] == 3
    assert "cortensor-miner-04-byzantine" in poi_out["outlier_miner_ids"]


def test_validate_cortensor_poi_pouw_quorum_detects_outliers_and_quorum_loss() -> None:
    quorum_res = validate_cortensor_poi_pouw_quorum(json.dumps(SAMPLE_POI_MINER_SAMPLES))
    assert quorum_res["ok"] is True
    assert quorum_res["quorum_reached"] is True
    assert quorum_res["total_miners"] == 4
    assert quorum_res["agreeing_miners"] == 3
    assert quorum_res["consensus_decision"] == "block"
    assert quorum_res["outlier_miner_ids"] == ["cortensor-miner-04-byzantine"]

    corrupted_samples = [
        {"miner_id": "m1", "session_id": 1, "latency_ms": 500, "output": "not-json"},
        {
            "miner_id": "m2",
            "session_id": 2,
            "latency_ms": 25000,
            "output": '{"decision":"approve"}',
        },
        {
            "miner_id": "m3",
            "session_id": 3,
            "latency_ms": 600,
            "output": '{"decision":"block","summary":"valid finding report"}',
        },
    ]
    failed_res = validate_cortensor_poi_pouw_quorum(json.dumps(corrupted_samples))
    assert failed_res["ok"] is True
    assert failed_res["quorum_reached"] is False
    assert failed_res["agreeing_miners"] == 0
    assert failed_res["required_quorum"] == 3


def test_sentinel_source_tree_passes_skip_and_complexity_budget() -> None:
    src_dir = Path(__file__).resolve().parents[1] / "src" / "cortensor_sentinel"
    res = verify_test_skip_and_complexity_budget(str(src_dir))
    assert res["unconditional_markers"] == 0
    assert res["high_complexity_functions"] == []
    assert res["passed"] is True
