"""Settings: defaults, an optional YAML file, and environment overrides.

Resolution order (later wins): built-in defaults -> ``<home>/nemothrones.yaml`` ->
``NEMOTHRONES_*`` environment variables -> command-line flags.

Inside the OpenShell sandbox the home defaults to
``/sandbox/.hermes/workspace/nemothrones`` because only ``/sandbox/.hermes`` survives
a sandbox rebuild (and ``hermes --backup`` copies the workspace).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

SANDBOX_WORKSPACE = Path("/sandbox/.hermes/workspace")


def default_home() -> Path:
    env = os.environ.get("NEMOTHRONES_HOME")
    if env:
        return Path(env).expanduser()
    if SANDBOX_WORKSPACE.is_dir():
        return SANDBOX_WORKSPACE / "nemothrones"
    return Path.home() / ".nemothrones"


class BackendConfig(BaseModel):
    kind: Literal["http", "mock"] = "http"
    base_url: str = ""
    model: str = "deepseek/deepseek-v4.1-flash"
    api_key_env: str | None = None  # name of an env var holding the key
    api_key_file: str | None = None  # path to a file holding the key
    timeout_s: float = 90.0
    json_mode: bool = True  # send response_format={"type":"json_object"}
    ca_bundle: str | None = None  # extra CA bundle (e.g. the OpenShell proxy CA)


class RoleConfig(BaseModel):
    backend: str
    fallback: list[str] = Field(default_factory=list)
    max_tokens: int = 1200
    temperature: float = 0.8


def _default_backends() -> dict[str, BackendConfig]:
    return {
        # Hermes Agent's OpenAI-compatible API inside the sandbox (the GM's brain).
        "hermes": BackendConfig(
            base_url="http://127.0.0.1:8642/v1",
            model="hermes-agent",
            api_key_env="NEMOTHRONES_HERMES_TOKEN",
            api_key_file="hermes_api_token",  # relative to home
            timeout_s=120.0,
            json_mode=False,
        ),
        # OpenShell's privacy router: injects the OpenRouter key, rewrites the model.
        "inference_local": BackendConfig(
            base_url="https://inference.local/v1",
            model="deepseek/deepseek-v4.1-flash",
            timeout_s=90.0,
        ),
        # Host-side development only. Never use inside the sandbox (no keys there).
        "openrouter_direct": BackendConfig(
            base_url="https://openrouter.ai/api/v1",
            model="deepseek/deepseek-v4.1-flash",
            api_key_env="OPENROUTER_API_KEY",
        ),
        "mock": BackendConfig(kind="mock", base_url="", model="mock"),
    }


def _default_roles() -> dict[str, RoleConfig]:
    gm = ["inference_local"]
    return {
        "gm": RoleConfig(backend="hermes", fallback=gm, max_tokens=1400, temperature=0.6),
        "director": RoleConfig(backend="hermes", fallback=gm, max_tokens=900, temperature=0.7),
        "lore_deep": RoleConfig(backend="hermes", fallback=gm, max_tokens=1200, temperature=0.3),
        "narrator": RoleConfig(backend="inference_local", max_tokens=2200, temperature=0.85),
        "agent": RoleConfig(backend="inference_local", max_tokens=700, temperature=0.9),
        "background": RoleConfig(backend="inference_local", max_tokens=700, temperature=0.8),
        "batch": RoleConfig(backend="inference_local", max_tokens=2400, temperature=0.8),
        "exchange": RoleConfig(backend="inference_local", max_tokens=700, temperature=0.85),
        "chronicler": RoleConfig(backend="inference_local", max_tokens=1600, temperature=0.7),
        "dossier": RoleConfig(backend="inference_local", max_tokens=2600, temperature=0.3),
        "fate": RoleConfig(backend="inference_local", max_tokens=2000, temperature=0.2),
        "slicer": RoleConfig(backend="inference_local", max_tokens=800, temperature=0.0),
    }


class LLMConfig(BaseModel):
    backends: dict[str, BackendConfig] = Field(default_factory=_default_backends)
    roles: dict[str, RoleConfig] = Field(default_factory=_default_roles)
    concurrency: int = 12
    # DeepSeek v4.1 Flash on OpenRouter, USD per million tokens.
    price_in_per_m: float = 0.14
    price_out_per_m: float = 0.42
    # After this many consecutive failures a backend is skipped for breaker_cooldown_s.
    breaker_failures: int = 2
    breaker_cooldown_s: float = 600.0


class BudgetConfig(BaseModel):
    session_warn_usd: float = 2.0
    session_hard_usd: float = 5.0


class GameConfig(BaseModel):
    canon_gravity: float = 0.6  # 0 sandbox ... 0.6 butterfly ... 1.0 fated
    odds_display: Literal["exact", "fuzzy", "off"] = "exact"
    verbosity: Literal["terse", "standard", "lush"] = "standard"
    cast_size: int = 100
    tier1_size: int = 25
    tier1_cadence_min: int = 24 * 60
    tier2_cadence_min: int = 7 * 24 * 60
    batch_size: int = 8
    max_speakers: int = 3
    lethal_confirm_threshold: float = 0.05
    idle_turns_for_hook: int = 3
    ironman: bool = True
    wrap: int = 92
    color: bool = True


class LoreConfig(BaseModel):
    api_url: str = "https://awoiaf.westeros.org/api.php"
    page_url: str = "https://awoiaf.westeros.org/index.php/"
    contact: str = ""  # optional; appended to the User-Agent only if you set it
    rate_limit_s: float = 1.0
    max_age_days: float = 30.0
    timeout_s: float = 30.0
    batch_titles: int = 20
    dossier_concurrency: int = 6


class Settings(BaseModel):
    home: Path = Field(default_factory=default_home)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    game: GameConfig = Field(default_factory=GameConfig)
    lore: LoreConfig = Field(default_factory=LoreConfig)
    dev: bool = False

    @property
    def saves_dir(self) -> Path:
        return self.home / "saves"

    @property
    def lore_db(self) -> Path:
        return self.home / "lore.db"

    @property
    def active_pointer(self) -> Path:
        return self.home / "active_save"

    def ensure_dirs(self) -> None:
        self.saves_dir.mkdir(parents=True, exist_ok=True)

    def force_backend(self, name: str) -> None:
        """Route every role to one backend (e.g. ``mock`` for tests)."""
        for role in self.llm.roles.values():
            role.backend = name
            role.fallback = []


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(home: Path | None = None, config_file: Path | None = None) -> Settings:
    base = Settings() if home is None else Settings(home=home)
    path = config_file or (base.home / "nemothrones.yaml")
    data = base.model_dump(mode="json")
    if path.is_file():
        user = yaml.safe_load(path.read_text()) or {}
        data = _deep_merge(data, user)
    env_gravity = os.environ.get("NEMOTHRONES_CANON_GRAVITY")
    if env_gravity:
        data["game"]["canon_gravity"] = float(env_gravity)
    if os.environ.get("NO_COLOR"):
        data["game"]["color"] = False
    return Settings.model_validate(data)
