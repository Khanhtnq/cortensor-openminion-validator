"""Command-line entrypoint for Cortensor Sentinel."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from openminion import ProviderError

from cortensor_sentinel.tools import (
    diagnose_cortensor_session_posture,
    inspect_diff_code_quality,
    validate_cortensor_poi_pouw_quorum,
    verify_test_skip_and_complexity_budget,
)
from cortensor_sentinel.workflow import (
    SAMPLE_POI_MINER_SAMPLES,
    SAMPLE_TRIAGE_DIFF,
    SentinelRuntimeSettings,
    run_sentinel_triage_workflow,
)


def _cmd_triage(args: argparse.Namespace) -> int:
    diff_text = (
        Path(args.diff_file).read_text(encoding="utf-8") if args.diff_file else SAMPLE_TRIAGE_DIFF
    )
    diff_res = inspect_diff_code_quality(diff_text)
    skip_res = verify_test_skip_and_complexity_budget(args.scan_path)
    payload = {
        "diff_quality": {k: v for k, v in diff_res.items() if k != "ok"},
        "skip_and_complexity_budget": {k: v for k, v in skip_res.items() if k != "ok"},
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _cmd_doctor_cortensor(args: argparse.Namespace) -> int:
    res = diagnose_cortensor_session_posture(
        config_path=args.config or "",
        probe_network=args.probe_network,
    )
    clean = {k: v for k, v in res.items() if k != "ok"}
    print(json.dumps(clean, indent=2, sort_keys=True))
    return 0 if clean.get("runtime_aware_status") != "fail" else 1


def _cmd_validate_poi(args: argparse.Namespace) -> int:
    samples_json = (
        Path(args.samples_file).read_text(encoding="utf-8")
        if args.samples_file
        else json.dumps(SAMPLE_POI_MINER_SAMPLES)
    )
    res = validate_cortensor_poi_pouw_quorum(
        samples_json=samples_json,
        required_fields_csv=args.required_fields,
        similarity_threshold=args.similarity_threshold,
        max_latency_ms=args.max_latency_ms,
    )
    clean = {k: v for k, v in res.items() if k != "ok"}
    print(json.dumps(clean, indent=2, sort_keys=True))
    return 0 if clean.get("quorum_reached") is True else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    provider = args.provider if args.live else (args.provider or "echo")
    settings = SentinelRuntimeSettings.from_env(
        provider=provider,
        model=args.model,
        approval_mode=args.approval,
    )
    diff_text = (
        Path(args.diff_file).read_text(encoding="utf-8") if args.diff_file else SAMPLE_TRIAGE_DIFF
    )
    try:
        with tempfile.TemporaryDirectory(prefix="cortensor-sentinel-") as tmp_dir:
            result = run_sentinel_triage_workflow(
                diff_text=diff_text,
                target_scan_path=args.scan_path,
                settings=settings,
                workspace_dir=Path(tmp_dir),
                candidate_ref=args.candidate_ref,
            )
    except ProviderError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": exc.code,
                    "message": exc.message,
                    "remediation": (
                        "Ensure `ollama serve` is running at OLLAMA_HOST for --provider ollama, "
                        "or set CORTENSOR_API_URL / CORTENSOR_API_KEY / CORTENSOR_SESSION_ID "
                        "for --provider cortensor."
                    ),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    rendered = result.model_dump_json(indent=2)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def _cmd_reproduce_bugs(_args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    repro_dir = repo_root / "reproducers"
    scripts = sorted(repro_dir.glob("repro_*.py"))
    if not scripts:
        print("No reproducer scripts found under reproducers/.", file=sys.stderr)
        return 1
    results = []
    for script in scripts:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        results.append(
            {
                "script": script.name,
                "reproduced": proc.returncode == 0,
                "output": proc.stdout.strip(),
            }
        )
    print(json.dumps({"reproducer_count": len(results), "results": results}, indent=2))
    return 0 if all(item["reproduced"] for item in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description=(
            "Cortensor Sentinel — OpenMinion PR, Smart-Contract & Cortensor Session Triage Agent."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_triage = sub.add_parser(
        "triage", help="Run deterministic diff and AST skip/complexity checks."
    )
    p_triage.add_argument("--diff-file", default="", help="Optional unified diff file path.")
    p_triage.add_argument(
        "--scan-path",
        default=str(Path(__file__).resolve().parent),
        help="File or directory to scan for pytest skip markers and AST complexity.",
    )
    p_triage.set_defaults(func=_cmd_triage)

    p_doc = sub.add_parser(
        "doctor-cortensor",
        help="Diagnose Cortensor Portal / Router session config and env override drift.",
    )
    p_doc.add_argument("--config", default="", help="Optional path to agents.json.")
    p_doc.add_argument(
        "--probe-network",
        action="store_true",
        help="Perform a bounded HTTP reachability check against the resolved Cortensor base_url.",
    )
    p_doc.set_defaults(func=_cmd_doctor_cortensor)

    p_demo = sub.add_parser(
        "demo",
        help="Run the full 9-building-block multi-agent triage workflow.",
    )
    p_demo.add_argument(
        "--live",
        action="store_true",
        help="Run against a live LLM provider (default: ollama qwen2.5:7b-instruct).",
    )
    p_demo.add_argument(
        "--provider",
        choices=("ollama", "cortensor", "echo"),
        default=None,
        help="Provider backend ('ollama', 'cortensor', or deterministic 'echo').",
    )
    p_demo.add_argument("--model", default=None, help="Override LLM model identifier.")
    p_demo.add_argument(
        "--approval",
        choices=("allow_once", "deny", "pause"),
        default="allow_once",
        help="Operator decision for peer Handoff and dangerous=True release gate tool.",
    )
    p_demo.add_argument("--diff-file", default="", help="Optional unified diff file path.")
    p_demo.add_argument(
        "--scan-path",
        default=str(Path(__file__).resolve().parent),
        help="Directory scanned by verify_test_skip_and_complexity_budget.",
    )
    p_demo.add_argument("--candidate-ref", default="pr-42-cortensor-escrow")
    p_demo.add_argument(
        "--output", default="", help="Optional path to save JSON workflow transcript."
    )
    p_demo.set_defaults(func=_cmd_demo)

    p_repro = sub.add_parser(
        "reproduce-bugs",
        help="Execute all 6 standalone OpenMinion v0.1.20 bug reproducers offline.",
    )
    p_repro.set_defaults(func=_cmd_reproduce_bugs)

    p_poi = sub.add_parser(
        "validate-poi",
        help="Validate Cortensor redundant miner PoI consensus and PoUW quality quorum.",
    )
    p_poi.add_argument("--samples-file", default="", help="Optional JSON file of miner outputs.")
    p_poi.add_argument("--required-fields", default="decision,summary")
    p_poi.add_argument("--similarity-threshold", type=float, default=0.45)
    p_poi.add_argument("--max-latency-ms", type=int, default=8000)
    p_poi.set_defaults(func=_cmd_validate_poi)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
