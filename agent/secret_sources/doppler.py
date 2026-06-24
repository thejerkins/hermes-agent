"""Doppler secret source (`doppler` CLI) integration.

Hermes pulls API keys from Doppler at process startup so they don't have
to live in plaintext in ``~/.hermes/.env``.  This mirrors the Bitwarden
Secrets Manager integration (``agent.secret_sources.bitwarden``) but is
much simpler: the ``doppler`` CLI is assumed to be installed already
(``brew install dopplerhq/cli/doppler`` / package manager), so there is
no binary download/verify machinery.

Design summary
--------------

* The service token is the one bootstrap secret.  It is read from the env
  var named by ``secrets.doppler.token_env`` (default ``DOPPLER_TOKEN``)
  if set, otherwise from a 0600 file at ``secrets.doppler.token_file``.
  The token_file path keeps the bootstrap secret out of ``.env`` and out
  of the launchd plist (which regenerates from a template), so the whole
  setup survives ``hermes setup`` / plist regeneration.
* Pulling secrets is a single ``doppler secrets download --no-file
  --format json --project <p> --config <c>`` call, authenticated via the
  ``DOPPLER_TOKEN`` env var.  Results are cached in-process and on disk
  for ``cache_ttl_seconds`` so back-to-back ``hermes`` invocations don't
  hammer the API.
* Failures NEVER block Hermes startup.  Missing CLI, no network, expired
  token, etc. all emit a one-line warning and continue with whatever
  credentials ``.env`` / the shell already had.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# How long to wait for the doppler subprocess, in seconds.
_DOPPLER_RUN_TIMEOUT = 30

# In-process cache so repeated load_hermes_dotenv() calls (CLI startup,
# gateway hot-reload, test suites) don't re-fetch from Doppler.
_CacheKey = Tuple[str, str, str]  # (token_fingerprint, project, config)
_CACHE: Dict[_CacheKey, "_CachedFetch"] = {}

# Disk-persisted cache so back-to-back CLI invocations (scripts, cron, the
# gateway forking new agents) don't each pay the `doppler secrets download`
# tax. Holds only secret VALUES, never the service token. Written 0600.
_DISK_CACHE_BASENAME = "doppler_cache.json"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Outcome of a single Doppler pull."""

    secrets: Dict[str, str] = field(default_factory=dict)
    applied: List[str] = field(default_factory=list)   # set into os.environ
    skipped: List[str] = field(default_factory=list)   # already set, not overridden
    warnings: List[str] = field(default_factory=list)  # non-fatal issues
    error: Optional[str] = None                        # fatal: nothing was fetched

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class _CachedFetch:
    secrets: Dict[str, str]
    fetched_at: float

    def is_fresh(self, ttl_seconds: float) -> bool:
        if ttl_seconds <= 0:
            return False
        return (time.time() - self.fetched_at) < ttl_seconds


# ---------------------------------------------------------------------------
# Token resolution + binary discovery
# ---------------------------------------------------------------------------


def find_doppler() -> Optional[Path]:
    """Return a path to the ``doppler`` CLI, or None if not on PATH."""
    system = shutil.which("doppler")
    return Path(system) if system else None


def resolve_token(token_env: str = "DOPPLER_TOKEN",
                  token_file: str = "") -> str:
    """Resolve the Doppler service token.

    Prefers the env var ``token_env`` when set; otherwise reads the first
    non-empty line of ``token_file`` (a 0600 file kept out of .env).
    Returns "" when neither yields a token.
    """
    env_token = os.environ.get(token_env, "").strip()
    if env_token:
        return env_token
    if token_file:
        try:
            return Path(token_file).expanduser().read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            return ""
    return ""


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _is_valid_env_name(name: str) -> bool:
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(c.isalnum() or c == "_" for c in name)


# ---------------------------------------------------------------------------
# Disk cache (best-effort, 0600)
# ---------------------------------------------------------------------------


def _hermes_home(home_path: Optional[Path] = None) -> Path:
    if home_path is None:
        home_path = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    return home_path


def _disk_cache_path(home_path: Optional[Path] = None) -> Path:
    return _hermes_home(home_path) / "cache" / _DISK_CACHE_BASENAME


def _doppler_config_dir(home_path: Optional[Path] = None) -> Path:
    """A dedicated, Hermes-owned doppler CLI config directory.

    macOS doppler stores tokens in the login keychain and reads its
    keychain-backed config during init — which fails (exit 36) in a
    non-GUI / launchd context where the keychain is locked, EVEN when a
    token is supplied.  Pointing ``--config-dir`` at a fresh dir with no
    configured scope sidesteps the keychain entirely: doppler then relies
    purely on the ``DOPPLER_TOKEN`` we pass in the env.
    """
    return _hermes_home(home_path) / "cache" / "doppler-cli-config"


def _cache_key_str(cache_key: _CacheKey) -> str:
    token_fp, project, config = cache_key
    return f"{token_fp}|{project}|{config}"


def _read_disk_cache(cache_key: _CacheKey, ttl_seconds: float,
                     home_path: Optional[Path] = None) -> Optional["_CachedFetch"]:
    if ttl_seconds <= 0:
        return None
    path = _disk_cache_path(home_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("key") != _cache_key_str(cache_key):
        return None
    secrets = payload.get("secrets")
    fetched_at = payload.get("fetched_at")
    if not isinstance(secrets, dict) or not isinstance(fetched_at, (int, float)):
        return None
    typed_secrets: Dict[str, str] = {
        k: v for k, v in secrets.items()
        if isinstance(k, str) and isinstance(v, str)
    }
    entry = _CachedFetch(secrets=typed_secrets, fetched_at=float(fetched_at))
    if not entry.is_fresh(ttl_seconds):
        return None
    return entry


def _write_disk_cache(cache_key: _CacheKey, entry: "_CachedFetch",
                      home_path: Optional[Path] = None) -> None:
    path = _disk_cache_path(home_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": _cache_key_str(cache_key),
            "secrets": entry.secrets,
            "fetched_at": entry.fetched_at,
        }
        fd, tmp = tempfile.mkstemp(
            prefix=".doppler_cache_", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass  # best-effort


# ---------------------------------------------------------------------------
# Secret fetch
# ---------------------------------------------------------------------------


def fetch_doppler_secrets(
    *,
    token: str,
    project: str,
    config: str,
    cache_ttl_seconds: float = 300,
    use_cache: bool = True,
    disk_cache: bool = False,
    home_path: Optional[Path] = None,
    binary: Optional[Path] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """Pull the secrets for ``project``/``config`` from Doppler.

    Returns ``(secrets_dict, warnings_list)``.  Raises :class:`RuntimeError`
    for fatal conditions (missing CLI, auth failure, unparseable output);
    callers in the env_loader path catch this and emit a single warning.
    """
    if not token:
        raise RuntimeError("Doppler service token is empty")
    if not project or not config:
        raise RuntimeError("Doppler project/config is empty")

    cache_key = (_token_fingerprint(token), project, config)
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached and cached.is_fresh(cache_ttl_seconds):
            return cached.secrets, []
        # Disk cache is opt-in: it persists secret VALUES to disk (0600),
        # which conflicts with a "no plaintext secret file" policy.  Off by
        # default — in-process caching still applies within each process.
        if disk_cache:
            disk_cached = _read_disk_cache(cache_key, cache_ttl_seconds, home_path)
            if disk_cached is not None:
                _CACHE[cache_key] = disk_cached
                return disk_cached.secrets, []

    doppler = binary or find_doppler()
    if doppler is None:
        raise RuntimeError(
            "doppler CLI not found on PATH — install it from "
            "https://docs.doppler.com/docs/install-cli"
        )

    secrets, warnings = _run_doppler_download(
        doppler, token, project, config, _doppler_config_dir(home_path)
    )
    entry = _CachedFetch(secrets=secrets, fetched_at=time.time())
    _CACHE[cache_key] = entry
    if use_cache and disk_cache:
        _write_disk_cache(cache_key, entry, home_path)
    return secrets, warnings


def _run_doppler_download(
    doppler: Path, token: str, project: str, config: str,
    config_dir: Optional[Path] = None,
) -> Tuple[Dict[str, str], List[str]]:
    cmd = [
        str(doppler), "secrets", "download",
        "--no-file", "--format", "json",
        "--project", project, "--config", config,
        "--no-check-version",
    ]
    if config_dir is not None:
        # See _doppler_config_dir(): keeps doppler off the macOS keychain.
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        cmd.extend(["--config-dir", str(config_dir)])
    env = os.environ.copy()
    env["DOPPLER_TOKEN"] = token
    env.setdefault("NO_COLOR", "1")

    try:
        proc = subprocess.run(  # noqa: S603 — doppler path is trusted
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_DOPPLER_RUN_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"doppler timed out after {_DOPPLER_RUN_TIMEOUT}s fetching secrets"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"failed to invoke doppler: {exc}") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().replace("\x1b", "")
        raise RuntimeError(f"doppler exited {proc.returncode}: {err[:200]}")

    raw = proc.stdout.strip()
    if not raw:
        return {}, ["doppler returned no output (empty config?)"]

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"doppler returned non-JSON output: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"doppler returned unexpected shape: {type(payload).__name__}"
        )

    secrets: Dict[str, str] = {}
    warnings: List[str] = []
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if not _is_valid_env_name(key):
            warnings.append(f"Skipping secret {key!r}: not a valid env-var name")
            continue
        secrets[key] = value
    return secrets, warnings


# ---------------------------------------------------------------------------
# Public entry point — called from hermes_cli.env_loader
# ---------------------------------------------------------------------------


def apply_doppler_secrets(
    *,
    enabled: bool,
    token_env: str = "DOPPLER_TOKEN",
    token_file: str = "",
    project: str = "",
    config: str = "",
    override_existing: bool = False,
    cache_ttl_seconds: float = 300,
    disk_cache: bool = False,
    home_path: Optional[Path] = None,
) -> FetchResult:
    """Pull secrets from Doppler and set them on ``os.environ``.

    This is the function ``load_hermes_dotenv()`` calls after the .env
    files have loaded.  It is intentionally defensive — any failure
    returns a :class:`FetchResult` with ``error`` set; it never raises.

    Parameters mirror the ``secrets.doppler.*`` config keys so the caller
    can just splat the dict in.
    """
    result = FetchResult()

    if not enabled:
        return result

    token = resolve_token(token_env=token_env, token_file=token_file)
    if not token:
        where = f"{token_env} or secrets.doppler.token_file"
        result.error = (
            f"secrets.doppler.enabled is true but no token found in {where}."
        )
        return result

    if not project or not config:
        result.error = (
            "secrets.doppler.project / secrets.doppler.config is empty."
        )
        return result

    if find_doppler() is None:
        result.error = (
            "doppler CLI not found on PATH.  Install it from "
            "https://docs.doppler.com/docs/install-cli"
        )
        return result

    try:
        secrets, warnings = fetch_doppler_secrets(
            token=token,
            project=project,
            config=config,
            cache_ttl_seconds=cache_ttl_seconds,
            disk_cache=disk_cache,
            home_path=home_path,
        )
    except RuntimeError as exc:
        result.error = str(exc)
        return result

    result.secrets = secrets
    result.warnings.extend(warnings)

    for key, value in secrets.items():
        if key == token_env:
            # Never let Doppler clobber the very token we used to fetch.
            result.skipped.append(key)
            continue
        if not override_existing and os.environ.get(key):
            result.skipped.append(key)
            continue
        os.environ[key] = value
        result.applied.append(key)

    return result
