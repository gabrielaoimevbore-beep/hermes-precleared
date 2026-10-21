"""Verdicts remembered between sends, kept in Hermes's per-plugin state (ctx.state).

One entry per address, keyed by a hash of the address so the file holds no
addresses. An entry is reused only for 30 days and only for the same facts:
changing the sender country, address source, pitch or prospect country
(or any fact a precleared_check call passed) means a fresh check.
Only definite verdicts are kept; unknown and failed checks are asked again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import client

logger = logging.getLogger(__name__)

STATE_KEY = "verdicts"
CACHE_VERSION = 1
MAX_AGE_SECONDS = 30 * 24 * 3600
MAX_ENTRIES = 2000
FACT_FIELDS = (
    "sender_country", "address_source", "pitch", "default_recipient_country",
    "recipient_country", "company_number", "existing_customer", "pitch_relevant",
)


@dataclass
class Cached:
    verdict: str
    reason: str
    receipt_id: str


def fingerprint(body: dict[str, Any]) -> str:
    """A short hash of the facts in a check request, apart from the address."""
    facts = {name: body.get(name) for name in FACT_FIELDS}
    facts["v"] = CACHE_VERSION
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def address_key(email: str) -> str:
    return "a" + hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:23]


class VerdictCache:
    def __init__(self, state: Any, clock: Callable[[], float] = time.time) -> None:
        self._state = state
        self._clock = clock

    def _load(self) -> dict[str, Any]:
        try:
            data = self._state.get(STATE_KEY, {})
        except Exception:  # noqa: BLE001 - unreadable state behaves like an empty cache
            logger.warning("precleared: could not read the verdict cache", exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def lookup(self, emails: list[str], fp: str) -> dict[str, Cached]:
        """Fresh entries for these addresses made with the same facts."""
        if not emails:
            return {}
        data = self._load()
        now = self._clock()
        found: dict[str, Cached] = {}
        for email in emails:
            entry = data.get(address_key(email))
            if not isinstance(entry, dict) or entry.get("k") != fp:
                continue
            checked = entry.get("t")
            if not isinstance(checked, (int, float)) or not 0 <= now - checked <= MAX_AGE_SECONDS:
                continue
            verdict = entry.get("v")
            if verdict not in client.DEFINITE_VERDICTS:
                continue
            found[email] = Cached(verdict, client.clean_text(entry.get("r")), client.clean_text(entry.get("id")))
        return found

    def store(self, results: dict[str, Cached], fp: str) -> None:
        """Remember definite verdicts, dropping stale entries and the oldest beyond the cap."""
        keep = {e: r for e, r in results.items() if r.verdict in client.DEFINITE_VERDICTS}
        if not keep:
            return
        data = self._load()
        now = self._clock()
        for email, result in keep.items():
            data[address_key(email)] = {
                "v": result.verdict, "r": result.reason[:300], "id": result.receipt_id[:40],
                "t": int(now), "k": fp,
            }
        fresh = {
            key: entry for key, entry in data.items()
            if isinstance(entry, dict) and isinstance(entry.get("t"), (int, float))
            and 0 <= now - entry["t"] <= MAX_AGE_SECONDS
        }
        if len(fresh) > MAX_ENTRIES:
            newest = sorted(fresh.items(), key=lambda item: item[1]["t"], reverse=True)[:MAX_ENTRIES]
            fresh = dict(newest)
        try:
            self._state.set(STATE_KEY, fresh)
        except Exception:  # noqa: BLE001 - caching is best effort
            logger.warning("precleared: could not save the verdict cache", exc_info=True)
