---
id: cortensor-gate
name: Cortensor Triage and Release Gate
version: 0.1.0
description: Run deterministic diff hygiene, test-skip budget, smart-contract safety, and Cortensor session checks to produce a typed release verdict.
tags: [cortensor, triage, code-quality, smart-contracts, diagnostics]
tools:
  - inspect_diff_code_quality
  - verify_test_skip_and_complexity_budget
  - diagnose_cortensor_session_posture
  - stage_release_gate_tag
risk: medium
when_to_use:
  - "A pull request or release candidate needs CODE_QUALITY.md and smart-contract verification"
  - "A Cortensor builder wants to validate Router session_id pools or Portal configuration before running agents"
---

# Purpose

Evaluate repository diffs, test suites, and Cortensor provider posture through deterministic OpenMinion tools, bounded subagents, and operator-approved handoffs.

# Procedure

1. Run `inspect_diff_code_quality` and `verify_test_skip_and_complexity_budget` inside a bounded read-only subagent to produce a `DiffQualityReport`.
2. Run `diagnose_cortensor_session_posture` to verify `OpenMinionConfig` and `CORTENSOR_*` environment alignment across `openai_chat` and `cortensor_completion` modes.
3. Delegate security-sensitive findings via `Handoff` to `security_auditor` under explicit operator `approval_callback` control.
4. Synthesize a schema-validated `ReleaseReadinessVerdict` and optionally stage a release receipt via `stage_release_gate_tag` when approved.

# Checks

1. Every finding references concrete file paths, line numbers, or config keys.
2. Unconditional `skip`/`xfail` additions and high-severity contract hazards always block release readiness.
3. Portable `MemoryBundle` exports contain zero secrets or local host credentials.

# Failure & Recovery

1. If the configured LLM provider is unreachable, report the exact provider error code and remediation command.
2. If operator approval is denied or paused, preserve turn state without writing release receipts.
