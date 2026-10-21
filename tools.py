"""Handlers for the precleared_check and precleared_verify_receipt tools.

Each handler takes the tool arguments and returns a JSON string, success or
error alike, and never raises.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from . import cache as cache_mod
from . import client
from .settings import SET_COMMAND, read_api_key, read_settings

logger = logging.getLogger(__name__)

WHAT_TO_DO = {
    "ok_to_email": (
        "Allowed. Send it with a footer that meets footer_text, usually who is sending, a postal "
        "address and a free, simple way to opt out. footer_text describes the requirement; it isn't "
        "wording to paste."
    ),
    "consent_needed": "Don't send this cold email. Follow next_step and tell the user.",
    "blocked": "Don't send. Follow next_step and tell the user.",
    "unknown": (
        "Treat this as consent_needed: don't send yet. If a missing fact would settle it (the "
        "recipient's country, a UK company number, where the address came from), get it from the "
        "user or a record, not a guess, and check again."
    ),
}
RECEIPT_FIELDS = (
    "id", "created_at", "verdict", "confidence", "rules_applied", "recipient_domain",
    "recipient_email_masked", "sender_country", "rules_version", "request_hash",
    "response_hash", "signature", "algorithm", "key_version", "public_key",
)
USAGE_LIMIT = 12


def _json(data: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in data.items() if v not in (None, "", [], {})}, ensure_ascii=False)


def _small(value: Any, depth: int = 0) -> Any:
    """Plain values from an API object, trimmed: strings, numbers, booleans and short lists or maps."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return client.clean_text(value)
    if depth < 2 and isinstance(value, list):
        return [_small(v, depth + 1) for v in value[:USAGE_LIMIT]]
    if depth < 2 and isinstance(value, dict):
        return {str(k)[:60]: _small(v, depth + 1) for k, v in list(value.items())[:USAGE_LIMIT]}
    return None


def make_check_handler(
    ctx: Any,
    *,
    opener: client.Opener | None = None,
    clock: Callable[[], float] = time.time,
    api_key_reader: Callable[[], str | None] = read_api_key,
) -> Callable[..., str]:
    def check(args: dict[str, Any]) -> dict[str, Any]:
        settings = read_settings(ctx)
        if not settings.configured:
            return {
                "error": (
                    "Precleared needs the country the sending business is based in, GB or US. Ask "
                    f"the user, then set it with: {SET_COMMAND} (or US). Precleared covers senders "
                    "based in the UK or the US only."
                )
            }
        body, problem = client.build_check_body(
            args.get("email", ""),
            settings.sender_country,
            address_source=client.clean_text(args.get("address_source")) or settings.address_source,
            pitch=client.clean_text(args.get("pitch")) or settings.pitch,
            prospect_country=settings.prospect_country,
            existing_customer=args.get("existing_customer"),
            recipient_country=client.clean_text(args.get("recipient_country")),
            company_number=client.clean_text(args.get("company_number")),
            pitch_relevant=args.get("pitch_relevant"),
        )
        if problem:
            return {"error": problem}
        api_key = api_key_reader()
        result = client.check(body, api_key, opener=opener)
        if result.error or result.status != 200:
            return {"error": client.error_message(result, api_key)}
        data = result.body or {}
        verdict = client.verdict_of(data)
        rule, source, link = client.first_reason(data)
        receipt_id = client.receipt_id_of(data)
        if verdict in client.DEFINITE_VERDICTS:
            # Lets the pre-send check reuse this answer instead of asking again,
            # when this call used the plugin's default facts.
            cache_mod.VerdictCache(ctx.state, clock).store(
                {body["recipient_email"]: cache_mod.Cached(verdict, client.one_line_reason(data), receipt_id)},
                cache_mod.fingerprint(body),
            )
        return {
            "email": body["recipient_email"],
            "verdict": verdict,
            "send": verdict == "ok_to_email",
            "what_to_do": WHAT_TO_DO.get(verdict, ""),
            "rule": rule,
            "source": source,
            "source_url": link,
            "footer_text": client.clean_text(data.get("footer_text")),
            "next_step": client.clean_text(data.get("next_step")),
            "confidence": client.clean_text(data.get("confidence")),
            "receipt_id": receipt_id,
            "receipt_url": f"{client.RECEIPT_PAGE_URL}{receipt_id}" if receipt_id else "",
            "checked_with": {k: v for k, v in body.items() if k != "recipient_email"},
            "allowance": _small(data.get("usage")),
            "disclaimer": client.clean_text(data.get("disclaimer")),
        }

    def handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        try:
            return _json(check(args if isinstance(args, dict) else {}))
        except Exception:  # noqa: BLE001 - handlers return errors, never raise
            logger.exception("precleared: precleared_check failed")
            return _json({"error": "The Precleared check failed unexpectedly. Try again shortly."})

    return handler


def make_receipt_handler(
    ctx: Any,
    *,
    opener: client.Opener | None = None,
    api_key_reader: Callable[[], str | None] = read_api_key,
) -> Callable[..., str]:
    def verify(args: dict[str, Any]) -> dict[str, Any]:
        receipt_id = client.normalise_receipt_id(args.get("receipt_id"))
        if not receipt_id:
            return {"error": "Give a receipt id, for example cc_rcpt_0123456789ABCDEF."}
        api_key = api_key_reader()
        result = client.get_receipt(receipt_id, api_key, opener=opener)
        if result.status == 404:
            return {"error": f"Precleared has no receipt with the id {receipt_id}."}
        if result.error or result.status != 200:
            return {"error": client.error_message(result, api_key)}
        data = result.body or {}
        receipt = {name: _small(data.get(name)) for name in RECEIPT_FIELDS}
        receipt["receipt_url"] = f"{client.RECEIPT_PAGE_URL}{receipt_id}"
        return receipt

    def handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        try:
            return _json(verify(args if isinstance(args, dict) else {}))
        except Exception:  # noqa: BLE001 - handlers return errors, never raise
            logger.exception("precleared: precleared_verify_receipt failed")
            return _json({"error": "Fetching the receipt failed unexpectedly. Try again shortly."})

    return handler
