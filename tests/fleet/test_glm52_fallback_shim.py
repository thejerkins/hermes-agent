from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "glm52_fallback_shim.py"
    spec = importlib.util.spec_from_file_location("glm52_fallback_shim", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rewrites_legacy_kimi_model_to_glm52():
    module = _load_module()
    raw = json.dumps({"model": "kimi-for-coding", "messages": []}).encode()

    rewritten = module._normalize_chat_payload(raw)

    assert json.loads(rewritten.decode())["model"] == "glm-5.2"


def test_preserves_explicit_glm52_model():
    module = _load_module()
    raw = json.dumps({"model": "glm-5.2", "messages": []}).encode()

    rewritten = module._normalize_chat_payload(raw)

    assert json.loads(rewritten.decode())["model"] == "glm-5.2"


def test_defaults_missing_model_to_glm52():
    module = _load_module()
    raw = json.dumps({"messages": []}).encode()

    rewritten = module._normalize_chat_payload(raw)

    assert json.loads(rewritten.decode())["model"] == "glm-5.2"


def test_api_key_prefers_zai_then_glm(monkeypatch):
    module = _load_module()
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.setenv("GLM_API_KEY", "glm-key")

    assert module._api_key() == "zai-key"


def test_api_key_falls_back_to_glm(monkeypatch):
    module = _load_module()
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setenv("GLM_API_KEY", "glm-key")

    assert module._api_key() == "glm-key"
