"""Test helpers: load the plugin as a package, a fake Hermes context and a fake network.

No test touches the network or needs Hermes installed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import urllib.error
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "hermes_precleared"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_package()

from hermes_precleared import cache, client, gate, hooks, schemas, settings, tools  # noqa: E402

RECEIPT = "cc_rcpt_0123456789ABCDEF"


class FakeState:
    """Like Hermes's PluginState: JSON values under validated keys."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.writes = 0

    def get(self, key: str, default: Any = None) -> Any:
        assert isinstance(key, str) and 0 < len(key) <= 128 and "@" not in key
        value = self.data.get(key)
        return default if value is None else json.loads(json.dumps(value))

    def set(self, key: str, value: Any) -> None:
        assert isinstance(key, str) and 0 < len(key) <= 128 and "@" not in key
        self.data[key] = json.loads(json.dumps(value))
        self.writes += 1


class FakeCtx:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.state = FakeState()
        self.tools: dict[str, dict[str, Any]] = {}
        self.hooks: dict[str, list[Callable]] = {}
        self.skills: dict[str, tuple[Path, str]] = {}

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler,
                            "description": description, "override": override}

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)

    def register_skill(self, name, path, description=""):
        self.skills[name] = (Path(path), description)


class FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Stands in for urllib.request.urlopen. `answer(body, request)` returns (status, payload)
    or raises; statuses of 400 and above are raised as HTTPError like urllib does."""

    def __init__(self, answer: Callable[[dict | None, Any], tuple[int, Any]]) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        with self._lock:
            self.requests.append({
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": body,
                "timeout": timeout,
            })
        status, payload = self.answer(body, request)
        if status >= 400:
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            raise urllib.error.HTTPError(request.full_url, status, "error", hdrs=None, fp=io.BytesIO(raw))
        return FakeResponse(status, payload)


def verdict_payload(verdict: str = "ok_to_email", receipt: str = RECEIPT, **extra: Any) -> dict[str, Any]:
    """A response shaped like a real /api/v1/check answer."""
    payload = {
        "verdict": verdict,
        "confidence": "high",
        "reasons": [
            {
                "rule_key": "gb_individual_consent",
                "text": "The recipient is a sole trader, so unsolicited marketing email needs prior consent.",
                "source_title": "PECR 2003 regulation 22(2)",
                "source_url": "https://www.legislation.gov.uk/uksi/2003/2426/regulation/22",
            },
            {"note": "Country (GB) read from the company's website."},
        ],
        "rules_applied": ["gb_individual_consent"],
        "footer_text": "Say who is sending, give a postal address and a free way to opt out." if verdict == "ok_to_email" else None,
        "next_step": "Do not cold email. Use a route they offer, such as their contact form." if verdict != "ok_to_email" else "Send, with the footer.",
        "facts": {"recipient_country": "GB", "legal_form": "individual"},
        "receipt": {"id": receipt, "verify_url": f"https://precleared.dev/api/v1/receipt?id={receipt}"},
        "rules_version": "v1-draft",
        "usage": {"free_remaining_today": 9, "credits_remaining": 0},
        "disclaimer": "Decision support with sources, not legal advice.",
    }
    payload.update(extra)
    return payload


def by_email(verdicts: dict[str, str]) -> Callable:
    """An answer function giving each address its verdict (default ok_to_email)."""

    def answer(body, request):
        email = (body or {}).get("recipient_email", "")
        verdict = verdicts.get(email, "ok_to_email")
        receipt = "cc_rcpt_" + (str(abs(hash(email)) % 10**16)).zfill(16)
        return 200, verdict_payload(verdict, receipt=receipt)

    return answer


@pytest.fixture
def ctx() -> FakeCtx:
    return FakeCtx({"sender_country": "GB", "address_source": "published", "pitch": "bookkeeping software"})


@pytest.fixture
def make_hook():
    def make(ctx: FakeCtx, answer: Callable, *, key: str | None = None, clock=None):
        opener = FakeOpener(answer)
        kwargs = {"opener": opener, "api_key_reader": lambda: key}
        if clock is not None:
            kwargs["clock"] = clock
        return hooks.make_pre_tool_call(ctx, **kwargs), opener

    return make
