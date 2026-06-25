#!/usr/bin/env python3
"""OpenAI-compatible local fallback shim for Z.AI GLM Coding Plan.

This shim preserves Hermes' local fallback endpoint contract:

    http://127.0.0.1:11536/v1

It previously proxied Kimi Coding Plan. It now proxies Z.AI GLM-5.2 through the
Coding Plan OpenAI-compatible API so Hermes can keep using the same local
fallback URL while the fallback model changes.

Secrets are read from the environment at request time so Doppler rotations land.
Preferred env vars: ZAI_API_KEY, GLM_API_KEY, Z_AI_API_KEY.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOST = os.environ.get("GLM_SHIM_HOST", os.environ.get("KIMI_SHIM_HOST", "127.0.0.1"))
PORT = int(os.environ.get("GLM_SHIM_PORT", os.environ.get("KIMI_SHIM_PORT", "11536")))
UPSTREAM = os.environ.get(
    "GLM_SHIM_UPSTREAM",
    os.environ.get("ZAI_SHIM_UPSTREAM", "https://api.z.ai/api/coding/paas/v4"),
).rstrip("/")
DEFAULT_MODEL = os.environ.get("GLM_SHIM_MODEL", "glm-5.2")
TIMEOUT = float(os.environ.get("GLM_SHIM_TIMEOUT", os.environ.get("KIMI_SHIM_TIMEOUT", "300")))


def _api_key() -> str:
    key = (
        os.environ.get("ZAI_API_KEY")
        or os.environ.get("GLM_API_KEY")
        or os.environ.get("Z_AI_API_KEY")
        or ""
    ).strip()
    if not key:
        raise RuntimeError("ZAI_API_KEY/GLM_API_KEY missing in shim env")
    return key


def _send_json(h: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(payload)))
    h.end_headers()
    h.wfile.write(payload)


def _forward(path: str, payload_bytes: bytes | None, content_type: str | None, *, stream: bool = False):
    req = urllib.request.Request(
        UPSTREAM + path,
        data=payload_bytes,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": content_type or "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        },
        method="POST" if payload_bytes is not None else "GET",
    )
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _normalize_chat_payload(raw: bytes) -> bytes:
    """Default missing legacy fallback model ids to GLM-5.2.

    Hermes config is being moved to glm-5.2, but keeping a compatibility map
    means an older process that still asks for kimi-for-coding gets the new
    backend instead of failing during the transition window.
    """
    if not raw:
        return raw
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    if isinstance(payload, dict):
        model = str(payload.get("model") or "").strip()
        if not model or model == "kimi-for-coding":
            payload["model"] = DEFAULT_MODEL
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return raw


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[glm-shim] " + (fmt % args) + "\n")
        sys.stdout.flush()

    def do_GET(self) -> None:
        if self.path.startswith("/v1/models"):
            try:
                resp = _forward("/models", None, None, stream=False)
                body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except urllib.error.HTTPError as e:
                body = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                _send_json(self, 502, {"error": {"message": str(e), "type": "shim_error"}})
            return
        _send_json(self, 404, {"error": {"message": "not_found", "type": "not_found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        stream = bool(isinstance(payload, dict) and payload.get("stream"))
        path = self.path
        if not path.startswith("/v1/"):
            _send_json(self, 404, {"error": {"message": "not_found", "type": "not_found"}})
            return
        upstream_path = path[len("/v1"):]  # /chat/completions, etc.
        forward_raw = _normalize_chat_payload(raw) if upstream_path == "/chat/completions" else raw
        try:
            resp = _forward(upstream_path, forward_raw, self.headers.get("Content-Type"), stream=stream)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            for k in ("Content-Type",):
                v = e.headers.get(k)
                if v:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as e:
            _send_json(self, 502, {"error": {"message": str(e), "type": "shim_error"}})
            return

        self.send_response(resp.status)
        ct = resp.headers.get("Content-Type", "application/json")
        self.send_header("Content-Type", ct)
        if stream and "event-stream" in ct.lower():
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                resp.close()
        else:
            body = resp.read()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main() -> None:
    print(f"[glm-shim] listening on http://{HOST}:{PORT} → {UPSTREAM} model={DEFAULT_MODEL}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
