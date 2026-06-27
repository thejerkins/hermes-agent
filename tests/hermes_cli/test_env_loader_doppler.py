from __future__ import annotations

from types import SimpleNamespace


def test_load_dotenv_applies_doppler_source(tmp_path, monkeypatch):
    from hermes_cli import env_loader

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "secrets:\n"
        "  doppler:\n"
        "    enabled: true\n"
        "    token_file: /tmp/doppler-token\n"
        "    project: hermes\n"
        "    config: dev_personal\n"
        "    override_existing: true\n",
        encoding="utf-8",
    )
    calls = []

    def fake_apply(**kwargs):
        calls.append(kwargs)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "from-doppler")
        return SimpleNamespace(applied=["DISCORD_BOT_TOKEN"], error=None, warnings=[])

    monkeypatch.setattr(
        "agent.secret_sources.doppler.apply_doppler_secrets",
        fake_apply,
    )
    env_loader.reset_secret_source_cache()
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    env_loader.load_hermes_dotenv(hermes_home=home)

    assert calls == [
        {
            "enabled": True,
            "token_env": "DOPPLER_TOKEN",
            "token_file": "/tmp/doppler-token",
            "project": "hermes",
            "config": "dev_personal",
            "override_existing": True,
            "cache_ttl_seconds": 300.0,
            "disk_cache": False,
            "home_path": home,
        }
    ]
    assert env_loader.get_secret_source("DISCORD_BOT_TOKEN") == "doppler"
    assert env_loader.format_secret_source_suffix("DISCORD_BOT_TOKEN") == " (from Doppler)"


def test_load_dotenv_applies_bitwarden_and_doppler(tmp_path, monkeypatch):
    from hermes_cli import env_loader

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: bw-project\n"
        "  doppler:\n"
        "    enabled: true\n"
        "    project: hermes\n"
        "    config: dev\n",
        encoding="utf-8",
    )
    calls = []

    def fake_bw(**kwargs):
        calls.append(("bitwarden", kwargs["project_id"]))
        monkeypatch.setenv("BW_KEY", "from-bw")
        return SimpleNamespace(applied=["BW_KEY"], error=None, warnings=[])

    def fake_doppler(**kwargs):
        calls.append(("doppler", kwargs["project"], kwargs["config"]))
        monkeypatch.setenv("DOPPLER_KEY", "from-doppler")
        return SimpleNamespace(applied=["DOPPLER_KEY"], error=None, warnings=[])

    monkeypatch.setattr("agent.secret_sources.bitwarden.apply_bitwarden_secrets", fake_bw)
    monkeypatch.setattr("agent.secret_sources.doppler.apply_doppler_secrets", fake_doppler)
    env_loader.reset_secret_source_cache()

    env_loader.load_hermes_dotenv(hermes_home=home)

    assert calls == [("bitwarden", "bw-project"), ("doppler", "hermes", "dev")]
    assert env_loader.get_secret_source("BW_KEY") == "bitwarden"
    assert env_loader.get_secret_source("DOPPLER_KEY") == "doppler"
