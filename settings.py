"""The plugin's settings (plugins.entries.precleared.settings) and its API key."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import client

logger = logging.getLogger(__name__)

PLUGIN_ID = "precleared"
API_KEY_ENV = "PRECLEARED_API_KEY"
MODES = ("ask", "block", "off")
SET_COMMAND = f"hermes config set plugins.entries.{PLUGIN_ID}.settings.sender_country GB"


@dataclass
class Settings:
    sender_country: str = ""
    mode: str = "ask"
    address_source: str = "unknown"
    prospect_country: str = ""
    pitch: str = ""
    ignore_domains: list[str] = field(default_factory=list)
    extra_tools: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return self.sender_country in client.SENDER_COUNTRIES


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    """A list setting, accepting a list or a comma-separated string. Always a new list."""
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()][:200]


def read_settings(ctx: Any) -> Settings:
    """Read the settings, falling back to the defaults for anything missing or invalid."""

    def get(key: str, default: Any) -> Any:
        try:
            return ctx.get_config(key, default)
        except Exception:  # noqa: BLE001 - a broken config entry falls back to the default
            logger.debug("precleared: could not read setting %s", key, exc_info=True)
            return default

    raw_mode = get("mode", "ask")
    # YAML reads a bare `off` as false (and `on` as true): `hermes config set ... mode off` stores false.
    if isinstance(raw_mode, bool):
        raw_mode = "ask" if raw_mode else "off"
    mode = _text(raw_mode).lower() or "ask"
    if mode not in MODES:
        logger.warning("precleared: unknown mode %r, using 'ask'", mode)
        mode = "ask"
    sender = _text(get("sender_country", "")).upper()
    if sender == "UK":
        sender = "GB"
    source = _text(get("address_source", "unknown")).lower() or "unknown"
    if source not in client.ADDRESS_SOURCES:
        logger.warning("precleared: unknown address_source %r, using 'unknown'", source)
        source = "unknown"
    raw_prospect = get("prospect_country", "")
    # The same YAML rule turns Norway's code, NO, into false.
    prospect = "NO" if raw_prospect is False else _text(raw_prospect).upper()
    if prospect == "UK":
        prospect = "GB"
    if prospect and not client.COUNTRY_RE.match(prospect):
        logger.warning("precleared: prospect_country %r is not a two-letter code; ignoring it", prospect)
        prospect = ""
    return Settings(
        sender_country=sender,
        mode=mode,
        address_source=source,
        prospect_country=prospect,
        pitch=_text(get("pitch", ""))[:200],
        ignore_domains=_string_list(get("ignore_domains", [])),
        extra_tools=_string_list(get("extra_tools", [])),
    )


def read_api_key() -> str | None:
    """The user's Precleared key through Hermes's secret scope, or None (the free allowance)."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:  # pragma: no cover - always present inside Hermes
        return None
    try:
        value = get_secret(API_KEY_ENV)
    except Exception:  # noqa: BLE001 - no scope for this profile: check without a key
        logger.debug("precleared: no secret scope for %s", API_KEY_ENV, exc_info=True)
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
