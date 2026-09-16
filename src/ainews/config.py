"""Config loading: YAML + ${ENV_VAR} expansion."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Raised when the config file is missing or malformed."""


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=value lines, # comments, optional quotes.

    Existing environment variables always win, so CI secrets are not clobbered
    by a stray local file.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in strings."""
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            resolved = os.environ.get(name)
            if resolved is None or resolved == "":
                if default is None:
                    log.debug("environment variable %s is unset", name)
                    return ""
                return default
            return resolved

        return _ENV_PATTERN.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


@dataclass
class HttpConfig:
    user_agent: str = (
        "ainews-digest/0.1 (personal weekly AI news digest; "
        "+https://github.com/jhard99/Test)"
    )
    timeout: int = 30
    requests_per_minute: int = 20
    respect_robots: bool = True
    max_retries: int = 3
    # Only these domains get cookies from the cookie jar, and only these are
    # allowed to be fetched with subscription credentials attached.
    cookie_domains: list[str] = field(default_factory=list)
    cookies_file: str = ""


@dataclass
class LLMConfig:
    # Where Claude is reached: anthropic (first-party API or an `ant auth login`
    # profile), bedrock, vertex, foundry. The cloud providers authenticate with
    # that cloud's own credentials - no Anthropic API key involved.
    provider: str = "anthropic"
    # Set false to run with no model access at all: keyword triage instead.
    enabled: bool = True
    aws_region: str = "us-east-1"
    vertex_project: str = ""
    vertex_region: str = "global"
    foundry_resource: str = ""

    model: str = "claude-opus-5"
    # Triage/clustering run over every candidate item. Point this at a cheaper
    # model (e.g. claude-sonnet-5 or claude-haiku-4-5) to cut cost; the digest
    # itself is always written by `model`.
    triage_model: str = "claude-opus-5"
    triage_effort: str = "low"
    writer_effort: str = "high"
    triage_batch_size: int = 40
    max_output_tokens: int = 32000


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    use_tls: bool = True  # STARTTLS on 587; port 465 switches to implicit SSL
    subject_template: str = "AI weekly - {start} to {end}"

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender and self.recipients)


@dataclass
class SourceConfig:
    type: str
    name: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    window_days: int = 7
    max_items_per_source: int = 80
    max_stories: int = 12
    per_item_chars: int = 8000  # body budget handed to the writer per item
    min_importance: int = 2  # drop triaged items below this before clustering
    output_dir: Path = Path("digests")
    state_db: Path = Path("state/ainews.sqlite3")
    http: HttpConfig = field(default_factory=HttpConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    sources: list[SourceConfig] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.is_file():
            raise ConfigError(
                f"Config not found: {path}. Copy config.example.yaml to {path} and edit it."
            )
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Could not parse {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
        return cls.from_dict(expand_env(raw))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        cfg = cls()
        for key in (
            "window_days",
            "max_items_per_source",
            "max_stories",
            "per_item_chars",
            "min_importance",
        ):
            if key in raw:
                setattr(cfg, key, int(raw[key]))
        if "output_dir" in raw:
            cfg.output_dir = Path(str(raw["output_dir"]))
        if "state_db" in raw:
            cfg.state_db = Path(str(raw["state_db"]))

        cfg.http = _build(HttpConfig, raw.get("http") or {})
        cfg.llm = _build(LLMConfig, raw.get("llm") or {})
        cfg.smtp = _build(SmtpConfig, raw.get("delivery", {}).get("smtp") or {})
        if isinstance(cfg.smtp.recipients, str):
            cfg.smtp.recipients = [r.strip() for r in cfg.smtp.recipients.split(",") if r.strip()]
        cfg.smtp.recipients = [r for r in cfg.smtp.recipients if r]

        if not cfg.http.cookies_file:
            cfg.http.cookies_file = os.environ.get("NEWS_COOKIES_FILE", "")

        sources = raw.get("sources") or []
        if not isinstance(sources, list):
            raise ConfigError("`sources` must be a list.")
        for entry in sources:
            if not isinstance(entry, dict):
                raise ConfigError(f"Each source must be a mapping, got: {entry!r}")
            options = dict(entry)
            stype = str(options.pop("type", "")).strip()
            if not stype:
                raise ConfigError(f"Source is missing `type`: {entry!r}")
            name = str(options.pop("name", "") or stype)
            enabled = bool(options.pop("enabled", True))
            cfg.sources.append(SourceConfig(type=stype, name=name, enabled=enabled, options=options))
        return cfg


def _build(dc_type: type, values: dict[str, Any]):
    """Instantiate a dataclass from a dict, ignoring unknown keys."""
    fields = {f.name: f for f in dc_type.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if key not in fields:
            log.warning("ignoring unknown config key %r in %s", key, dc_type.__name__)
            continue
        if value is None:
            continue
        annotation = fields[key].type
        if annotation is int or annotation == "int":
            value = int(value)
        elif annotation is bool or annotation == "bool":
            value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        kwargs[key] = value
    return dc_type(**kwargs)
