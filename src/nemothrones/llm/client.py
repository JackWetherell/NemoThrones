"""The role-routed LLM client.

Each *role* (gm, narrator, agent, ...) maps to a primary backend plus fallbacks.
Every call: budget check -> backend -> JSON extraction -> pydantic validation ->
one repair retry -> next backend in the chain. A small circuit breaker skips a
backend after repeated failures (e.g. Hermes down), so the game stays responsive.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings
from .backends import BackendError, HttpChatBackend, MockBackend
from .budget import Budget, BudgetExceeded
from .jsonutil import extract_json

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    pass


def prompt_hash(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:16]


class LLM:
    def __init__(self, settings: Settings, *, log_sink: Callable[[dict], None] | None = None,
                 backends: dict | None = None):
        self.settings = settings
        cfg = settings.llm
        self.budget = Budget(cfg.price_in_per_m, cfg.price_out_per_m,
                             settings.budget.session_warn_usd, settings.budget.session_hard_usd)
        self._backends: dict = dict(backends or {})
        self._sem = asyncio.Semaphore(max(1, cfg.concurrency))
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self.log_sink = log_sink
        self.last_backend: dict[str, str] = {}
        self.notices: list[str] = []

    # ---------------------------------------------------------------- backends
    def backend(self, name: str):
        if name not in self._backends:
            bcfg = self.settings.llm.backends.get(name)
            if bcfg is None:
                raise LLMUnavailable(f"unknown backend {name!r}")
            if bcfg.kind == "mock":
                self._backends[name] = MockBackend(name=name)
            else:
                self._backends[name] = HttpChatBackend(name, bcfg, self.settings.home)
        return self._backends[name]

    def chain(self, role: str) -> list[str]:
        rc = self.settings.llm.roles.get(role) or self.settings.llm.roles["agent"]
        out = [rc.backend] + [b for b in rc.fallback if b != rc.backend]
        return out

    def _breaker_open(self, name: str) -> bool:
        return time.monotonic() < self._open_until.get(name, 0.0)

    def _record_failure(self, name: str, err: str) -> None:
        n = self._failures.get(name, 0) + 1
        self._failures[name] = n
        if n >= self.settings.llm.breaker_failures:
            self._open_until[name] = time.monotonic() + self.settings.llm.breaker_cooldown_s
            self._failures[name] = 0
            self.notices.append(f"LLM backend '{name}' is failing ({err[:120]}); "
                                f"falling back for {int(self.settings.llm.breaker_cooldown_s)}s.")

    # -------------------------------------------------------------------- call
    async def call(self, role: str, system: str, user: str, schema: type[T], *,
                   agent_id: str | None = None, max_tokens: int | None = None,
                   temperature: float | None = None) -> T:
        rc = self.settings.llm.roles.get(role) or self.settings.llm.roles["agent"]
        self.budget.check(role)
        base_msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        errors: list[str] = []
        chain = self.chain(role)
        for i, name in enumerate(chain):
            if self._breaker_open(name) and i < len(chain) - 1:
                errors.append(f"{name}: circuit open")
                continue
            backend = self.backend(name)
            msgs = list(base_msgs)
            for _attempt in range(2):
                t0 = time.time()
                try:
                    async with self._sem:
                        comp = await backend.complete(
                            msgs, role=role, json_mode=True,
                            max_tokens=max_tokens or rc.max_tokens,
                            temperature=rc.temperature if temperature is None else temperature)
                except BackendError as e:
                    errors.append(str(e))
                    self._log(role, name, agent_id, msgs, "", 0, 0, time.time() - t0, False, str(e))
                    self._record_failure(name, str(e))
                    break
                cost = self.budget.charge(role, comp.tokens_in, comp.tokens_out)
                try:
                    obj = schema.model_validate(extract_json(comp.text))
                except (ValueError, ValidationError) as e:
                    err = _short_error(e)
                    errors.append(f"{name}: invalid output ({err})")
                    self._log(role, name, agent_id, msgs, comp.text, comp.tokens_in, comp.tokens_out,
                              comp.latency, False, f"invalid: {err}", cost)
                    msgs = msgs + [
                        {"role": "assistant", "content": comp.text[:6000]},
                        {"role": "user", "content": f"That reply was not valid ({err}). Reply with ONLY the corrected JSON object."},
                    ]
                    continue
                self._log(role, name, agent_id, msgs, comp.text, comp.tokens_in, comp.tokens_out,
                          comp.latency, True, None, cost)
                self._failures[name] = 0
                self.last_backend[role] = name
                return obj
            else:
                self._record_failure(name, errors[-1] if errors else "invalid output")
        raise LLMUnavailable(f"role {role!r} failed on {chain}: " + " | ".join(errors[-3:]))

    def _log(self, role, backend, agent_id, msgs, response, tin, tout, latency, ok, error, cost=0.0):
        if not self.log_sink:
            return
        self.log_sink({
            "ts": time.time(), "role": role, "backend": backend, "agent_id": agent_id,
            "prompt_hash": prompt_hash(msgs[:2]), "request": json.dumps(msgs, ensure_ascii=False),
            "response": response, "tokens_in": tin, "tokens_out": tout, "cost": cost,
            "latency": latency, "ok": ok, "error": error,
        })

    async def aclose(self) -> None:
        for b in self._backends.values():
            await b.aclose()


def _short_error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors()[:3]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            parts.append(f"{loc}: {err.get('msg')}")
        return "; ".join(parts)
    return str(e)[:200]


__all__ = ["LLM", "LLMUnavailable", "BudgetExceeded", "prompt_hash"]
