# What We Learned — Actionable OpenMinion SDK Feedback & Reproducible Bug Reports

While building **Cortensor Sentinel** on `openminion==0.1.20` (`dev @ 28217999e1942993ab4e448fb9f70401ee287d49`), we stress-tested multi-agent composition (`Agent` + `@tool` + `subagent` + `Handoff` + `output_type` + `CortensorProvider` diagnostics) end-to-end.

Below are **6 reproducible upstream bugs** uncovered during development (each paired with a deterministic, zero-network `<60`-line script in `reproducers/` and automated assertions in `tests/test_openminion_building_blocks_and_findings.py`), plus **2 developer-experience design observations** for maintainers.

---

## Summary of Reproducible Upstream Findings (`v0.1.20` / `dev @ 2821799`)

| ID | Component & Exact Location | Severity | Reproducer Script | Upstream Patch |
|---|---|---|---|---|
| **BUG-01** | `src/openminion/api/handoff.py:168-175` (`build_delegate_tool`) | High (Crash) | `reproducers/repro_01_handoff_basemodel_crash.py` | `patches/01_handoff_basemodel_output_serialization.patch` |
| **BUG-02** | `src/openminion/modules/tool/runtime/registry_toolspec.py:182-246` (`_tool_result_from_payload`) | High (Crash + Corrupt JSON) | `reproducers/repro_02_tool_basemodel_return_crash.py` | `patches/02_tool_basemodel_return_serialization.patch` |
| **BUG-03** | `src/openminion/modules/tool/runtime/registry_toolspec.py:98-100` (`_validated_tool_arguments`) | Medium (Silent Data Corruption) | `reproducers/repro_03_explicit_none_arg_clobbered.py` | `patches/03_preserve_explicit_none_tool_arguments.patch` |
| **BUG-04** | `src/openminion/api/handoff.py:293-301` (`subagent`) | Medium (Wrong Profile/Model Routing) | `reproducers/repro_04_subagent_drops_agent_id_and_model.py` | `patches/04_subagent_inherit_agent_id_and_model.patch` |
| **BUG-05** | `src/openminion/api/agent.py:259-277, 442-455` (`_coerce_output` / `_extract_json_object`) | Medium (False Validation Failure) | `reproducers/repro_05_coerce_output_first_json_trap.py` | `patches/05_agent_coerce_output_multi_json_spans.patch` |
| **BUG-06** | `src/openminion/services/health/probes.py:164-196` & `providers/cortensor/adapter.py:261-279` | Medium (False `doctor`/`/health` Failure + Unpooled HTTP) | `reproducers/repro_06_cortensor_doctor_env_and_pool.py` | `patches/06_cortensor_doctor_env_and_http_pool_parity.patch` |

---

## Detailed Bug Reports

### BUG-01: `Handoff` targeting `Agent(output_type=BaseModel)` crashes the tool loop with `TypeError`
- **Affected Files:** `src/openminion/api/handoff.py:168-175`, `src/openminion/modules/tool/executor.py:56-72`, `src/openminion/modules/brain/loop/tools/messages.py:56-80`
- **Steps to Reproduce:**
  ```bash
  python reproducers/repro_01_handoff_basemodel_crash.py
  ```
  1. Define a specialist `Agent(name="auditor", output_type=AuditVerdict)` where `AuditVerdict` subclasses `pydantic.BaseModel`.
  2. Attach `Handoff(target=specialist)` to a coordinator `Agent` and invoke `transfer_to_auditor`.
- **Expected:** `transfer_to_auditor` serializes `result.output` into a JSON-compatible dictionary so `ToolExecutionBatch.to_metadata_payload()` and `action_result_to_tool_message()` succeed.
- **Actual:** `build_delegate_tool` stores the live `BaseModel` instance directly inside `data = {"output": result.output}`, raising `TypeError: Object of type AuditVerdict is not JSON serializable`.
- **Recommended Upstream Fix:** Normalize `result.output` via `result.output.model_dump(mode="json")` when `isinstance(result.output, BaseModel)` in `build_delegate_tool` (and normalize `BaseModel` / `dataclass` objects in `_tool_result_from_payload`).

---

### BUG-02: `@tool` functions returning a Pydantic `BaseModel` emit Python `repr` strings in `content` and crash `ToolExecutionBatch`
- **Affected Files:** `src/openminion/modules/tool/runtime/registry_toolspec.py:182-246`
- **Steps to Reproduce:**
  ```bash
  python reproducers/repro_02_tool_basemodel_return_crash.py
  ```
  1. Decorate a typed function `@tool def summarize_pr(pr_number: int) -> DiffSummary` returning a `BaseModel`.
  2. Execute the tool through `_tool_result_from_payload` and serialize `ToolExecutionBatch`.
- **Expected:** Pydantic `BaseModel` return values are converted via `.model_dump(mode="json")` so `content` contains valid JSON and `ToolExecutionResult.data` is JSON-serializable.
- **Actual:** `_tool_result_from_payload` wraps the model as `{"ok": True, "data": {"result": payload}}`, stringifies `content` using `json.dumps(..., default=str)` (`{"result": "severity='medium' files=42"}`), and leaves the raw `BaseModel` inside `result.data["result"]`, crashing `ToolExecutionBatch.to_metadata_payload()` with `TypeError`.
- **Recommended Upstream Fix:** Convert `BaseModel` payloads with `payload.model_dump(mode="json")` at the entry of `_tool_result_from_payload` (and recursively normalize nested `BaseModel` values inside mappings/lists).

---

### BUG-03: `_validated_tool_arguments` silently overwrites explicit `None` arguments with non-`None` defaults
- **Affected Files:** `src/openminion/modules/tool/runtime/registry_toolspec.py:98-100` & `src/openminion/tools/decorator.py:219-225`
- **Steps to Reproduce:**
  ```bash
  python reproducers/repro_03_explicit_none_arg_clobbered.py
  ```
  1. Define `@tool def list_pull_requests(repo: str, state: str | None = "open") -> str`.
  2. Call `_validated_tool_arguments` with `{"repo": "cortensor/openminion", "state": None}` and pass the validated dict to `decl.handler`.
- **Expected:** Explicit `state=None` is preserved (`state=None`), allowing callers/models to request unfiltered results.
- **Actual:** Line 99 calls `parsed.model_dump(exclude_none=True)`, dropping `"state": None` from the dictionary; `decorator.py:222` then re-validates the stripped dict and re-applies the default `"open"`.
- **Recommended Upstream Fix:** Preserve explicitly supplied `None` fields (`{k: v for k, v in parsed.model_dump().items() if v is not None or k in parsed.model_fields_set}`).

---

### BUG-04: `subagent(parent, ...)` drops `parent.agent_id` and `parent.model` when omitted
- **Affected Files:** `src/openminion/api/handoff.py:293-301` (also affects `examples/sdk/delegation.py:13-21` when invoked with `--mode subagent --agent <id>`)
- **Steps to Reproduce:**
  ```bash
  python reproducers/repro_04_subagent_drops_agent_id_and_model.py
  ```
  1. Create `parent = Agent(name="coordinator", agent_id="cortensor-reviewer", model="qwen2.5:7b-instruct", runtime=rt)`.
  2. Spawn `child = subagent(parent, name="diff-auditor", instructions="Audit diff.")` and call `child.run(...)`.
- **Expected:** `child` inherits `agent_id="cortensor-reviewer"` and `model="qwen2.5:7b-instruct"` unless explicitly overridden, matching `examples/sdk/delegation.py --mode subagent --agent <id>`.
- **Actual:** `subagent(...)` instantiates `Agent(...)` without `agent_id=parent.agent_id` and passes `model=None`, causing `child.run(...)` to omit `agent_id` and `override_model` and route to the runtime's default profile.
- **Recommended Upstream Fix:** Pass `agent_id=parent.agent_id` and `model=parent.model if model is None else model` inside `subagent(...)`.

---

### BUG-05: `Agent._coerce_output` / `_extract_json_object` only checks the first `{...}` span in prose
- **Affected Files:** `src/openminion/api/agent.py:259-277, 442-455`
- **Steps to Reproduce:**
  ```bash
  python reproducers/repro_05_coerce_output_first_json_trap.py
  ```
  1. Configure `Agent(output_type=GateVerdict)`.
  2. Have a model reply with brief context containing an inline JSON fragment before the final schema JSON, e.g. `'Inspected diff stats {"files": 2}. Final verdict: {"decision": "approve", "summary": "all checks clean"}'`.
- **Expected:** `_coerce_output` scans candidate balanced JSON object spans and returns the validated `GateVerdict(decision="approve", summary="all checks clean")`.
- **Actual:** `_extract_json_object` returns only the first `{...}` span (`{"files": 2}`), fails schema validation, and raises `AgentOutputValidationError` without checking subsequent valid JSON objects in the same reply.
- **Recommended Upstream Fix:** Iterate over all balanced JSON object spans (`_iter_json_objects(text)`) and validate candidates against `self.output_type` before raising `AgentOutputValidationError`.

---

### BUG-06: `probe_provider_session` ignores `CORTENSOR_*` runtime env overrides & Cortensor Portal chat mode bypasses HTTP connection pooling
- **Affected Files:** `src/openminion/services/health/probes.py:164-196, 318-342` and `src/openminion/modules/llm/providers/cortensor/adapter.py:261-279`
- **Steps to Reproduce:**
  ```bash
  python reproducers/repro_06_cortensor_doctor_env_and_pool.py
  ```
  1. Set `config.providers.cortensor.session_id = 0` in config while exporting `CORTENSOR_SESSION_ID=42` (Router) or `CORTENSOR_API_MODE=openai_chat` + `CORTENSOR_API_URL=https://api.cortensor.app/v1` (Portal).
  2. Compare `resolve_cortensor_runtime_config(config)` against `probe_provider_session(config, "cortensor")`.
- **Expected:** `probe_provider_session` honors the same `resolve_cortensor_runtime_config(config)` contract used by live turns (`ok` for Router session `42`, `None` for Portal `openai_chat`), and `CortensorProvider._complete_via_openai_chat_mode` passes `http_client=http_client_for_config(self._http_client, config)` just like `_complete_via_completion_mode` (`adapter.py:342`).
- **Actual:** `probe_provider_session` reads raw `config.providers.cortensor`, false-failing `openminion doctor` and `GET /health` in both setups; meanwhile `_complete_via_openai_chat_mode` omits `http_client=`, bypassing `ProviderHTTPClient` pooling on every Portal chat call.
- **Recommended Upstream Fix:** Call `resolve_cortensor_runtime_config(config)` inside `probe_provider_session` and pass `http_client=http_client_for_config(self._http_client, config)` in `_complete_via_openai_chat_mode`.

---

## Additional Builder Ergonomics Feedback

1. **Unify `status` Key Semantics Between `run_tool_spec` and `_tool_result_from_payload`:**
   - `_tool_result_from_payload` (`registry_toolspec.py:199`) treats a returned mapping's `"status"` field as successful unless `status in {"error", "denied", "timeout"}`.
   - By contrast, `run_tool_spec` (`modules/brain/adapters/tool/results.py:201-208`) treats any mapping containing `"status"` (without `"ok": True`) as `BRAIN_STATE_ERROR` unless `status in ("ok", "success", "running")`.
   - If a builder writes `@tool def check_node() -> dict: return {"status": "online"}` or `return {"status": "clean"}`, the Brain loop silently marks the tool call as `EXEC_ERROR` (`tool_errors += 1`). Aligning both paths (or only reserving `"ok": bool` as the envelope status signal for `@tool` user functions) removes a major surprise for new SDK adopters.
2. **Avoid Circular Import on `openminion.modules.tool.executor`:**
   - Importing `from openminion.modules.tool.executor import ToolExecutionBatch` directly before `openminion.modules.tool.registry` has been imported triggers a `partially initialized module` `ImportError` (`executor.py:19` -> `registry/__init__.py:12` -> `executor.py`). Breaking that cycle makes unit-testing custom tool batches much smoother.
