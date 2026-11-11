# OpenMinion Building Blocks Tested — Cortensor Sentinel

This document maps every OpenMinion (`v0.1.20` / `dev @ 2821799`) building block exercised in **Cortensor Sentinel**, what worked cleanly in multi-step builder workflows, and where edge cases surfaced during integration.

---

## Summary Matrix (9 Building Blocks Exercised)

| # | Building Block | OpenMinion Surface | Where Used in `Cortensor Sentinel` | Outcome |
|---:|---|---|---|---|
| 1 | **Shared Application Runtime** | `APIRuntime.from_config_path(..., home_root=..., data_root=...)` | `src/cortensor_sentinel/workflow.py` | **Worked cleanly** across `ollama`, `cortensor`, and `echo` providers with single-process SQLite storage isolation. |
| 2 | **Typed Declarative `Agent` + `output_type`** | `Agent(..., output_type=ReleaseReadinessVerdict)` & `AgentOutputValidationError` | `src/cortensor_sentinel/workflow.py`, `models.py` | **Worked cleanly** for pure JSON replies; uncovered **Finding #5** when prose contains an inline `{...}` snippet prior to the final schema object. |
| 3 | **Custom `@tool` Functions & Risk Scopes** | `@tool(min_scope=..., idempotent=..., dangerous=True, tags=...)` | `src/cortensor_sentinel/tools.py` | **Worked cleanly** with `dict` envelopes (`{"ok": True, ...}`); uncovered **Finding #2** (`BaseModel` returns crash `ToolExecutionBatch`), **Finding #3** (`exclude_none=True` overwrites explicit `None`), and a subtle `status` key trap in `run_tool_spec`. |
| 4 | **Bounded `subagent` + Delegated Memory** | `subagent(parent, timeout_seconds=..., memory=DelegatedMemoryReadRequest(...))` | `src/cortensor_sentinel/workflow.py`, `sdk_compat.py` | **Worked cleanly** for single-use `PolicyCtl` `delegated_read` grants and automatic post-turn grant revocation; uncovered **Finding #4** (`subagent` drops `parent.agent_id` and `parent.model`). |
| 5 | **Peer `Handoff` + Operator Approval Gate** | `Handoff(target=..., name="transfer_to_security_auditor")` + `approval_callback` | `src/cortensor_sentinel/workflow.py`, `sdk_compat.py` | **Worked cleanly** across `allow_once`, `deny`, and `pause` (`policy_confirmation_prompt`) flows when the target returns `str`; uncovered **Finding #1** when the target uses `output_type=BaseModel`. |
| 6 | **Durable `session_id` + `thread_id` Continuity** | `Agent(..., session_id=...)` & `agent.run(..., thread_id=...)` | `src/cortensor_sentinel/workflow.py` | **Worked cleanly** — multi-turn follow-up retained recorded release gate state while an isolated control `session_id` remained uncontaminated. |
| 7 | **Portable `MemoryBundle` Lifecycle** | `MemoryBundle`, `export_bundle`, `save_bundle`, `load_bundle`, `import_bundle(trust_mode="candidate")` | `src/cortensor_sentinel/workflow.py` | **Worked cleanly** — deterministic sorted-key JSON serialization and `direct` / `candidate` trust-mode validation. |
| 8 | **Identity & Skill Bundle (`AGENT.md` + `SOUL.md` + `SKILL.md`)** | `load_identity_bundle("cortensor-sentinel", root=...)` | `agents/cortensor-sentinel/` | **Worked cleanly** — SHA-256 document fingerprinting verified `AGENT.md`, `SOUL.md`, `SKILLS/cortensor-gate/SKILL.md`, and `NOTES/runbook.md` (`bundle.ok == True`). |
| 9 | **Runtime Self-Inspection & Diagnostics** | `runtime.runtime_posture()`, `runtime.capability_report()`, `runtime.tool_inventory_report(readiness=True)` + `CortensorProvider` probes | `src/cortensor_sentinel/workflow.py`, `tools.py` | **Worked cleanly** for runtime/tool introspection; uncovered **Finding #6** in `probe_provider_session(config, "cortensor")` and `CortensorProvider._complete_via_openai_chat_mode`. |

---

## Detailed Notes by Building Block

### 1. Shared `APIRuntime` & Seamless Provider Switching (`ollama` / `cortensor` / `echo`)
- **What we tested:** Bootstrapping one application-owned `APIRuntime` from a generated `agents.json` with explicit `home_root` and `data_root` isolation, then running five distinct `Agent` instances (`triage_coordinator`, `diff_quality_subagent`, `escalation_router` -> `security_auditor`, `verdict_synthesizer`, `release_stager`, and `session_ledger_agent`) over the same runtime before deterministic `runtime.close()`.
- **What worked:**
  - Zero cross-agent tool leakage: temporary `@tool` and `Handoff` exposure profiles registered inside `Agent._register_tools_for_run` were cleanly unregistered in `finally` blocks after every turn.
  - Switching between local `$0` `ollama` (`qwen2.5:7b-instruct` / `qwen3.5:4b`), `cortensor` (`openai_chat` Portal or `cortensor_completion` Router `/api/v2/completions`), and offline `echo` required zero changes to agent or tool code.

### 2. Custom `@tool` Decorator & Policy Confirmation (`dangerous=True`)
- **What we tested:**
  - four `READ_ONLY`, `idempotent=True` tools (`inspect_diff_code_quality`, `verify_test_skip_and_complexity_budget`, `diagnose_cortensor_session_posture`, and `validate_cortensor_poi_pouw_quorum` for redundant Cortensor miner Proof-of-Inference / Proof-of-Useful-Work quorum scoring)
  - one `WRITE_SAFE`, `dangerous=True`, `idempotent=False` tool (`stage_release_gate_tag`) gated by `approval_callback`.
- **What worked:** Automatic Pydantic argument model synthesis (`_build_args_model`), automatic `ToolExposureProfile` activation (`user_tool_*`), and inline confirmation replay when `approval_callback` returns `True`.
- **What did not work out of the box:**
  1. Returning a Pydantic `BaseModel` directly from `@tool` stringifies via Python `repr` in `content` and crashes `ToolExecutionBatch.to_metadata_payload()` with `TypeError` (**Finding #2**).
  2. Passing an explicit `None` to an optional tool parameter with a non-`None` default (`state: str | None = "open"`) is silently overwritten back to `"open"` by `parsed.model_dump(exclude_none=True)` (**Finding #3**).
  3. Returning a plain `dict` containing a domain `"status"` key (such as `{"status": "clean", ...}`) without `"ok": True` causes `run_tool_spec` (`modules/brain/adapters/tool/results.py:199-208`) to classify the tool call as `EXEC_ERROR` because `_tool_result_status` only whitelists `("ok", "success", "running")`. Wrapping tool returns with `safe_tool_envelope(...)` (`{"ok": True, ...}`) resolved this completely.

### 3. Bounded `subagent(...)` & `DelegatedMemoryReadRequest`
- **What we tested:** Spawning `diff_quality_subagent` from `triage_coordinator` with `timeout_seconds`, an explicit `tools=[inspect_diff_code_quality]` allowlist, `output_type=DiffQualityReport`, and a bounded `DelegatedMemoryReadRequest` (`MemoryNamespace(agent_id="cortensor-sentinel")`, `max_results=10`, `max_context_tokens=1024`).
- **What worked:** `materialize_subagent_run_context` minted a single-use `PolicyGrantInput` (`max_uses=1`, `operations=["read"]`, `can_reshare=False`) on `runtime.action_policy` and revoked the grant immediately on turn exit.
- **What did not work out of the box:** Calling `subagent(parent, ...)` without repeating `model=` dropped both `parent.agent_id` and `parent.model` (**Finding #4**), routing child turns to the default profile instead of the parent's configured agent profile. Our `safe_subagent(...)` helper in `src/cortensor_sentinel/sdk_compat.py` preserves both fields.

### 4. Peer `Handoff` (`transfer_to_*`) & Operator Approval Callback
- **What we tested:** Routing high-severity Python/Solidity findings from `escalation_router` to `security_auditor` via `Handoff(target=security_specialist, name="transfer_to_security_auditor")` under `allow_once`, `deny`, and `pause` operator policies.
- **What worked:** `Handoff` automatically compiled into a temporary `WRITE_SAFE` tool family (`openminion.api.handoff.delegate`) and honored operator allow/deny/pause decisions cleanly.
- **What did not work out of the box:** Pointing `Handoff(target=...)` at an `Agent` configured with `output_type=BaseModel` placed the raw `BaseModel` instance into `data = {"output": result.output}`, crashing `ToolExecutionBatch.to_metadata_payload()` and `action_result_to_tool_message()` with `TypeError` (**Finding #1**). Our `safe_handoff(...)` adapter serializes `BaseModel` target outputs via `.model_dump(mode="json")`.

### 5. Cortensor Provider & Doctor Health Probes
- **What we tested:** `CortensorProviderConfig`, `resolve_cortensor_runtime_config`, `probe_provider_session(config, "cortensor")`, and `CortensorProvider` transport paths across both Cortensor Portal (`openai_chat`) and Cortensor Router (`cortensor_completion`) configurations.
- **What worked:** Runtime env overrides (`CORTENSOR_API_URL`, `CORTENSOR_API_MODE`, `CORTENSOR_SESSION_ID`, `CORTENSOR_SESSION_IDS`, `CORTENSOR_DEDICATED_SESSION_IDS`, `CORTENSOR_EPHEMERAL_SESSION_IDS`) resolved cleanly inside `resolve_cortensor_runtime_config`.
- **What did not work out of the box:** `probe_provider_session` (`services/health/probes.py:164-196`) inspected raw `config.providers.cortensor` without calling `resolve_cortensor_runtime_config(config)`, causing `openminion doctor` and `/health` to false-fail valid env-configured Router and Portal setups (**Finding #6**), while `_complete_via_openai_chat_mode` omitted `http_client=http_client_for_config(...)`.

---

## Reproducing All Verification Checks

```bash
# Run the full deterministic unit + building-block + reproducer test suite:
pytest -v

# Execute all 6 standalone OpenMinion v0.1.20 bug reproducers directly:
sentinel reproduce-bugs

# Run the live 9-building-block workflow against local Ollama ($0 API cost):
sentinel demo --live --provider ollama --model qwen2.5:7b-instruct
```
