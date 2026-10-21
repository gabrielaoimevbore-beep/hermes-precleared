"""A small HTTPS client for Precleared's REST API, using only the standard library.

Every request goes to the fixed host precleared.dev. The API key, when there is
one, is sent only in the Authorization header and is never logged or returned
in a message.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

BASE_URL = "https://precleared.dev"
CHECK_URL = f"{BASE_URL}/api/v1/check"
RECEIPT_URL = f"{BASE_URL}/api/v1/receipt"
GET_KEY_URL = f"{BASE_URL}/get-key"
BUY_URL = f"{BASE_URL}/buy"
RECEIPT_PAGE_URL = f"{BASE_URL}/r/"
USER_AGENT = "hermes-precleared/0.1.0 (+https://github.com/gabrielaoimevbore-beep/hermes-precleared)"

# Precleared answers within about 25 seconds even when a company register is slow.
# The pre-send hook runs inside Hermes's 30-second hook budget, so it passes a
# shorter timeout of its own.
DEFAULT_TIMEOUT = 30.0

MAX_TEXT = 500
MAX_LINK = 2048
VERDICTS = ("ok_to_email", "consent_needed", "blocked", "unknown")
DEFINITE_VERDICTS = ("ok_to_email", "consent_needed", "blocked")
ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")
SENDER_COUNTRIES = ("GB", "US")
RECEIPT_ID_RE = re.compile(r"^cc_rcpt_[0-9A-Z]{16}$")
COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
ADDRESS_RE = re.compile(r"[^\s<>@,;:\"']+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

# A function with urllib.request.urlopen's shape. Tests pass a fake one.
Opener = Callable[..., Any]


@dataclass
class ApiResult:
    """What came back from Precleared. status is None when the API could not be reached."""

    status: int | None
    body: dict[str, Any] | None
    error: str | None = None


def clean_text(value: Any) -> str:
    """A stripped, length-limited string, or "" for anything that isn't one."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_TEXT]


def is_https_link(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("https://")
        and len(value) <= MAX_LINK
        and value.isprintable()
        and not any(ch.isspace() for ch in value)
    )


def redact(text: str, hidden: str | None) -> str:
    """Replace every copy of `hidden` (the API key) in a message with ***."""
    if hidden and text:
        text = text.replace(hidden, "***")
    return text


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _parse(raw: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def call(
    method: str,
    url: str,
    api_key: str | None = None,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Opener | None = None,
) -> ApiResult:
    """Make one request to Precleared. Network problems come back as ApiResult.error."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    headers = _headers(api_key)
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            return ApiResult(response.status, _parse(response.read()))
    except urllib.error.HTTPError as err:
        try:
            raw = err.read()
        except Exception:  # noqa: BLE001 - a broken error body is just an empty one
            raw = b""
        return ApiResult(err.code, _parse(raw))
    except (TimeoutError, socket.timeout):
        return ApiResult(None, None, "Precleared did not answer in time. Try again in a minute.")
    except (urllib.error.URLError, OSError, ValueError):
        return ApiResult(None, None, "Could not reach Precleared. Check the network connection and try again.")


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
    return None


def build_check_body(
    email: str,
    sender_country: str,
    *,
    address_source: str = "unknown",
    pitch: str = "",
    prospect_country: str = "",
    existing_customer: Any = None,
    recipient_country: str = "",
    company_number: str = "",
    pitch_relevant: Any = None,
) -> tuple[dict[str, Any], str | None]:
    """Turn settings and tool arguments into a /api/v1/check request body.

    Returns (body, problem). Blank values count as not given, as they do in the API.
    """
    match = ADDRESS_RE.search(clean_text(email))
    if not match or len(match.group(0)) > 254:
        return {}, "Give one recipient email address, for example jane@acme.co.uk."
    email = match.group(0).lower()
    sender = clean_text(sender_country).upper()
    if sender not in SENDER_COUNTRIES:
        return {}, (
            "Precleared needs the sending business's country, GB or US. Set sender_country in the "
            "Precleared plugin settings (it covers senders based in the UK or the US only)."
        )
    source = clean_text(address_source).lower() or "unknown"
    if source not in ADDRESS_SOURCES:
        return {}, f"address_source must be one of: {', '.join(ADDRESS_SOURCES)}."
    body: dict[str, Any] = {
        "recipient_email": email,
        "sender_country": sender,
        "address_source": source,
    }
    if clean_text(pitch):
        body["pitch"] = clean_text(pitch)[:200]
    for name, value, label in (
        ("default_recipient_country", prospect_country, "prospect_country"),
        ("recipient_country", recipient_country, "recipient_country"),
    ):
        code = clean_text(value)
        if code:
            if not COUNTRY_RE.match(code):
                return {}, f"{label} must be a two-letter country code, such as GB, US or DE."
            body[name] = {"UK": "GB"}.get(code.upper(), code.upper())
    if clean_text(company_number):
        body["company_number"] = clean_text(company_number)[:12]
    relevant = _as_bool(pitch_relevant)
    if relevant is not None:
        body["pitch_relevant"] = relevant
    body["existing_customer"] = bool(_as_bool(existing_customer))
    return body, None


def first_reason(body: dict[str, Any]) -> tuple[str, str, str]:
    """The deciding rule (or note), its source title and its source link."""
    reasons = body.get("reasons")
    first = reasons[0] if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict) else {}
    text = clean_text(first.get("text")) or clean_text(first.get("note"))
    source = clean_text(first.get("source_title"))
    link = first.get("source_url") if is_https_link(first.get("source_url")) else ""
    return text, source, link


def receipt_id_of(body: dict[str, Any]) -> str:
    receipt = body.get("receipt")
    return clean_text(receipt.get("id")) if isinstance(receipt, dict) else ""


def verdict_of(body: dict[str, Any] | None) -> str:
    verdict = (body or {}).get("verdict")
    return verdict if isinstance(verdict, str) and verdict in VERDICTS else ""


def one_line_reason(body: dict[str, Any]) -> str:
    """The rule that decided it (with its source), then the next step, in one line."""
    rule, source, _ = first_reason(body)
    parts = []
    if rule:
        core = rule.rstrip(". ")
        parts.append(f"{core} ({source})." if source else f"{core}.")
    next_step = clean_text(body.get("next_step"))
    if next_step:
        parts.append(next_step if next_step.endswith((".", "!", "?")) else next_step + ".")
    text = " ".join(parts)
    return text if len(text) <= 400 else text[:397].rstrip() + "..."


def error_message(result: ApiResult, api_key: str | None = None) -> str:
    """A plain message for anything other than a successful answer."""
    if result.error:
        return result.error
    body = result.body or {}
    api_message = clean_text(body.get("message")) or clean_text(body.get("error"))
    # operator_message is written for the person running the agent (limit replies).
    for_person = clean_text(body.get("operator_message"))
    status = result.status
    if status == 400:
        text = f"Precleared could not run this check: {api_message or 'the request was not valid.'}"
    elif status == 401:
        text = (
            "Precleared rejected the API key (wrong or revoked). Get a free key at "
            f"{GET_KEY_URL} and update PRECLEARED_API_KEY in the plugin settings."
        )
    elif status == 402:
        link = body.get("buy_url") if is_https_link(body.get("buy_url")) else BUY_URL
        text = for_person or api_message or "Today's free checks on this key are used and it has no credits left."
        if link not in text:
            text += f" More checks: {link}"
    elif status == 429 and body.get("error") == "rate_limited":
        text = "Precleared is getting too many checks from this key at once."
        retry = body.get("retry_after")
        if isinstance(retry, (int, float)) and not isinstance(retry, bool) and 0 < retry <= 3600:
            text += f" Try again in {int(retry)} seconds."
    elif status == 429:
        link = body.get("get_key_url") if is_https_link(body.get("get_key_url")) else GET_KEY_URL
        text = for_person or api_message or "Precleared's free daily allowance is used up for today."
        if link not in text:
            text += f" A free key gives you your own allowance: {link}"
    else:
        text = f"Precleared returned an unexpected answer (HTTP {status}). Try again shortly."
    return redact(text, api_key)


def check(
    body: dict[str, Any],
    api_key: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Opener | None = None,
) -> ApiResult:
    """POST one check. A 200 without a known verdict comes back as an error."""
    result = call("POST", CHECK_URL, api_key, json_body=body, timeout=timeout, opener=opener)
    if result.status == 200 and not verdict_of(result.body):
        return ApiResult(
            200, result.body,
            "Precleared answered without a verdict. Treat this address as not cleared and try again later.",
        )
    return result


def normalise_receipt_id(value: Any) -> str:
    """cc_rcpt_ plus 16 capital letters or digits, or "" if the value is not a receipt ID."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if candidate.lower().startswith("cc_rcpt_"):
        candidate = "cc_rcpt_" + candidate[8:].upper()
    return candidate if RECEIPT_ID_RE.match(candidate) else ""


def get_receipt(
    receipt_id: str,
    api_key: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    opener: Opener | None = None,
) -> ApiResult:
    return call("GET", RECEIPT_URL, api_key, params={"id": receipt_id}, timeout=timeout, opener=opener)
