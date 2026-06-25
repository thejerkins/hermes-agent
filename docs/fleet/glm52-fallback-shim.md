# Barry GLM-5.2 fallback shim

Barry fleet shim for preserving Hermes fallback endpoint compatibility while routing the fallback rung to Z.AI GLM-5.2 Coding Plan.

## Contract

Keep the local OpenAI-compatible endpoint stable:

```text
http://127.0.0.1:11536/v1
```

Hermes config keeps the existing provider key for compatibility:

```yaml
providers:
  kimi-coding-shim:
    name: GLM-5.2 Coding Plan via local fallback shim
    base_url: http://127.0.0.1:11536/v1
    api_key: no-key-required
    api_mode: chat_completions
    default_model: glm-5.2
    models:
      glm-5.2:
        context_length: 1048576
    context_length: 1048576
    discover_models: true
fallback_providers:
- provider: custom:kimi-coding-shim
  model: glm-5.2
  base_url: http://127.0.0.1:11536/v1
  api_key: no-key-required
```

## Runtime

The Mac Mini launch agent may still be named `ai.hermes.kimi-coding-shim` during the transition. That is intentional compatibility, not current model identity.

Launch command used live:

```text
/opt/homebrew/bin/doppler run --project hermes --config dev_personal -- /usr/bin/python3 /Users/jerkins/.hermes/kimi-coding-shim/kimi_coding_shim.py
```

Secrets come from Doppler. Do not commit `ZAI_API_KEY`, `GLM_API_KEY`, or token values.

## Compatibility behavior

The shim rewrites legacy `model: kimi-for-coding` requests to `model: glm-5.2` so older running Hermes processes keep working during the migration window.

## Install/update live shim

```bash
install -m 0755 scripts/fleet/glm52_fallback_shim.py /Users/jerkins/.hermes/kimi-coding-shim/kimi_coding_shim.py
plutil -lint /Users/jerkins/Library/LaunchAgents/ai.hermes.kimi-coding-shim.plist
launchctl bootout gui/$(id -u)/ai.hermes.kimi-coding-shim || true
launchctl bootstrap gui/$(id -u) /Users/jerkins/Library/LaunchAgents/ai.hermes.kimi-coding-shim.plist
launchctl kickstart -k gui/$(id -u)/ai.hermes.kimi-coding-shim
```

This restarts only the fallback shim. It must not restart the live Hermes Discord gateway.

## Verification

```bash
curl -fsS http://127.0.0.1:11536/v1/models
python -c "import pytest; raise SystemExit(pytest.main(['tests/fleet/test_glm52_fallback_shim.py','-q','-o','addopts=']))"
hermes chat -Q --provider custom:kimi-coding-shim --model glm-5.2 --toolsets '' -q 'Reply exactly: glm fallback live'
hermes chat -Q --toolsets '' -q 'Reply exactly: codex primary live'
```

Expected:

- `/v1/models` includes `glm-5.2`.
- Direct custom-provider smoke returns exact requested text.
- Primary Codex smoke still returns exact requested text.
- `launchctl print gui/$(id -u)/ai.hermes.gateway` still shows the Hermes gateway running as bare Python.
