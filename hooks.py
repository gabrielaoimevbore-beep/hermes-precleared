"""The pre-send check: a pre_tool_call hook that holds cold email Precleared hasn't cleared.

For each tool call that sends email (see gate.py), every recipient is
checked once (verdicts are cached for 30 days). If all of them come back
ok_to_email the send goes ahead untouched. Otherwise, depending on the mode
setting:

- ask (default): the call goes to Hermes's own approval prompt with the
  verdicts, so a person decides. Unattended runs (cron, gateway without a
  person) fail closed unless the user configured Hermes otherwise.
- block: the call is refused and the agent is told why.
- off: no checks.

The hook never approves anything itself, and every check finishes inside
Hermes's default 30-second hook budget.
"""

from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable

from . import cache as cache_mod
from . import client, gate
from .settings import SET_COMMAND, Settings, read_api_key, read_settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 24.0  # per check; Precleared itself answers within 25 s
DEADLINE = 26.0  # for all the checks in one send, inside Hermes's 30 s hook timeout
WORKERS = 5
MAX_CHECKS_PER_SEND = 10
MAX_LINES = 5
MAX_MESSAGE = 1800
TOO_MANY = "too_many"

ASK_TAIL = (
    "If this isn't a cold or marketing email (for example a reply, a message to an existing "
    "customer, or a receipt), it can be allowed."
)
BLOCK_TAIL = (
    "The email was not sent. Don't send cold or marketing email to these addresses: tell the "
    "user and follow the next step above. If it isn't a cold or marketing email, the user can "
    "switch the Precleared plugin's mode to ask and approve it."
)


@dataclass
class Outcome:
    email: str
    verdict: str = ""
    reason: str = ""
    receipt_id: str = ""
    error: str = ""

    @property
    def cleared(self) -> bool:
        return not self.error and self.verdict == "ok_to_email"

    @property
    def status(self) -> str:
        return self.verdict if not self.error and self.verdict else "unchecked"


def check_body(settings: Settings, email: str) -> tuple[dict[str, Any], str | None]:
    """The request the hook sends for one recipient: the plugin's settings, not a customer."""
    return client.build_check_body(
        email,
        settings.sender_country,
        address_source=settings.address_source,
        pitch=settings.pitch,
        prospect_country=settings.prospect_country,
    )


def run_checks(
    emails: list[str],
    settings: Settings,
    api_key: str | None,
    *,
    opener: client.Opener | None = None,
    timeout: float | None = None,
    deadline: float | None = None,
) -> dict[str, Outcome]:
    """Check addresses in parallel. Anything unanswered by the deadline comes back as an error."""
    if not emails:
        return {}
    timeout = REQUEST_TIMEOUT if timeout is None else timeout
    deadline = DEADLINE if deadline is None else deadline

    def one(email: str) -> Outcome:
        body, problem = check_body(settings, email)
        if problem:
            return Outcome(email, error=problem)
        result = client.check(body, api_key, timeout=timeout, opener=opener)
        if result.error or result.status != 200:
            return Outcome(email, error=client.error_message(result, api_key))
        data = result.body or {}
        return Outcome(
            email,
            verdict=client.verdict_of(data),
            reason=client.one_line_reason(data),
            receipt_id=client.receipt_id_of(data),
        )

    outcomes: dict[str, Outcome] = {}
    pool = ThreadPoolExecutor(max_workers=min(WORKERS, len(emails)), thread_name_prefix="precleared")
    try:
        futures = {pool.submit(one, email): email for email in emails}
        done, _ = wait(futures, timeout=deadline)
        for future, email in futures.items():
            if future not in done:
                outcomes[email] = Outcome(email, error="Precleared did not answer in time. Try again in a minute.")
                continue
            try:
                outcomes[email] = future.result()
            except Exception:  # noqa: BLE001 - one failed check must not hide the others
                logger.exception("precleared: check for one recipient failed")
                outcomes[email] = Outcome(email, error="The check failed unexpectedly. Try again shortly.")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return outcomes


def describe(outcome: Outcome) -> str:
    if outcome.error:
        return f"- {outcome.email}: not checked. {outcome.error}"
    text = f"- {outcome.email}: {outcome.verdict}."
    if outcome.reason:
        text += f" {outcome.reason}"
    if outcome.receipt_id:
        text += f" Receipt: {client.RECEIPT_PAGE_URL}{outcome.receipt_id}"
    return text


def _context(detection: gate.Detection) -> str:
    parts = [f"via {detection.channel}"] if detection.channel else []
    if detection.subject:
        parts.append(f'subject "{detection.subject}"')
    return f" ({', '.join(parts)})" if parts else ""


def held_message(held: list[Outcome], total: int, detection: gate.Detection) -> str:
    if total == 1:
        head = "Precleared hasn't cleared this email"
    elif len(held) == total:
        head = f"Precleared hasn't cleared any of the {total} recipients of this email"
    else:
        head = f"Precleared hasn't cleared {len(held)} of the {total} recipients of this email"
    lines = [head + _context(detection) + "."]
    checked = [o for o in held if o.error != TOO_MANY]
    too_many = [o.email for o in held if o.error == TOO_MANY]
    lines += [describe(o) for o in checked[:MAX_LINES]]
    if len(checked) > MAX_LINES:
        lines.append(f"- And {len(checked) - MAX_LINES} more.")
    if too_many:
        shown = ", ".join(too_many[:MAX_LINES])
        if len(too_many) > MAX_LINES:
            shown += f" and {len(too_many) - MAX_LINES} more"
        lines.append(
            f"- Not checked, because this email goes to more than {MAX_CHECKS_PER_SEND} new addresses: "
            f"{shown}. Check them first with precleared_check, or send in smaller batches."
        )
    return "\n".join(lines)


def _digest(items: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(items))).encode("utf-8")).hexdigest()[:20]


def rule_key_for(held: list[Outcome]) -> str:
    """The [a]lways-approve scope: these recipients with these verdicts, never the whole tool.

    The addresses are hashed, so Hermes's saved approvals hold no addresses. A
    different verdict later (say blocked, after an approval given while an
    address couldn't be checked) asks again.
    """
    return "precleared:" + _digest([f"{o.email}={o.status}" for o in held])


def directive(mode: str, message: str, rule_key: str) -> dict[str, str]:
    if len(message) > MAX_MESSAGE:
        message = message[: MAX_MESSAGE - 3].rstrip() + "..."
    if mode == "block":
        return {"action": "block", "message": f"{message}\n{BLOCK_TAIL}"}
    return {"action": "approve", "message": f"{message}\n{ASK_TAIL}", "rule_key": rule_key}


def make_pre_tool_call(
    ctx: Any,
    *,
    opener: client.Opener | None = None,
    clock: Callable[[], float] = time.time,
    api_key_reader: Callable[[], str | None] = read_api_key,
) -> Callable[..., dict[str, str] | None]:
    """Build the pre_tool_call callback for this plugin context."""

    def decide(settings: Settings, detection: gate.Detection, recipients: list[str]) -> dict[str, str] | None:
        if not settings.configured:
            who = recipients[0] if len(recipients) == 1 else f"{len(recipients)} recipients"
            return directive(
                settings.mode,
                f"Precleared can't check this email to {who}{_context(detection)} yet, because the "
                f"country the business sends from isn't set. Set it with: {SET_COMMAND} (or US).",
                "precleared:setup:" + _digest(recipients),
            )
        first_body, _ = check_body(settings, recipients[0])
        fp = cache_mod.fingerprint(first_body)
        verdicts = cache_mod.VerdictCache(ctx.state, clock)
        outcomes = {
            email: Outcome(email, verdict=c.verdict, reason=c.reason, receipt_id=c.receipt_id)
            for email, c in verdicts.lookup(recipients, fp).items()
        }
        unchecked = [email for email in recipients if email not in outcomes]
        to_check, too_many = unchecked[:MAX_CHECKS_PER_SEND], unchecked[MAX_CHECKS_PER_SEND:]
        if to_check:
            fresh = run_checks(to_check, settings, api_key_reader(), opener=opener)
            outcomes.update(fresh)
            verdicts.store(
                {e: cache_mod.Cached(o.verdict, o.reason, o.receipt_id) for e, o in fresh.items() if not o.error},
                fp,
            )
        for email in too_many:
            outcomes[email] = Outcome(email, error=TOO_MANY)
        held = [outcomes[email] for email in recipients if not outcomes[email].cleared]
        if not held:
            return None
        return directive(settings.mode, held_message(held, len(recipients), detection), rule_key_for(held))

    def pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", **kwargs: Any) -> dict[str, str] | None:
        try:
            settings = read_settings(ctx)
            if settings.mode == "off":
                return None
            detection = gate.detect(tool_name, args, settings.extra_tools)
            if detection is None or detection.reply:
                return None
            recipients = [e for e in detection.recipients if not gate.is_skipped_address(e, settings.ignore_domains)]
        except Exception:  # noqa: BLE001 - a bug here must never hold up unrelated tools
            logger.exception("precleared: could not inspect a %s call; letting it through", tool_name)
            return None
        if not recipients:
            return None
        try:
            return decide(settings, detection, recipients)
        except Exception:  # noqa: BLE001 - a bug after detection holds the email instead of sending it
            logger.exception("precleared: the check for a %s call failed", tool_name)
            return directive(
                settings.mode,
                f"Precleared couldn't check this email{_context(detection)} because of an unexpected "
                "error in the plugin.",
                "precleared:error:" + _digest(recipients),
            )

    return pre_tool_call
