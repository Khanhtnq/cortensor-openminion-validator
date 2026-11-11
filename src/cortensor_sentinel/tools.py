"""Deterministic OpenMinion @tool functions for PR, contract, PoI/PoUW, and Cortensor triage."""

from __future__ import annotations

import ast
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from openminion import OpenMinionConfig, tool
from openminion.base.config import load_config
from openminion.modules.llm.providers.cortensor.config import (
    resolve_cortensor_runtime_config,
)
from openminion.services.health.probes import probe_provider_session

from cortensor_sentinel.models import (
    CodeQualityFinding,
    CortensorDiagnosticReport,
    CortensorPoIPoUWReport,
    DiffQualityReport,
    MinerScorecard,
    SkipBudgetSummary,
)
from cortensor_sentinel.sdk_compat import (
    _is_cortensor_completion_mode,
    _positive_unique_sessions,
    iter_json_object_spans,
    probe_cortensor_session_runtime_aware,
    safe_tool_envelope,
)

_JUNK_DRAWER_FILENAMES = frozenset({"utils.py", "helpers.py", "misc.py", "common_utils.py"})
_MARKER_KINDS = frozenset({"skip", "skipif", "xfail"})
_STOPWORDS = frozenset({"in", "and", "or", "the", "of", "to", "is", "by", "on", "at", "for"})
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?i)\b(api_key|secret_key|private_key|bearer_token)\s*=\s*['"][A-Za-z0-9_\-]{16,}['"]"""
)
_SOLIDITY_TX_ORIGIN_RE = re.compile(r"\btx\.origin\b")
_SOLIDITY_UNCHECKED_CALL_RE = re.compile(r"\.call\s*\{[^}]*\}\s*\(")
_SOLIDITY_SELFDESTRUCT_RE = re.compile(r"\bselfdestruct\s*\(")


def _extract_changed_files_from_diff(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Map target file paths in a unified diff to added (line_no, line_text) pairs."""
    files: dict[str, list[tuple[int, str]]] = {}
    current_file: str | None = None
    current_line = 1
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:].strip()
            files.setdefault(current_file, [])
            current_line = 1
        elif raw_line.startswith("@@"):
            match = re.search(r"\+(\d+)", raw_line)
            current_line = int(match.group(1)) if match else 1
        elif current_file is not None:
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                files[current_file].append((current_line, raw_line[1:]))
                current_line += 1
            elif not raw_line.startswith("-"):
                current_line += 1
    return files


def _needs_top_docstring_warning(
    file_path: str, basename: str, added_lines: list[tuple[int, str]]
) -> bool:
    if not file_path.endswith(".py") or basename.startswith("test_") or basename == "__init__.py":
        return False
    if not added_lines or added_lines[0][0] != 1:
        return False
    first_non_empty = next(
        (text.strip() for _, text in added_lines if text.strip() and not text.startswith("#!")),
        "",
    )
    return not (first_non_empty.startswith('"""') or first_non_empty.startswith("'''"))


def _audit_file_level_rules(
    file_path: str, added_lines: list[tuple[int, str]]
) -> list[CodeQualityFinding]:
    findings: list[CodeQualityFinding] = []
    basename = Path(file_path).name.lower()
    if basename in _JUNK_DRAWER_FILENAMES:
        findings.append(
            CodeQualityFinding(
                rule_id="CQ-JUNK-DRAWER",
                severity="medium",
                file_path=file_path,
                line=1,
                summary=(
                    f"Generic junk-drawer module name '{Path(file_path).name}' "
                    "violates CODE_QUALITY.md §4."
                ),
                remediation="Move helpers into the domain package that truthfully owns them.",
            )
        )
    if _needs_top_docstring_warning(file_path, basename, added_lines):
        findings.append(
            CodeQualityFinding(
                rule_id="CQ-TOP-EXPLAINER",
                severity="low",
                file_path=file_path,
                line=1,
                summary=(
                    "New Python module lacks a short top-of-file module docstring "
                    "(CODE_QUALITY.md §2)."
                ),
                remediation="Add a one-line factual module docstring at line 1.",
            )
        )
    return findings


def _audit_python_line(file_path: str, line_no: int, text: str) -> list[CodeQualityFinding]:
    findings: list[CodeQualityFinding] = []
    if re.search(r"\bexcept\s*:\s*$", text):
        findings.append(
            CodeQualityFinding(
                rule_id="CQ-BLANKET-EXCEPT",
                severity="high",
                file_path=file_path,
                line=line_no,
                summary="Bare `except:` clause hides unexpected failures (CODE_QUALITY.md §1).",
                remediation=(
                    "Catch narrow, expected exception types and preserve observable errors."
                ),
            )
        )
    if re.search(r"@pytest\.mark\.(skip|xfail)\b", text):
        findings.append(
            CodeQualityFinding(
                rule_id="CQ-SKIP-RATCHET",
                severity="high",
                file_path=file_path,
                line=line_no,
                summary=(
                    "Added pytest skip/xfail marker increases test skip debt ceiling "
                    "(CODE_QUALITY.md §7)."
                ),
                remediation=(
                    "Fix the underlying test or justify baseline update in test_skip_baseline.tsv."
                ),
            )
        )
    return findings


def _audit_solidity_line(file_path: str, line_no: int, text: str) -> list[CodeQualityFinding]:
    findings: list[CodeQualityFinding] = []
    if _SOLIDITY_TX_ORIGIN_RE.search(text):
        findings.append(
            CodeQualityFinding(
                rule_id="SOL-TX-ORIGIN",
                severity="critical",
                file_path=file_path,
                line=line_no,
                summary="Authorization check using `tx.origin` enables phishing relay attacks.",
                remediation="Use `msg.sender` with explicit role or callback-sender checks.",
            )
        )
    if _SOLIDITY_SELFDESTRUCT_RE.search(text):
        findings.append(
            CodeQualityFinding(
                rule_id="SOL-SELFDESTRUCT",
                severity="high",
                file_path=file_path,
                line=line_no,
                summary="Deprecated `selfdestruct` opcode detected in smart contract diff.",
                remediation="Use pausable escrow withdrawal patterns instead of `selfdestruct`.",
            )
        )
    if _SOLIDITY_UNCHECKED_CALL_RE.search(text) and "bool " not in text and "(bool" not in text:
        findings.append(
            CodeQualityFinding(
                rule_id="SOL-UNCHECKED-CALL",
                severity="high",
                file_path=file_path,
                line=line_no,
                summary="Low-level `.call{...}(...)` ignores boolean success return value.",
                remediation="Capture `(bool ok, ) = target.call{...}(...)` and revert when `!ok`.",
            )
        )
    return findings


def _audit_added_lines(
    file_path: str, added_lines: list[tuple[int, str]]
) -> list[CodeQualityFinding]:
    findings = _audit_file_level_rules(file_path, added_lines)
    is_py = file_path.endswith(".py")
    is_sol = file_path.endswith(".sol")
    for line_no, text in added_lines:
        if _SECRET_ASSIGNMENT_RE.search(text):
            findings.append(
                CodeQualityFinding(
                    rule_id="SEC-HARDCODED-SECRET",
                    severity="critical",
                    file_path=file_path,
                    line=line_no,
                    summary="Hardcoded credential or private key literal detected in added code.",
                    remediation="Load secrets from environment variables via EnvironmentConfig.",
                )
            )
        if is_py:
            findings.extend(_audit_python_line(file_path, line_no, text))
        if is_sol:
            findings.extend(_audit_solidity_line(file_path, line_no, text))
    return findings


@tool(min_scope="READ_ONLY", idempotent=True, tags=("triage", "code_quality"))
def inspect_diff_code_quality(diff_text: str) -> dict[str, Any]:
    """Inspect a unified git diff against CODE_QUALITY.md and smart-contract safety rules."""
    changed = _extract_changed_files_from_diff(diff_text)
    findings: list[CodeQualityFinding] = []
    for file_path, added_lines in sorted(changed.items()):
        findings.extend(_audit_added_lines(file_path, added_lines))

    blocking_count = sum(1 for item in findings if item.severity in {"high", "critical"})
    if blocking_count > 0:
        status = "block"
    elif findings:
        status = "warn"
    else:
        status = "clean"

    report = DiffQualityReport(
        status=status,
        files_inspected=len(changed),
        finding_count=len(findings),
        blocking_count=blocking_count,
        findings=findings,
        summary=(
            f"Inspected {len(changed)} changed file(s): {len(findings)} finding(s), "
            f"{blocking_count} blocking."
        ),
    )
    return safe_tool_envelope(report)


def _marker_kind(node: ast.AST) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if not isinstance(target, ast.Attribute) or target.attr not in _MARKER_KINDS:
        return None
    owner = target.value
    if not isinstance(owner, ast.Attribute) or owner.attr != "mark":
        return None
    if not isinstance(owner.value, ast.Name) or owner.value.id != "pytest":
        return None
    return target.attr


def _literal_truth(expr: ast.AST | None) -> bool | None:
    if not isinstance(expr, ast.Constant):
        return None
    if not isinstance(expr.value, str):
        return bool(expr.value)
    try:
        parsed = ast.parse(expr.value.strip(), mode="eval")
    except SyntaxError:
        return None
    if isinstance(parsed.body, ast.Constant) and not isinstance(parsed.body.value, str):
        return bool(parsed.body.value)
    return None


def _is_unconditional_marker(node: ast.AST, kind: str) -> bool:
    if kind == "skip" or not isinstance(node, ast.Call):
        return True
    kw_cond = next((kw.value for kw in node.keywords if kw.arg == "condition"), None)
    conditions = (kw_cond,) if kw_cond is not None else tuple(node.args)
    if not conditions:
        return True
    return any(_literal_truth(cond) is True for cond in conditions)


def _cyclomatic_complexity(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = 1
    for child in ast.walk(fn_node):
        if isinstance(
            child,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp),
        ):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += max(1, len(child.values) - 1)
    return score


def _count_node_markers(node: ast.AST) -> tuple[int, int]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return 0, 0
    total = 0
    unconditional = 0
    for dec in node.decorator_list:
        kind = _marker_kind(dec)
        if kind is not None:
            total += 1
            if _is_unconditional_marker(dec, kind):
                unconditional += 1
    return total, unconditional


def _scan_single_ast(
    tree: ast.AST, rel_label: str, max_complexity: int
) -> tuple[int, int, list[str]]:
    total = 0
    unconditional = 0
    complex_fns: list[str] = []
    for node in ast.walk(tree):
        node_total, node_uncond = _count_node_markers(node)
        total += node_total
        unconditional += node_uncond
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            complexity = _cyclomatic_complexity(node)
            if complexity > max_complexity:
                complex_fns.append(f"{rel_label}:{node.name} (cc={complexity})")
    return total, unconditional, complex_fns


@tool(min_scope="READ_ONLY", idempotent=True, tags=("triage", "validation"))
def verify_test_skip_and_complexity_budget(
    target_path: str,
    max_complexity: int = 12,
) -> dict[str, Any]:
    """Scan Python files for unconditional pytest skip/xfail markers and AST complexity ceilings."""
    root = Path(target_path)
    py_files = [root] if root.is_file() else sorted(root.rglob("*.py"))
    total_markers = 0
    unconditional_markers = 0
    high_complexity: list[str] = []

    for py_file in py_files:
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        rel_label = (
            py_file.name if root.is_file() else str(py_file.relative_to(root)).replace("\\", "/")
        )
        file_total, file_uncond, file_complex = _scan_single_ast(tree, rel_label, max_complexity)
        total_markers += file_total
        unconditional_markers += file_uncond
        high_complexity.extend(file_complex)

    summary = SkipBudgetSummary(
        scanned_files=len(py_files),
        total_markers=total_markers,
        unconditional_markers=unconditional_markers,
        conditional_markers=total_markers - unconditional_markers,
        high_complexity_functions=high_complexity,
        passed=(unconditional_markers == 0 and not high_complexity),
    )
    return safe_tool_envelope(summary)


def _probe_http_endpoint(url: str, timeout_seconds: float = 2.0) -> bool:
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
            return int(resp.status) < 500
    except urllib_error.HTTPError as exc:
        return int(exc.code) < 500
    except Exception:
        return False


def _cortensor_remediation_message(
    runtime_status: str,
    runtime_probe: Any,
    provider_mode: str,
    api_key_configured: bool,
    key_env_name: str,
    drift: bool,
) -> str:
    if runtime_status == "fail" and runtime_probe is not None:
        return str(runtime_probe.remediation)
    if provider_mode == "openai_chat" and not api_key_configured:
        return f"Export `{key_env_name}` for authenticated Cortensor Portal chat completions."
    if drift:
        return (
            "Sync `providers.cortensor` in `agents.json` with `CORTENSOR_*` environment "
            "variables so `openminion doctor` matches runtime execution."
        )
    return ""


def _cortensor_probe_drift(upstream_probe: Any, runtime_probe: Any) -> bool:
    if (upstream_probe is None) != (runtime_probe is None):
        return True
    if upstream_probe is not None and runtime_probe is not None:
        return bool(upstream_probe.status != runtime_probe.status)
    return False


@tool(min_scope="READ_ONLY", idempotent=True, tags=("cortensor", "diagnostics"))
def diagnose_cortensor_session_posture(
    config_path: str = "",
    probe_network: bool = False,
) -> dict[str, Any]:
    """Audit Cortensor Router/Portal session config, env overrides, and upstream doctor drift."""
    cfg = (
        load_config(config_path)
        if config_path.strip() and Path(config_path).is_file()
        else OpenMinionConfig()
    )
    env_map = dict(os.environ)
    resolved = resolve_cortensor_runtime_config(cfg, env=env_map)
    completion_mode = _is_cortensor_completion_mode(
        api_mode=resolved.api_mode,
        base_url=resolved.base_url,
    )
    provider_mode = "cortensor_completion" if completion_mode else "openai_chat"
    key_env_name = resolved.api_key_env.strip() or "CORTENSOR_API_KEY"
    api_key_configured = bool(resolved.api_key.strip() or env_map.get(key_env_name, "").strip())

    upstream_probe = probe_provider_session(cfg, "cortensor")
    runtime_probe = probe_cortensor_session_runtime_aware(cfg, env=env_map)
    upstream_status = upstream_probe.status if upstream_probe is not None else "none"
    runtime_status = (
        runtime_probe.status
        if runtime_probe is not None
        else ("ok" if api_key_configured else "warn")
    )
    drift = _cortensor_probe_drift(upstream_probe, runtime_probe)
    reachable = (
        _probe_http_endpoint(resolved.base_url.strip())
        if probe_network and resolved.base_url.strip()
        else None
    )

    report = CortensorDiagnosticReport(
        provider_mode=provider_mode,
        base_url=resolved.base_url,
        model=resolved.model,
        api_key_configured=api_key_configured,
        effective_session_ids=_positive_unique_sessions(resolved),
        upstream_doctor_status=upstream_status,  # type: ignore[arg-type]
        runtime_aware_status=runtime_status,  # type: ignore[arg-type]
        doctor_drift_detected=drift,
        endpoint_reachable=reachable,
        remediation=_cortensor_remediation_message(
            runtime_status, runtime_probe, provider_mode, api_key_configured, key_env_name, drift
        ),
    )
    return safe_tool_envelope(report)


@tool(
    min_scope="WRITE_SAFE",
    dangerous=True,
    idempotent=False,
    tags=("release", "gate"),
)
def stage_release_gate_tag(
    candidate_ref: str,
    decision: str,
    receipt_path: str,
) -> dict[str, Any]:
    """Write a signed-off release gate receipt after operator confirmation (dangerous=True)."""
    target = Path(receipt_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "candidate_ref": candidate_ref.strip(),
        "decision": decision.strip(),
        "staged_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"ok": True, "staged": True, **payload, "receipt_path": target.name}


def _parse_miner_json_payload(raw_output: str) -> dict[str, Any] | None:
    text = raw_output.strip()
    if not text:
        return None
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass
    spans = list(iter_json_object_spans(text))
    if not spans:
        return None
    try:
        fallback = json.loads(spans[-1])
        return fallback if isinstance(fallback, dict) else None
    except json.JSONDecodeError:
        return None


def _tokenize_miner_output(raw_output: str, parsed: dict[str, Any] | None) -> set[str]:
    words = {
        tok for tok in re.findall(r"[a-z0-9_]{2,}", raw_output.lower()) if tok not in _STOPWORDS
    }
    if parsed:
        for key, val in parsed.items():
            if isinstance(val, (str, int, bool)) and len(str(val).strip()) <= 24:
                words.add(f"{str(key).lower()}={str(val).strip().lower()}")
    return words


def _jaccard_similarity(left: set[str], right: set[str]) -> float:
    union = left | right
    return (len(left & right) / len(union)) if union else 1.0


def _compute_pouw_quality(
    parsed: dict[str, Any] | None,
    required_fields: tuple[str, ...],
    raw_output: str,
    latency_ms: int,
    max_latency_ms: int,
) -> tuple[bool, float]:
    present = sum(
        1 for key in required_fields if parsed is not None and str(parsed.get(key, "")).strip()
    )
    schema_ratio = (
        present / len(required_fields) if required_fields else (1.0 if parsed is not None else 0.0)
    )
    schema_valid = parsed is not None and present == len(required_fields)
    depth_ratio = min(1.0, len(raw_output.strip()) / 60.0)
    latency_budget = max(1, max_latency_ms)
    latency_ratio = (
        1.0
        if latency_ms <= latency_budget
        else max(0.0, 1.0 - (latency_ms - latency_budget) / latency_budget)
    )
    score = round(0.45 * schema_ratio + 0.30 * depth_ratio + 0.25 * latency_ratio, 4)
    return schema_valid, score


def _majority_decision_label(decisions: list[str]) -> str:
    counts: dict[str, int] = {}
    for label in decisions:
        if label:
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _extract_miner_decision(parsed: dict[str, Any] | None) -> str:
    if not parsed:
        return ""
    raw = parsed.get("decision") or parsed.get("status") or ""
    return str(raw).strip().lower()


def _compute_peer_poi_similarity(
    idx: int,
    token_sets: list[set[str]],
    parsed_payloads: list[dict[str, Any] | None],
    decisions: list[str],
    consensus_decision: str,
    schema_valid: bool,
) -> float:
    consensus_peers = [
        _jaccard_similarity(token_sets[idx], token_sets[j])
        for j in range(len(token_sets))
        if j != idx and parsed_payloads[j] is not None and decisions[j] == consensus_decision
    ]
    if consensus_peers:
        return round(sum(consensus_peers) / len(consensus_peers), 4)
    any_valid_peers = [
        _jaccard_similarity(token_sets[idx], token_sets[j])
        for j in range(len(token_sets))
        if j != idx and parsed_payloads[j] is not None
    ]
    if any_valid_peers:
        return round(sum(any_valid_peers) / len(any_valid_peers), 4)
    return 1.0 if schema_valid else 0.0


def _score_single_miner_sample(
    idx: int,
    entry: dict[str, Any],
    token_sets: list[set[str]],
    parsed_payloads: list[dict[str, Any] | None],
    decisions: list[str],
    required_fields: tuple[str, ...],
    consensus_decision: str,
    similarity_threshold: float,
    max_latency_ms: int,
) -> MinerScorecard:
    miner_id = str(entry.get("miner_id") or f"miner-{idx + 1}")
    session_id = max(1, int(entry.get("session_id") or (idx + 1)))
    latency_ms = max(0, int(entry.get("latency_ms") or 0))
    raw_out = str(entry.get("output") or "")
    schema_valid, pouw_score = _compute_pouw_quality(
        parsed_payloads[idx], required_fields, raw_out, latency_ms, max_latency_ms
    )
    poi_sim = _compute_peer_poi_similarity(
        idx, token_sets, parsed_payloads, decisions, consensus_decision, schema_valid
    )
    decision_mismatch = bool(consensus_decision and decisions[idx] != consensus_decision)
    is_outlier = (
        (not schema_valid)
        or (poi_sim < similarity_threshold)
        or decision_mismatch
        or (latency_ms > max_latency_ms * 2)
    )
    return MinerScorecard(
        miner_id=miner_id,
        session_id=session_id,
        schema_valid=schema_valid,
        poi_similarity=poi_sim,
        pouw_quality_score=pouw_score,
        latency_ms=latency_ms,
        decision_value=decisions[idx],
        is_outlier=is_outlier,
    )


def _summarize_scorecards(
    scorecards: list[MinerScorecard], consensus_decision: str
) -> CortensorPoIPoUWReport:
    total = len(scorecards)
    required_quorum = (total * 2) // 3 + 1 if total > 0 else 1
    valid_cards = [card for card in scorecards if card.schema_valid]
    honest_cards = [card for card in scorecards if not card.is_outlier]
    outliers = [card.miner_id for card in scorecards if card.is_outlier]
    poi_mean = (
        round(sum(c.poi_similarity for c in honest_cards) / len(honest_cards), 4)
        if honest_cards
        else 0.0
    )
    pouw_mean = (
        round(sum(c.pouw_quality_score for c in scorecards) / total, 4) if total > 0 else 0.0
    )
    quorum_ok = len(honest_cards) >= required_quorum and total > 0
    return CortensorPoIPoUWReport(
        quorum_reached=quorum_ok,
        total_miners=total,
        valid_miners=len(valid_cards),
        agreeing_miners=len(honest_cards),
        required_quorum=required_quorum,
        poi_consensus_score=poi_mean,
        pouw_mean_quality=pouw_mean,
        consensus_decision=consensus_decision,
        outlier_miner_ids=outliers,
        miner_scorecards=scorecards,
        summary=(
            f"PoI/PoUW quorum={'PASS' if quorum_ok else 'FAIL'} "
            f"({len(honest_cards)}/{total} miners >= {required_quorum}, "
            f"PoI={poi_mean:.2f}, PoUW={pouw_mean:.2f}, outliers={len(outliers)})."
        ),
    )


@tool(min_scope="READ_ONLY", idempotent=True, tags=("cortensor", "poi", "pouw"))
def validate_cortensor_poi_pouw_quorum(
    samples_json: str,
    required_fields_csv: str = "decision,summary",
    similarity_threshold: float = 0.45,
    max_latency_ms: int = 8000,
) -> dict[str, Any]:
    """Validate redundant Cortensor miner outputs via Proof-of-Inference & Proof-of-Useful-Work."""
    raw_items = json.loads(samples_json)
    entries = (
        [item for item in raw_items if isinstance(item, dict)]
        if isinstance(raw_items, list)
        else []
    )
    required_fields = tuple(f.strip() for f in required_fields_csv.split(",") if f.strip())
    parsed_payloads = [_parse_miner_json_payload(str(item.get("output") or "")) for item in entries]
    token_sets = [
        _tokenize_miner_output(str(entries[i].get("output") or ""), parsed_payloads[i])
        for i in range(len(entries))
    ]
    decisions = [_extract_miner_decision(parsed) for parsed in parsed_payloads]
    valid_decisions = [
        decisions[i]
        for i, parsed in enumerate(parsed_payloads)
        if parsed is not None and all(str(parsed.get(k, "")).strip() for k in required_fields)
    ]
    consensus_decision = _majority_decision_label(valid_decisions)
    scorecards = [
        _score_single_miner_sample(
            i,
            entries[i],
            token_sets,
            parsed_payloads,
            decisions,
            required_fields,
            consensus_decision,
            similarity_threshold,
            max_latency_ms,
        )
        for i in range(len(entries))
    ]
    return safe_tool_envelope(_summarize_scorecards(scorecards, consensus_decision))
