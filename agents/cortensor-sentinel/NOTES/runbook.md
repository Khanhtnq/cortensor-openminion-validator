# Cortensor Sentinel Runbook Notes

- Keep `CODE_QUALITY.md` ownership clear: one truthful owner per module, top-of-file explainers on source files, and zero generic junk-drawer `utils.py` modules.
- When switching from local `ollama` to `cortensor`, verify `CORTENSOR_API_MODE` (`openai_chat` for Portal vs `cortensor_completion` for Router) and pre-created `session_id` pools.
