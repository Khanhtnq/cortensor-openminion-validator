"""Repro 06: probe_provider_session ignores CORTENSOR_* env overrides + chat drops HTTP pool."""

from __future__ import annotations

import inspect
import os
from unittest.mock import patch

from openminion import OpenMinionConfig
from openminion.modules.llm.providers.cortensor.adapter import CortensorProvider
from openminion.modules.llm.providers.cortensor.config import resolve_cortensor_runtime_config
from openminion.services.health.probes import probe_provider_session


def main() -> int:
    cfg = OpenMinionConfig()
    cfg.providers.cortensor.session_id = 0
    env_router = {"CORTENSOR_SESSION_ID": "42"}
    env_portal = {
        "CORTENSOR_API_MODE": "openai_chat",
        "CORTENSOR_API_URL": "https://api.cortensor.app/v1",
    }

    with patch.dict(os.environ, env_router, clear=False):
        resolved_router = resolve_cortensor_runtime_config(cfg)
        probe_router = probe_provider_session(cfg, "cortensor")
    with patch.dict(os.environ, env_portal, clear=False):
        resolved_portal = resolve_cortensor_runtime_config(cfg)
        probe_portal = probe_provider_session(cfg, "cortensor")

    chat_src = inspect.getsource(CortensorProvider._complete_via_openai_chat_mode)
    print(
        "EXPECTED: probe_router='ok' (session 42), probe_portal=None, chat mode passes http_client="
    )
    r_stat = probe_router.status if probe_router else None
    p_stat = probe_portal.status if probe_portal else None
    print(
        f"ACTUAL:   resolved_router.session_id={resolved_router.session_id} vs "
        f"probe_router={r_stat}; resolved_portal.api_mode={resolved_portal.api_mode!r} "
        f"vs probe_portal={p_stat}; chat_passes_http_client={'http_client=' in chat_src}"
    )
    assert probe_router is not None and probe_router.status == "fail"
    assert probe_portal is not None and probe_portal.status == "fail"
    assert "http_client=" not in chat_src
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
