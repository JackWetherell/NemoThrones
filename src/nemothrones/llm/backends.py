"""LLM backends. All real endpoints speak OpenAI-style /chat/completions:

* ``hermes``           Hermes Agent API inside the sandbox (runs its agent loop + tools)
* ``inference_local``  OpenShell privacy router (credentials injected, model rewritten)
* ``openrouter_direct`` host-side development only
* ``mock`` / replay    in-process, deterministic
"""

from __future__ import annotations

import os
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import BackendConfig


@dataclass
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    backend: str
    latency: float


class BackendError(RuntimeError):
    pass


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _ssl_context(extra_ca: str | None) -> ssl.SSLContext:
    # System store (honours SSL_CERT_FILE / SSL_CERT_DIR), so a sandbox proxy CA works.
    ctx = ssl.create_default_context()
    for path in (extra_ca, os.environ.get("NEMOTHRONES_CA_BUNDLE")):
        if path and Path(path).is_file():
            ctx.load_verify_locations(path)
    return ctx


def resolve_api_key(cfg: BackendConfig, home: Path) -> str | None:
    if cfg.api_key_env and os.environ.get(cfg.api_key_env):
        return os.environ[cfg.api_key_env]
    if cfg.api_key_file:
        p = Path(cfg.api_key_file)
        if not p.is_absolute():
            p = home / p
        if p.is_file():
            return p.read_text().strip() or None
    if cfg.base_url.startswith("http://127.0.0.1:8642") or cfg.base_url.startswith("http://localhost:8642"):
        # Inside the sandbox Hermes keeps its API key in its own .env.
        env = Path("/sandbox/.hermes/.env")
        if env.is_file():
            for line in env.read_text().splitlines():
                if line.strip().startswith("API_SERVER_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"") or None
    return None


class HttpChatBackend:
    def __init__(self, name: str, cfg: BackendConfig, home: Path):
        self.name = name
        self.cfg = cfg
        self.api_key = resolve_api_key(cfg, home)
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            verify: ssl.SSLContext | bool = True
            if self.cfg.base_url.startswith("https://"):
                verify = _ssl_context(self.cfg.ca_bundle)
            self._client = httpx.AsyncClient(timeout=self.cfg.timeout_s, verify=verify, trust_env=True)
        return self._client

    async def complete(self, messages: list[dict], *, role: str, json_mode: bool = True,
                       max_tokens: int = 1200, temperature: float = 0.8) -> Completion:
        headers = {"Content-Type": "application/json", "X-Title": "NemoThrones"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict = {"model": self.cfg.model, "messages": messages,
                         "max_tokens": max_tokens, "temperature": temperature, "stream": False}
        if json_mode and self.cfg.json_mode:
            payload["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        try:
            r = await self._http().post(self.cfg.base_url.rstrip("/") + "/chat/completions",
                                        json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise BackendError(f"{self.name}: {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise BackendError(f"{self.name}: HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise BackendError(f"{self.name}: malformed response: {r.text[:300]}") from e
        usage = data.get("usage") or {}
        prompt_text = "".join(m.get("content", "") for m in messages)
        return Completion(
            text=text,
            tokens_in=int(usage.get("prompt_tokens") or estimate_tokens(prompt_text)),
            tokens_out=int(usage.get("completion_tokens") or estimate_tokens(text)),
            model=str(data.get("model") or self.cfg.model),
            backend=self.name,
            latency=time.monotonic() - t0,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


Responder = Callable[[str, list[dict]], str]


class MockBackend:
    """Deterministic in-process backend. ``responder(role, messages) -> text``."""

    def __init__(self, responder: Responder | None = None, name: str = "mock"):
        from .mock import default_responder

        self.name = name
        self.responder = responder or default_responder
        self.calls: list[tuple[str, list[dict]]] = []

    async def complete(self, messages: list[dict], *, role: str, json_mode: bool = True,
                       max_tokens: int = 1200, temperature: float = 0.8) -> Completion:
        self.calls.append((role, messages))
        text = self.responder(role, messages)
        prompt_text = "".join(m.get("content", "") for m in messages)
        return Completion(text, estimate_tokens(prompt_text), estimate_tokens(text), "mock", self.name, 0.0)

    async def aclose(self) -> None:
        return None


class ReplayBackend:
    """Replays responses recorded in a save's ``llm_log`` (matched by role+prompt hash, then order)."""

    def __init__(self, rows: list[dict], name: str = "replay"):
        self.name = name
        self.by_hash: dict[tuple[str, str], list[str]] = {}
        self.by_role: dict[str, list[str]] = {}
        for r in rows:
            if not r.get("ok"):
                continue
            self.by_hash.setdefault((r["role"], r["prompt_hash"]), []).append(r["response"])
            self.by_role.setdefault(r["role"], []).append(r["response"])

    async def complete(self, messages: list[dict], *, role: str, json_mode: bool = True,
                       max_tokens: int = 1200, temperature: float = 0.8) -> Completion:
        from .client import prompt_hash

        h = prompt_hash(messages[:2])
        queue = self.by_hash.get((role, h)) or self.by_role.get(role)
        if not queue:
            raise BackendError(f"replay: no recorded response for role {role!r}")
        text = queue.pop(0)
        return Completion(text, 0, 0, "replay", self.name, 0.0)

    async def aclose(self) -> None:
        return None
