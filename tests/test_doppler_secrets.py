"""Tests for the Doppler secret source (agent.secret_sources.doppler)."""

import json
import subprocess

import pytest

from agent.secret_sources import doppler


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch, tmp_path):
    # Isolate every test: empty in-process cache + a throwaway HERMES_HOME
    # so disk-cache reads/writes never touch the real one.
    doppler._CACHE.clear()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield
    doppler._CACHE.clear()


def _fake_download(payload):
    """Return a subprocess.run stub that yields ``payload`` as JSON stdout."""
    def _run(cmd, **kwargs):
        return _FakeProc(returncode=0, stdout=json.dumps(payload))
    return _run


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    result = doppler.apply_doppler_secrets(enabled=False)
    assert result.applied == []
    assert result.error is None


def test_missing_token_errors(monkeypatch):
    monkeypatch.delenv("DOPPLER_TOKEN", raising=False)
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c"
    )
    assert result.applied == []
    assert "token" in (result.error or "")


def test_missing_cli_errors(monkeypatch):
    monkeypatch.setenv("DOPPLER_TOKEN", "tok")
    monkeypatch.setattr(doppler, "find_doppler", lambda: None)
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c"
    )
    assert "doppler CLI not found" in (result.error or "")


def test_applies_secrets_from_env_token(monkeypatch):
    monkeypatch.setenv("DOPPLER_TOKEN", "tok")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    monkeypatch.setattr(
        subprocess, "run",
        _fake_download({"DISCORD_BOT_TOKEN": "abc", "DISCORD_HOME_CHANNEL": "123"}),
    )
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c", cache_ttl_seconds=0
    )
    assert result.error is None
    assert set(result.applied) == {"DISCORD_BOT_TOKEN", "DISCORD_HOME_CHANNEL"}
    import os
    assert os.environ["DISCORD_BOT_TOKEN"] == "abc"


def test_reads_token_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DOPPLER_TOKEN", raising=False)
    tok_file = tmp_path / "doppler_token"
    tok_file.write_text("file-tok\n")
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    captured = {}

    def _run(cmd, **kwargs):
        captured["token"] = kwargs["env"]["DOPPLER_TOKEN"]
        return _FakeProc(returncode=0, stdout=json.dumps({"K_API_KEY": "v"}))

    monkeypatch.setattr(subprocess, "run", _run)
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c",
        token_file=str(tok_file), cache_ttl_seconds=0,
    )
    assert result.error is None
    assert captured["token"] == "file-tok"
    assert "K_API_KEY" in result.applied


def test_non_override_skips_existing(monkeypatch):
    monkeypatch.setenv("DOPPLER_TOKEN", "tok")
    monkeypatch.setenv("EXISTING_KEY", "keep-me")
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    monkeypatch.setattr(
        subprocess, "run", _fake_download({"EXISTING_KEY": "new"})
    )
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c",
        override_existing=False, cache_ttl_seconds=0,
    )
    import os
    assert os.environ["EXISTING_KEY"] == "keep-me"
    assert "EXISTING_KEY" in result.skipped


def test_skips_invalid_env_names(monkeypatch):
    monkeypatch.setenv("DOPPLER_TOKEN", "tok")
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    monkeypatch.setattr(
        subprocess, "run",
        _fake_download({"GOOD_KEY": "v", "bad-key": "v", "1BAD": "v"}),
    )
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c", cache_ttl_seconds=0
    )
    assert result.applied == ["GOOD_KEY"]
    assert any("bad-key" in w for w in result.warnings)


def test_token_env_never_clobbered(monkeypatch):
    monkeypatch.setenv("DOPPLER_TOKEN", "tok")
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    monkeypatch.setattr(
        subprocess, "run",
        _fake_download({"DOPPLER_TOKEN": "leaked", "OK_KEY": "v"}),
    )
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c", cache_ttl_seconds=0
    )
    import os
    assert os.environ["DOPPLER_TOKEN"] == "tok"
    assert "DOPPLER_TOKEN" in result.skipped
    assert "OK_KEY" in result.applied


def test_disk_cache_off_by_default_writes_no_file(monkeypatch, tmp_path):
    # HERMES_HOME is tmp_path (fixture). With disk_cache off (default) and a
    # live TTL, no plaintext secret file should land on disk.
    monkeypatch.setenv("DOPPLER_TOKEN", "tok")
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    monkeypatch.setattr(subprocess, "run", _fake_download({"K_API_KEY": "v"}))
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c", cache_ttl_seconds=300,
    )
    assert "K_API_KEY" in result.applied
    assert not doppler._disk_cache_path(tmp_path).exists()


def test_disk_cache_on_writes_file(monkeypatch, tmp_path):
    monkeypatch.setenv("DOPPLER_TOKEN", "tok")
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")
    monkeypatch.setattr(subprocess, "run", _fake_download({"K_API_KEY": "v"}))
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c",
        cache_ttl_seconds=300, disk_cache=True,
    )
    assert "K_API_KEY" in result.applied
    assert doppler._disk_cache_path(tmp_path).exists()


def test_cli_failure_returns_error(monkeypatch):
    monkeypatch.setenv("DOPPLER_TOKEN", "tok")
    monkeypatch.setattr(doppler, "find_doppler", lambda: "doppler")

    def _run(cmd, **kwargs):
        return _FakeProc(returncode=1, stderr="auth failed")

    monkeypatch.setattr(subprocess, "run", _run)
    result = doppler.apply_doppler_secrets(
        enabled=True, project="p", config="c", cache_ttl_seconds=0
    )
    assert "auth failed" in (result.error or "")
    assert result.applied == []
