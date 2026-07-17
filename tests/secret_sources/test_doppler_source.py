from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.secret_sources import registry as reg
from agent.secret_sources.base import ErrorKind
from agent.secret_sources.doppler import DopplerSource


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    reg._reset_registry_for_tests()
    monkeypatch.setattr(reg, "_ensure_builtin_sources", lambda: None)
    yield
    reg._reset_registry_for_tests()


def test_identity_and_bootstrap_protection():
    source = DopplerSource()
    assert source.name == "doppler"
    assert source.label == "Doppler"
    assert source.shape == "bulk"
    assert source.override_existing({}) is True
    assert source.protected_env_vars({}) == frozenset({"DOPPLER_TOKEN"})
    assert source.protected_env_vars({"token_env": "CUSTOM_TOKEN"}) == frozenset(
        {"CUSTOM_TOKEN"}
    )


def test_missing_token_is_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("DOPPLER_TOKEN", raising=False)
    result = DopplerSource().fetch(
        {"enabled": True, "project": "hermes", "config": "dev"}, tmp_path
    )
    assert result.error_kind is ErrorKind.NOT_CONFIGURED
    assert "DOPPLER_TOKEN" in (result.error or "")


def test_fetch_uses_file_bootstrap_token_and_returns_project_secrets(tmp_path, monkeypatch):
    import agent.secret_sources.doppler as doppler

    token_file = tmp_path / "doppler-token"
    token_file.write_text("bootstrap-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    binary = tmp_path / "doppler"
    binary.write_text("", encoding="utf-8")
    seen = {}

    monkeypatch.setattr(doppler, "find_doppler", lambda: binary)

    def fake_fetch(**kwargs):
        seen.update(kwargs)
        return {"DISCORD_BOT_TOKEN": "bot", "API_SERVER_KEY": "api"}, ["notice"]

    monkeypatch.setattr(doppler, "fetch_doppler_secrets", fake_fetch)
    result = DopplerSource().fetch(
        {
            "enabled": True,
            "token_file": str(token_file),
            "project": "hermes",
            "config": "dev_personal",
            "cache_ttl_seconds": 0,
        },
        tmp_path,
    )

    assert result.ok
    assert result.secrets == {"DISCORD_BOT_TOKEN": "bot", "API_SERVER_KEY": "api"}
    assert result.warnings == ["notice"]
    assert seen["token"] == "bootstrap-token"
    assert seen["project"] == "hermes"
    assert seen["config"] == "dev_personal"
    assert seen["home_path"] == tmp_path


def test_registry_applies_doppler_but_protects_bootstrap_token(tmp_path, monkeypatch):
    import agent.secret_sources.doppler as doppler

    monkeypatch.setenv("DOPPLER_TOKEN", "bootstrap-token")
    monkeypatch.setattr(doppler, "find_doppler", lambda: Path("/fake/doppler"))
    monkeypatch.setattr(
        doppler,
        "fetch_doppler_secrets",
        lambda **kwargs: ({"DISCORD_BOT_TOKEN": "bot", "DOPPLER_TOKEN": "replace-me"}, []),
    )
    assert reg.register_source(DopplerSource())

    env = {"DOPPLER_TOKEN": "bootstrap-token"}
    report = reg.apply_all(
        {
            "doppler": {
                "enabled": True,
                "project": "hermes",
                "config": "dev_personal",
                "override_existing": True,
            }
        },
        tmp_path,
        environ=env,
    )

    assert env == {"DOPPLER_TOKEN": "bootstrap-token", "DISCORD_BOT_TOKEN": "bot"}
    assert report.provenance["DISCORD_BOT_TOKEN"].source == "doppler"
    assert report.sources[0].skipped_protected == ["DOPPLER_TOKEN"]


def test_child_invocation_has_only_bootstrap_token(monkeypatch, tmp_path):
    import agent.secret_sources.doppler as doppler

    observed = {}

    def fake_run(argv, *, extra_env, timeout):
        observed.update({"argv": argv, "extra_env": extra_env, "timeout": timeout})
        return SimpleNamespace(returncode=0, stdout='{"DISCORD_BOT_TOKEN":"bot"}', stderr="")

    monkeypatch.setattr(doppler, "run_secret_cli", fake_run)
    secrets, warnings = doppler._run_doppler_download(
        Path("/fake/doppler"), "bootstrap-token", "hermes", "dev_personal", tmp_path
    )

    assert secrets == {"DISCORD_BOT_TOKEN": "bot"}
    assert warnings == []
    assert observed["extra_env"] == {"DOPPLER_TOKEN": "bootstrap-token"}
    assert "--config-dir" in observed["argv"]
