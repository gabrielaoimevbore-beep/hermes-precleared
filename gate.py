"""Spot email sends in Hermes tool calls and work out who they go to.

Nothing here touches the network or Hermes itself. hooks.py calls detect()
for every tool call; when it returns a Detection with recipients, they are
checked with Precleared before the tool runs.

Three kinds of send are recognised:

- plugin and MCP tools whose name says they send email (inkbox_send_email,
  openmail_send, mcp__gmail__send_message, ...), plus any tool named in the
  extra_tools setting. Recipients come from the arguments (to, cc, bcc,
  recipients, ...); sender fields (from, sender, reply_to) are skipped.
- terminal commands from the email skills that ship with Hermes: himalaya,
  agentmail and the Google Workspace script (gmail send), plus swaks, sendmail,
  msmtp, mail, mutt and `hermes send --to email:...`.
- curl calls to the send endpoints of common email APIs (Resend, SendGrid,
  Postmark, Mailgun, Brevo, Mailjet, AgentMail, Microsoft Graph, Amazon SES).

In shell commands, words inside quotes, comments and heredoc bodies are not
read as commands, so writing a file or a commit message that mentions "mail"
isn't a send. Replies in an existing thread are not checked. A send whose
recipients aren't visible in the call (a message read from a file, a script)
can't be checked, so it isn't held either.

Everything here runs on every tool call, so it is linear in the size of the
call: no regular expression can backtrack across lines.
"""

from __future__ import annotations

import bisect
import fnmatch
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# An address must start at a token boundary, so "$x@acme.com" (a shell
# variable) or fragments of longer strings are not misread.
EMAIL_RE = re.compile(
    r"(?<![$\w%+.\-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)

# Domains that can't receive mail (RFC 2606 and RFC 6761). Sends to them are tests.
RESERVED_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost", ".local")

MAX_RECIPIENTS = 100
MAX_DEPTH = 8
MAX_ITEMS = 500
MAX_COMMAND = 500_000  # characters of a shell command that are read
MAX_SEGMENT = 100_000  # characters of one command's arguments that are parsed
MAX_COMMANDS = 200  # command words looked at in one shell command
MAX_SCANNED = 2_000_000  # characters of command arguments parsed in one shell command
MAX_SUBJECT = 120


@dataclass
class Detection:
    """An email send found in a tool call."""

    channel: str
    recipients: list[str] = field(default_factory=list)
    reply: bool = False
    subject: str = ""


# ---------------------------------------------------------------------------
# Addresses


def normalise_email(value: str) -> str:
    return value.strip().strip(".").lower()


def emails_in(text: Any) -> list[str]:
    """Every address in a string, lower-cased, in order, without repeats."""
    if not isinstance(text, str) or "@" not in text:
        return []
    return dedupe(normalise_email(m.group(0)) for m in EMAIL_RE.finditer(text))


def dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def _domain_matches(domain: str, pattern: str) -> bool:
    pattern = pattern.strip().lower().lstrip("@").lstrip("*").lstrip(".")
    return bool(pattern) and (domain == pattern or domain.endswith("." + pattern))


def is_skipped_address(email: str, ignore_domains: Iterable[str] = ()) -> bool:
    """True for test domains and for domains the user listed in ignore_domains."""
    domain = domain_of(email)
    if not domain:
        return True
    if domain in RESERVED_DOMAINS or domain.endswith(RESERVED_SUFFIXES):
        return True
    return any(_domain_matches(domain, p) for p in ignore_domains if isinstance(p, str))


def _clean_subject(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= MAX_SUBJECT else text[: MAX_SUBJECT - 3].rstrip() + "..."


# ---------------------------------------------------------------------------
# Tool names

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_SEND_TOKEN = re.compile(r"(?:re)?send(?:mail|email|message|now)?s?")

# Tokens that, with "send", mark an email tool. Anything containing "mail" counts
# too (email, gmail, agentmail, mailgun, ...).
MAIL_TOKENS = frozenset({
    "smtp", "outlook", "resend", "sendgrid", "postmark", "brevo", "sendinblue",
    "ses", "mailgun", "mailjet", "office365", "o365", "ms365", "m365",
})

# Tool-name tokens for things that aren't a first email to a new person:
# drafts (a person sends them), replies and forwards, test sends, reads, and
# transactional mail.
SKIP_TOKENS = frozenset({
    "draft", "drafts", "reply", "replies", "forward", "forwards", "fwd",
    "test", "read", "list", "search", "get", "fetch", "receipt", "receipts",
    "invoice", "invoices", "password", "otp", "verification", "verify", "reset",
    "confirmation", "transactional",
})


def _tokens(name: str) -> list[str]:
    return [t.lower() for t in _NON_ALNUM.split(_CAMEL.sub("_", name)) if t]


def _tool_part(tool_name: str) -> str:
    """The tool's own name: for MCP tools (mcp__server__tool) the part after the server."""
    if tool_name.startswith("mcp__") and tool_name.count("__") >= 2:
        return tool_name.split("__", 2)[2]
    return tool_name


def matches_extra(tool_name: str, extra_tools: Iterable[str]) -> bool:
    lowered = tool_name.lower()
    for pattern in extra_tools:
        if isinstance(pattern, str) and pattern.strip():
            if fnmatch.fnmatchcase(lowered, pattern.strip().lower()):
                return True
    return False


def is_email_send_tool(tool_name: str) -> bool:
    """True when a tool's name says it sends email."""
    if not isinstance(tool_name, str) or not tool_name:
        return False
    own = _tokens(_tool_part(tool_name))
    if not any(_SEND_TOKEN.fullmatch(t) for t in own):
        return False
    if any(t in SKIP_TOKENS for t in own):
        return False
    every = _tokens(tool_name)
    return any("mail" in t or t in MAIL_TOKENS for t in every)


# ---------------------------------------------------------------------------
# Recipients in structured arguments


def _norm_key(key: Any) -> str:
    return _NON_ALNUM.sub("", str(key)).lower()


RECIPIENT_KEYS = frozenset({
    "to", "cc", "bcc", "recipient", "recipients", "toemail", "toemails",
    "toaddress", "toaddresses", "torecipients", "ccrecipients", "bccrecipients",
    "email", "emails", "emailaddress", "emailaddresses", "tolist", "sendto",
    "ccemail", "ccemails", "bccemail", "bccemails", "ccaddress", "ccaddresses",
    "bccaddress", "bccaddresses", "destination", "destinations",
    "recipientemail", "recipientemails", "recipientaddress", "recipientaddresses",
    "extrarecipients", "toemailaddress", "toemailaddresses", "mailto", "emailto",
})
# Keys whose contents are never recipients: the sender's side of the message,
# and containers for the sender's own account or settings.
NOT_RECIPIENT_KEYS = frozenset({
    "from", "sender", "replyto", "fromemail", "fromaddress", "fromname",
    "senderemail", "senderaddress", "sendername", "inboxid", "inbox",
    "returnpath", "envelopefrom", "mailfrom", "account", "accountid", "auth",
    "user", "owner", "credentials", "login", "connection", "profile",
    "signature", "settings", "config", "headers", "metadata",
})
# Keys that make a send a reply in an existing thread.
REPLY_KEYS = frozenset({
    "inreplyto", "replytomessageid", "replytoid", "threadid", "parentmessageid",
    "originalmessageid",
})


def _has_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return value
    return isinstance(value, (int, float)) or bool(value)


def recipients_from_args(args: Any) -> tuple[list[str], bool, str]:
    """Addresses under recipient keys, whether the call is a reply in a thread, and its subject.

    A reply marker (thread id, In-Reply-To) covers the object it's in, so in a
    batch of messages only the replies are left out.
    """
    found: list[str] = []
    replies = 0
    subject = ""

    def walk(node: Any, depth: int, collecting: bool) -> None:
        nonlocal replies, subject
        if depth > MAX_DEPTH or len(found) > MAX_RECIPIENTS:
            return
        if isinstance(node, dict):
            items = list(node.items())[:MAX_ITEMS]
            if any(_norm_key(k) in REPLY_KEYS and _has_value(v) for k, v in items):
                replies += 1
                return
            for key, value in items:
                k = _norm_key(key)
                if k in NOT_RECIPIENT_KEYS or k in REPLY_KEYS:
                    continue
                if k == "subject" and not subject and isinstance(value, str):
                    subject = _clean_subject(value)
                    continue
                walk(value, depth + 1, collecting or k in RECIPIENT_KEYS)
        elif isinstance(node, (list, tuple)):
            for item in list(node)[:MAX_ITEMS]:
                walk(item, depth + 1, collecting)
        elif isinstance(node, str):
            if collecting:
                found.extend(emails_in(node))
            elif looks_like_message(node):
                # A whole message passed as text: headers, a blank line, the body.
                message = read_messages(node)
                found.extend(message.recipients)
                replies += message.replies
                subject = subject or message.subject

    walk(args, 0, False)
    recipients = dedupe(found)[:MAX_RECIPIENTS]
    return recipients, bool(replies) and not recipients, subject


# ---------------------------------------------------------------------------
# Messages written out as text (himalaya templates, sendmail -t, raw messages)

_HEADER_LINE = re.compile(r"([A-Za-z][A-Za-z0-9-]*)[ \t]*:[ \t]*(.*)")
_RECIPIENT_HEADERS = frozenset({"to", "cc", "bcc"})
_KNOWN_HEADERS = frozenset({
    "from", "to", "cc", "bcc", "subject", "date", "reply-to", "in-reply-to",
    "references", "message-id", "mime-version", "content-type",
    "content-transfer-encoding", "sender", "return-path", "list-unsubscribe",
    "list-unsubscribe-post", "importance", "priority",
})
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([^\s;|&<>()'\"`]+))")
# printf 'To: a@b.com\nSubject: x' and echo -e "To: ...": a header right after an
# opening quote starts on its own line once the message text is unpacked.
_QUOTED_HEADER = re.compile(
    r"(?<=['\"])(?=(?:from|to|cc|bcc|subject|reply-to|in-reply-to|date|message-id)[ \t]*:)",
    re.IGNORECASE,
)


def _known_header(name: str) -> bool:
    lowered = name.lower()
    return lowered in _KNOWN_HEADERS or lowered.startswith("x-")


def looks_like_message(text: str) -> bool:
    """True when a string starts with an email header line (From:, To:, Subject:, ...)."""
    if "\n" not in text or ":" not in text:
        return False
    for line in text.splitlines():
        if line.strip():
            match = _HEADER_LINE.fullmatch(line.strip())
            return bool(match and _known_header(match.group(1)))
    return False


@dataclass
class Messages:
    recipients: list[str]  # from messages that aren't replies
    replies: int  # messages with an In-Reply-To header
    subject: str


def unpack_message_text(text: str) -> str:
    """Turn printf/echo style literal \\n into line breaks and split headers off opening quotes."""
    if "\\n" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    return _QUOTED_HEADER.sub("\n", text)


def read_messages(text: str) -> Messages:
    """Recipients from the header blocks in text, skipping messages that are replies.

    A message is a run of header lines ("Name: value", with folded
    continuation lines), a blank line, then the body. Body lines are never
    read as headers. A heredoc's closing delimiter ends a message, so a
    command can hold more than one.
    """
    found: list[str] = []
    replies = 0
    subject = ""
    state = "outside"  # outside -> headers -> body (-> outside at a heredoc delimiter)
    current: str | None = None
    block: list[str] = []
    block_reply = False
    delimiters: set[str] = set()

    def close_block() -> None:
        nonlocal block, block_reply, replies
        if block_reply:
            replies += 1
        else:
            found.extend(block)
        block, block_reply = [], False

    for raw in text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if state == "body":
            if stripped and stripped in delimiters:
                state, current = "outside", None
            continue
        if not stripped:
            if state == "headers":
                close_block()
                state, current = "body", None
            continue
        match = _HEADER_LINE.fullmatch(stripped)
        known = bool(match and _known_header(match.group(1)))
        if state == "headers":
            if line[:1] in (" ", "\t") and not known and current is not None:
                # A folded continuation of the header above.
                if current in _RECIPIENT_HEADERS:
                    block.extend(emails_in(line))
                continue
            if not match:
                close_block()
                state, current = "outside", None
        if match and (state == "headers" or known):
            state = "headers"
            current = match.group(1).lower()
            value = match.group(2)
            if current in _RECIPIENT_HEADERS:
                block.extend(emails_in(value))
            elif current == "in-reply-to" and value.strip():
                block_reply = True
            elif current == "subject" and not subject:
                subject = _clean_subject(value.strip().strip("'\""))
            continue
        if stripped in delimiters:
            continue
        if "<<" in line and len(delimiters) < 50:
            delimiters.update(m.group(1) or m.group(2) or m.group(3) for m in _HEREDOC.finditer(line))
    if state == "headers":
        close_block()
    return Messages(dedupe(found), replies, subject)


# ---------------------------------------------------------------------------
# Shell commands

# The parts of a command that skeleton() blanks or tracks: quoted strings
# ($'...' with its escapes first; a double quote is scanned by _scan_double),
# escapes, arithmetic $((...)) and ((...)) (whose << is a shift, not a
# heredoc), heredoc operators (not <<< here-strings) and comments.
_SKELETON_TOKEN = re.compile(
    r"\$'(?:[^'\\]|\\.)*'?"
    r"|'[^']*'?"
    r"|\""
    r"|\\."
    r"|\$?\(\((?:[^()\n]|\([^()\n]*\))*\)\)"
    r"|\$\((?!\()"
    r"|[()`]"
    r"|(?<!<)<<(?!<)-?[ \t]*(?:'(?P<sq>[^'\n]+)'|\"(?P<dq>[^\"\n]+)\"|\\?(?P<bare>[^\s;|&<>()'\"`]+))"
    r"|(?:(?<=[\s;|&()])|^)#[^\n]*",
    re.DOTALL,
)
_HEREDOC_OP = r"(?<!<)<<(?!<)-?[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([^\s;|&<>()'\"`]+))"
_DQ_SPECIAL = re.compile(r"[\"\\`]|\$\(")
_DQ_PLAIN = re.compile(r"[\"\\]")
_PAREN_SPECIAL = re.compile(r"[()'\"\\`\n]|\$'|" + _HEREDOC_OP)
_ANSI_C_BODY = re.compile(r"(?:[^'\\]|\\.)*'?", re.DOTALL)
MAX_NESTING = 3
MAX_SPANS = 2000


def _blank(chunk: str) -> str:
    return " " * len(chunk)


def _heredoc_end(text: str, pos: int, end: int, delimiter: str, tabs: bool) -> int:
    """Just past the closing delimiter line of a heredoc whose body starts at pos (or end)."""
    closing = re.compile(
        r"^" + (r"\t*" if tabs else "") + re.escape(delimiter) + r"[ \t]*\r?$", re.MULTILINE
    ).search(text, pos, end)
    return closing.end() if closing else end


def _scan_backtick(text: str, pos: int, end: int) -> int:
    """From just after an opening backtick to just after its closing one."""
    while True:
        close = text.find("`", pos, end)
        if close == -1:
            return end
        if text[close - 1] == "\\":
            pos = close + 1
            continue
        return close + 1


def _scan_parens(text: str, pos: int, end: int, depth: int) -> int:
    """From just after "$(" to just after its closing ")", skipping quoted text and heredoc bodies."""
    level = 1
    pending: list[tuple[str, bool]] = []
    while True:
        match = _PAREN_SPECIAL.search(text, pos, end)
        if match is None:
            return end
        token, pos = match.group(0), match.end()
        if token == "\n":
            for delimiter, tabs in pending:
                pos = _heredoc_end(text, pos, end, delimiter, tabs)
            pending = []
        elif token.startswith("<<"):
            if len(pending) < 20:
                pending.append((match.group(1) or match.group(2) or match.group(3), token[2:3] == "-"))
        elif token == "(":
            level += 1
        elif token == ")":
            level -= 1
            if level == 0:
                return pos
        elif token == "'":
            close = text.find("'", pos, end)
            pos = end if close == -1 else close + 1
        elif token == "$'":
            pos = _ANSI_C_BODY.match(text, pos, end).end()
        elif token == '"':
            pos, _ = _scan_double(text, pos, end, depth + 1)
        elif token == "\\":
            pos += 1
        else:  # backtick
            pos = _scan_backtick(text, pos, end)


def _scan_double(text: str, pos: int, end: int, depth: int) -> tuple[int, list[tuple[int, int]]]:
    """From just after an opening double quote: where the string ends (just after its
    closing quote) and the $(...) and `...` spans inside it, which the shell still runs."""
    subs: list[tuple[int, int]] = []
    special = _DQ_SPECIAL if depth < MAX_NESTING else _DQ_PLAIN
    while True:
        match = special.search(text, pos, end)
        if match is None:
            return end, subs
        token = match.group(0)
        if token == '"':
            return match.end(), subs
        if token == "\\":
            pos = match.end() + 1
            continue
        if token == "`":
            close = _scan_backtick(text, match.end(), end)
        elif text.startswith("$((", match.start()):
            close = text.find("))", match.end(), end)  # arithmetic, not a command
            pos = end if close == -1 else close + 2
            continue
        else:
            close = _scan_parens(text, match.end(), end, depth)
        subs.append((match.start(), close))
        pos = close


def _skeleton(text: str, depth: int, base: int) -> tuple[str, list[tuple[int, int]]]:
    """skeleton(), plus the spans of every command substitution ($(...) and `...`), as
    positions offset by base."""
    edits: list[tuple[int, int, str]] = []
    spans: list[tuple[int, int]] = []
    pending: list[tuple[str, bool]] = []
    # Open command substitutions, tracked in one pass: [start, nested "(" count] for $(,
    # and the start of an open backtick.
    open_parens: list[list[int]] = []
    open_backtick: int | None = None
    pos, size = 0, len(text)
    while pos < size:
        limit = size
        if pending:
            # Heredoc bodies start after the line their operators are on.
            line_end = text.find("\n", pos)
            limit = size if line_end == -1 else line_end
            match = _SKELETON_TOKEN.search(text, pos, limit)
            if match is None:
                pos = limit + 1
                for delimiter, tabs in pending:
                    closing = re.compile(
                        r"^" + (r"\t*" if tabs else "") + re.escape(delimiter) + r"[ \t]*\r?$", re.MULTILINE
                    ).search(text, pos)
                    stop = closing.start() if closing else size
                    if closing and stop > pos and text[stop - 1] == "\n":
                        stop -= 1  # keep the line break before the closing delimiter
                    if stop > pos:
                        edits.append((pos, stop, _blank(text[pos:stop])))
                    pos = closing.end() if closing else size
                pending = []
                continue
        else:
            match = _SKELETON_TOKEN.search(text, pos)
            if match is None:
                break
        token = match.group(0)
        start, pos = match.start(), match.end()
        if token.startswith("<<"):
            if len(pending) < 20:
                delimiter = match.group("sq") or match.group("dq") or match.group("bare")
                pending.append((delimiter, token[2:3] == "-"))
        elif token == '"':
            stop, subs = _scan_double(text, pos, limit, depth)
            inner_end = stop - 1 if stop > pos and text[stop - 1] == '"' else stop
            cursor = pos
            for sub_start, sub_end in subs:
                sub_end = min(sub_end, inner_end)
                if sub_start > cursor:
                    edits.append((cursor, sub_start, _blank(text[cursor:sub_start])))
                if sub_end > sub_start:
                    inner, inner_spans = _skeleton(text[sub_start:sub_end], depth + 1, base + sub_start)
                    edits.append((sub_start, sub_end, inner))
                    if len(spans) < MAX_SPANS:
                        spans.append((base + sub_start, base + sub_end))
                        spans.extend(inner_spans[: MAX_SPANS - len(spans)])
                cursor = max(cursor, sub_end)
            if inner_end > cursor:
                edits.append((cursor, inner_end, _blank(text[cursor:inner_end])))
            pos = stop
        elif token == "$(":
            if len(open_parens) < 100:
                open_parens.append([start, 0])
        elif token == "(":
            if open_parens:
                open_parens[-1][1] += 1
        elif token == ")":
            if open_parens:
                if open_parens[-1][1]:
                    open_parens[-1][1] -= 1
                else:
                    sub_start = open_parens.pop()[0]
                    if len(spans) < MAX_SPANS:
                        spans.append((base + sub_start, base + pos))
        elif token == "`":
            if open_backtick is None:
                open_backtick = start
            else:
                if len(spans) < MAX_SPANS:
                    spans.append((base + open_backtick, base + pos))
                open_backtick = None
        elif token.startswith(("$'", "'")):
            inner = start + (2 if token[0] == "$" else 1)
            inner_end = pos - 1 if len(token) > inner - start and token.endswith("'") else pos
            if inner_end > inner:
                edits.append((inner, inner_end, _blank(text[inner:inner_end])))
        elif token[0] == "\\" or token[0] == "#":
            edits.append((start, pos, _blank(token)))
    for sub_start, _ in open_parens:  # never closed: runs to the end
        if len(spans) < MAX_SPANS:
            spans.append((base + sub_start, base + size))
    if open_backtick is not None and len(spans) < MAX_SPANS:
        spans.append((base + open_backtick, base + size))
    if not edits:
        return text, spans
    pieces: list[str] = []
    last = 0
    for start, end, replacement in edits:
        pieces.append(text[last:start])
        pieces.append(replacement)
        last = end
    pieces.append(text[last:])
    return "".join(pieces), spans


def skeleton(text: str) -> str:
    """The command with quoted text, comments, escapes and heredoc bodies blanked out.

    It has the same length as the command, so a position in one is the same
    position in the other. Command words and flags are searched for here, so
    "mail" inside a string, a comment or a file being written isn't a command,
    and a line break inside a quoted argument doesn't end the command. Command
    substitutions inside double quotes ("$(...)") still count, since the shell
    runs them.
    """
    return _skeleton(text, 0, 0)[0]


# A command word: at the start of the text or after a separator or a shell
# keyword, after any VAR=value assignments and sudo/env/timeout style wrappers,
# with an optional path (~/bin/, $HOME/.local/bin/, /usr/sbin/).
#
# The repeated parts are bounded (up to 8 assignments, 4 wrappers and 6 flags per
# wrapper), a quoted assignment value can only be read one way, and a wrapper word
# is never taken as a flag's argument. Without that, an input such as "sudo -a 1 ",
# 'A="1" ' or "sudo -a " repeated a few dozen times makes the match backtrack
# exponentially, and this runs on every terminal call that mentions a mail command
# or curl.
_WRAPPER = r"(?:sudo|doas|exec|command|nohup|env|time|nice|ionice|stdbuf|timeout|xargs|setsid|caffeinate|unbuffer|chronic)"
_COMMAND_PREFIX = (
    r"(?:^|(?<=[\n;|&(`])|(?<=\$\())[ \t]*"
    r"(?:(?:if|then|do|else|elif|while|until|!|\{)[ \t]+)*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:(?:\"[^\"\n]*\"|'[^'\n]*')[^\s;|&]*|[^\s;|&\"'][^\s;|&]*)?[ \t]+){0,8}"
    r"(?:" + _WRAPPER + r"(?:[ \t]+-{1,2}[A-Za-z][\w-]*(?:[ \t]+(?!" + _WRAPPER + r"[ \t])[^\s;|&-][^\s;|&]*)?){0,6}"
    r"(?:[ \t]+\d[\w.]*)?[ \t]+){0,4}"
    r"(?:(?:~|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\.{1,2})?/(?:[\w.-]+/)*)?"
)
MAILERS = ("sendmail", "msmtp", "mailx", "mail", "mutt", "neomutt")
_COMMAND = re.compile(
    _COMMAND_PREFIX
    + r"(himalaya|agentmail|swaks|hermes|curl|" + "|".join(MAILERS) + r")(?=[ \t\n;|&)<>]|$)"
)
_GMAIL_SEND = re.compile(r"\bgmail[ \t]+send\b")
_SEGMENT_END = re.compile(r"[\n;|&)`]")
_TRIGGER = re.compile(r"himalaya|agentmail|swaks|hermes|curl|mail|mutt|msmtp")

EMAIL_API = re.compile(
    r"https?://(?:"
    r"api\.resend\.com/emails"
    r"|api\.sendgrid\.com/v3/mail/send"
    r"|api\.postmarkapp\.com/email"
    r"|api\.(?:eu\.)?mailgun\.net/v3/[^\s'\"]+/messages"
    r"|api\.(?:brevo|sendinblue)\.com/v3/smtp/email"
    r"|api\.mailjet\.com/v3(?:\.1)?/send"
    r"|api\.agentmail\.(?:to|eu)/[^\s'\"]*/messages/send"
    r"|graph\.microsoft\.com/[^\s'\"]*/sendmail"
    r"|email\.[a-z0-9-]+\.amazonaws\.com/[^\s'\"]*"
    r")",
    re.IGNORECASE,
)

# An email API URL put in a variable (URL=https://api.resend.com/emails) for a later curl "$URL".
_API_ASSIGNMENT = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*=[\"']?" + EMAIL_API.pattern, re.IGNORECASE)

_VALUE = re.compile(r"(?:=|[ \t]+)(\"(?:[^\"\\]|\\.)*\"|\$'(?:[^'\\]|\\.)*'|'[^']*'|[^\s;|&]+)")


def _unquote(value: str) -> str:
    if value.startswith("$'") and value.endswith("'") and len(value) >= 3:
        return re.sub(r"\\(.)", r"\1", value[2:-1])
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
        if value and "\\" in value:
            value = re.sub(r"\\(.)", r"\1", value)
    return value


def _flag_values(seg: str, seg_skel: str, flags: Iterable[str]) -> list[str]:
    """Values of these flags in a command. Flags are found on the skeleton, so a flag
    written inside a quoted argument (an email body, say) doesn't count."""
    names = "|".join(re.escape(f) for f in flags)
    values: list[str] = []
    for match in re.finditer(r"(?:^|[ \t])(?:" + names + r")(?==|[ \t])", seg_skel):
        value = _VALUE.match(seg, match.end())
        if value:
            values.append(_unquote(value.group(1)))
    return values


def _flag_recipients(seg: str, seg_skel: str, flags: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in _flag_values(seg, seg_skel, flags):
        out.extend(emails_in(value))
    return out


def _header_flag_recipients(seg: str, seg_skel: str) -> list[str]:
    """himalaya `-H "To:jane@acme.co.uk"` header flags."""
    out: list[str] = []
    for value in _flag_values(seg, seg_skel, ("-H", "--header")):
        name, _, rest = value.partition(":")
        if name.strip().lower() in _RECIPIENT_HEADERS:
            out.extend(emails_in(rest))
    return out


# Sender, subject, attachment and command options whose values aren't recipients.
_NOT_RECIPIENT_FLAGS = ("-r", "-f", "-F", "-s", "-a", "-e", "--from", "--sender", "--subject",
                        "--read-envelope-from")
_READS_HEADERS = re.compile(r"(?:^|[ \t])(?:-[A-Za-z]*t[A-Za-z]*|--read-recipients)(?=[ \t]|$)")


def _mailer_recipients(seg: str, seg_skel: str) -> list[str]:
    """Addresses on a sendmail/msmtp/mail/mutt command line, without the sender's."""
    names = "|".join(re.escape(f) for f in _NOT_RECIPIENT_FLAGS)
    keep = list(seg)
    for match in re.finditer(r"(?:^|[ \t])(?:" + names + r")(?==|[ \t])", seg_skel):
        value = _VALUE.match(seg, match.end())
        if value:
            for i in range(match.start(), value.end()):
                keep[i] = " "
    return emails_in("".join(keep))


def _curl_data_values(seg: str) -> list[str] | None:
    """The request bodies a curl command sends (-d, --data*, --json, -F, --form), or None
    when the command line can't be split (an unclosed quote, say)."""
    try:
        tokens = shlex.split(seg, comments=False, posix=True)
    except ValueError:
        return None
    flags = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json", "-F", "--form", "--form-string"}
    values: list[str] = []
    for i, token in enumerate(tokens):
        if token in flags and i + 1 < len(tokens):
            values.append(tokens[i + 1])
        elif token.startswith("--") and "=" in token and token.split("=", 1)[0] in flags:
            values.append(token.split("=", 1)[1])
    return values


_LOOSE_KEY = re.compile(r"[\\\"']*([A-Za-z_][A-Za-z0-9_-]*)[\\\"']*\s*[:=]")


def _loose_recipients(text: str) -> list[str]:
    """Addresses whose nearest preceding recipient-or-sender key is a recipient key.

    For request bodies that can't be read directly: a body from a file, stdin
    or a shell variable, or a very large one.
    """
    out: list[str] = []
    for match in EMAIL_RE.finditer(text):
        window = text[max(0, match.start() - 80):match.start()]
        role = None
        for key in _LOOSE_KEY.finditer(window):
            k = _norm_key(key.group(1))
            if k in NOT_RECIPIENT_KEYS:
                role = "sender"
            elif k in RECIPIENT_KEYS and k not in ("email", "emails", "emailaddress"):
                role = "recipient"
            elif k in REPLY_KEYS:
                role = "reply"
        if role == "recipient":
            out.append(normalise_email(match.group(0)))
    return dedupe(out)


def _curl_recipients(seg: str, loose: Callable[[], list[str]]) -> list[str]:
    """Recipients in a curl request body, skipping bodies that are replies.

    A body read from a file, stdin or a shell variable is looked for in the
    whole command, where a heredoc or variable set before the curl holds it:
    `loose`.
    """
    values = _curl_data_values(seg) if len(seg) < MAX_SEGMENT else None
    if values is None:
        return loose()
    found: list[str] = []
    for value in values:
        if value.startswith("@"):
            found.extend(loose())
            continue
        try:
            data = json.loads(value)
        except ValueError:
            data = None
        if isinstance(data, (dict, list)):
            more, is_reply, _ = recipients_from_args(data)
            if not is_reply:
                found.extend(more)
            continue
        if "$" in value:
            found.extend(loose())  # "$PAYLOAD" or JSON built around variables
            continue
        pairs = [part.partition("=") for part in re.split(r"[&\n]", value)]
        form = {_norm_key(k.split("[", 1)[0]): v for k, sep, v in pairs if sep}
        if any(k in RECIPIENT_KEYS for k in form):
            if not any(k in REPLY_KEYS and v.strip() for k, v in form.items()):
                for k, v in form.items():
                    if k in RECIPIENT_KEYS:
                        found.extend(emails_in(v))
            continue
        found.extend(_loose_recipients(value))  # a body that isn't quite JSON
    return dedupe(found)


class _Segments:
    """Where each command in a shell command ends.

    A command ends at the next unquoted |, ;, &, ) or backtick, or line break,
    but a command substitution in its arguments ("$(date)", `cat body.txt`)
    is skipped over, and a command inside a substitution ends with it.
    """

    def __init__(self, skel: str, spans: list[tuple[int, int]]) -> None:
        self.skel = skel
        self.spans = sorted(set(spans))
        self.starts = [s for s, _ in self.spans]
        merged: list[list[int]] = []
        for span_start, span_end in self.spans:
            if merged and span_start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], span_end)
            else:
                merged.append([span_start, span_end])
        self.merged = merged
        self.merged_starts = [m[0] for m in merged]

    def end(self, start: int) -> int:
        limit = len(self.skel)
        for span_start, span_end in self.spans:
            if span_start >= start:
                break
            if span_end > start:
                limit = min(limit, span_end - 1)  # inside this substitution: stop before its closing ) or `
        limit = min(limit, start + MAX_SEGMENT)
        pos = start
        while pos < limit:
            found = _SEGMENT_END.search(self.skel, pos, limit)
            stop = found.start() if found else limit
            i = bisect.bisect_right(self.starts, pos)
            if i < len(self.starts) and self.starts[i] <= stop:
                pos = max(self.spans[i][1], self.starts[i] + 1)  # skip a substitution in the arguments
                continue
            return stop
        return limit

    def flat(self, start: int, end: int) -> str:
        """The skeleton of one command with the substitutions in its arguments blanked."""
        pieces: list[str] = []
        cursor = start
        i = bisect.bisect_right(self.merged_starts, start)
        while i < len(self.merged) and self.merged[i][0] < end:
            span_start, span_end = self.merged[i][0], min(self.merged[i][1], end)
            pieces.append(self.skel[cursor:span_start])
            pieces.append(" " * (span_end - span_start))
            cursor = span_end
            i += 1
        pieces.append(self.skel[cursor:end])
        return "".join(pieces)


def terminal_detection(command: Any) -> Detection | None:
    """An email send in a shell command, or None."""
    if not isinstance(command, str) or not command.strip():
        return None
    text = command[:MAX_COMMAND].replace("\\\r\n", " ").replace("\\\n", " ")
    if not _TRIGGER.search(text):
        return None
    skel, spans = _skeleton(text, 0, 0)
    segments = _Segments(skel, spans)
    budget = MAX_SCANNED
    channels: list[str] = []
    recipients: list[str] = []
    subjects: list[str] = []
    cache: dict[str, Any] = {}

    def written() -> Messages:
        if "messages" not in cache:
            cache["messages"] = read_messages(unpack_message_text(text))
        return cache["messages"]

    def loose() -> list[str]:
        if "loose" not in cache:
            cache["loose"] = _loose_recipients(text)
        return cache["loose"]

    api_in_variable = _API_ASSIGNMENT.search(text) is not None
    for count, match in enumerate(_COMMAND.finditer(skel)):
        word = match.group(1)
        start = match.start(1)
        end = segments.end(start)
        budget -= end - start
        if count >= MAX_COMMANDS or budget < 0:
            break
        seg, seg_skel = text[start:end], segments.flat(start, end)
        if word == "himalaya":
            if re.search(r"\b(?:template[ \t]+send|message[ \t]+(?:send|write))\b", seg_skel):
                channels.append("himalaya")
                recipients += _header_flag_recipients(seg, seg_skel) + written().recipients
                subjects.append(written().subject)
        elif word == "agentmail":
            if re.search(r"\bmessages[ \t]+send\b", seg_skel):
                channels.append("agentmail")
                recipients += _flag_recipients(seg, seg_skel, ("--to", "--cc", "--bcc"))
                subjects += _flag_values(seg, seg_skel, ("--subject",))[:1]
        elif word == "swaks":
            channels.append("swaks")
            recipients += _flag_recipients(seg, seg_skel, ("--to", "-t", "--cc", "--bcc"))
        elif word == "hermes":
            if re.search(r"\bsend\b", seg_skel):
                targets = [v for v in _flag_values(seg, seg_skel, ("--to", "-t")) if v.lower().startswith("email:")]
                if targets:
                    channels.append("hermes send")
                    for target in targets:
                        recipients += emails_in(target)
                    subjects += _flag_values(seg, seg_skel, ("--subject",))[:1]
        elif word == "curl":
            # The URL can also come from a variable set earlier: URL=https://api.resend.com/emails
            if EMAIL_API.search(seg) or (api_in_variable and "$" in seg):
                channels.append("email API")
                recipients += _curl_recipients(seg, loose)
        else:  # sendmail, msmtp, mail, mutt
            channels.append(word)
            recipients += _mailer_recipients(seg, seg_skel)
            subjects += _flag_values(seg, seg_skel, ("-s", "--subject"))[:1]
            if _READS_HEADERS.search(seg_skel):
                recipients += written().recipients  # -t: recipients come from the headers
                subjects.append(written().subject)

    for count, match in enumerate(_GMAIL_SEND.finditer(skel)):
        end = segments.end(match.start())
        budget -= end - match.start()
        if count >= MAX_COMMANDS or budget < 0:
            break
        seg, seg_skel = text[match.start():end], segments.flat(match.start(), end)
        channels.append("gmail send")
        if not [v for v in _flag_values(seg, seg_skel, ("--thread-id",)) if v.strip()]:
            recipients += _flag_recipients(seg, seg_skel, ("--to", "--cc", "--bcc"))
            subjects += _flag_values(seg, seg_skel, ("--subject",))[:1]

    if not channels:
        return None
    subject = next((_clean_subject(s) for s in subjects if s and s.strip()), "")
    return Detection(
        channel=", ".join(dedupe(channels)),
        recipients=dedupe(recipients)[:MAX_RECIPIENTS],
        subject=subject,
    )


# ---------------------------------------------------------------------------
# Entry point

TERMINAL_TOOLS = frozenset({"terminal"})


def detect(tool_name: Any, args: Any, extra_tools: Iterable[str] = ()) -> Detection | None:
    """The email send in this tool call, or None when it isn't one."""
    if not isinstance(tool_name, str) or not tool_name:
        return None
    args = args if isinstance(args, dict) else {}
    if tool_name in TERMINAL_TOOLS:
        return terminal_detection(args.get("command"))
    if tool_name.startswith("precleared_"):
        return None
    if matches_extra(tool_name, extra_tools) or is_email_send_tool(tool_name):
        recipients, reply, subject = recipients_from_args(args)
        return Detection(channel=tool_name, recipients=recipients, reply=reply, subject=subject)
    return None
