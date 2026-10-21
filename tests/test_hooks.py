import threading
import time

from conftest import FakeCtx, by_email, cache, hooks, verdict_payload

SEND = "inkbox_send_email"


def test_cleared_recipients_go_ahead_untouched(ctx, make_hook):
    hook, opener = make_hook(ctx, by_email({}))
    assert hook(tool_name=SEND, args={"to": ["a@acme.co.uk", "b@acme.co.uk"]}, task_id="t1") is None
    assert len(opener.requests) == 2
    body = opener.requests[0]["body"]
    assert body["sender_country"] == "GB" and body["address_source"] == "published"
    assert body["pitch"] == "bookkeeping software" and body["existing_customer"] is False


def test_held_recipient_goes_to_the_approval_prompt(ctx, make_hook):
    hook, _ = make_hook(ctx, by_email({"jane@acme.co.uk": "consent_needed"}))
    result = hook(tool_name=SEND, args={"to": "jane@acme.co.uk", "subject": "Hi"}, task_id="t1")
    assert result["action"] == "approve"
    assert result["rule_key"].startswith("precleared:") and "@" not in result["rule_key"]
    message = result["message"]
    assert message.startswith("Precleared hasn't cleared this email (via inkbox_send_email, subject \"Hi\").")
    assert "\n- jane@acme.co.uk: consent_needed." in message
    assert "needs prior consent (PECR 2003 regulation 22(2))." in message
    assert "https://precleared.dev/r/cc_rcpt_" in message
    assert message.endswith(hooks.ASK_TAIL)


def test_only_held_recipients_are_listed(ctx, make_hook):
    hook, _ = make_hook(ctx, by_email({"b@acme.co.uk": "blocked"}))
    result = hook(tool_name=SEND, args={"to": ["a@acme.co.uk", "b@acme.co.uk", "c@acme.co.uk"]})
    assert "1 of the 3 recipients" in result["message"]
    assert "- b@acme.co.uk: blocked." in result["message"]
    assert "a@acme.co.uk" not in result["message"]
    assert result["rule_key"] == hooks.rule_key_for([hooks.Outcome("b@acme.co.uk", verdict="blocked")])


def test_unknown_is_held(ctx, make_hook):
    hook, _ = make_hook(ctx, by_email({"u@acme.co.uk": "unknown"}))
    assert hook(tool_name=SEND, args={"to": "u@acme.co.uk"})["action"] == "approve"


def test_block_mode_refuses(make_hook):
    ctx = FakeCtx({"sender_country": "US", "mode": "block"})
    hook, _ = make_hook(ctx, by_email({"x@acme.com": "consent_needed"}))
    result = hook(tool_name=SEND, args={"to": "x@acme.com"})
    assert result["action"] == "block"
    assert "The email was not sent." in result["message"]
    assert "rule_key" not in result


def test_off_mode_checks_nothing(make_hook):
    ctx = FakeCtx({"sender_country": "GB", "mode": "off"})
    hook, opener = make_hook(ctx, by_email({"x@acme.co.uk": "blocked"}))
    assert hook(tool_name=SEND, args={"to": "x@acme.co.uk"}) is None
    assert opener.requests == []


def test_not_configured_asks_for_the_sender_country(make_hook):
    ctx = FakeCtx({})
    hook, opener = make_hook(ctx, by_email({}))
    result = hook(tool_name=SEND, args={"to": "x@acme.co.uk"})
    assert result["action"] == "approve" and result["rule_key"].startswith("precleared:setup:")
    assert "@" not in result["rule_key"]
    assert "hermes config set plugins.entries.precleared.settings.sender_country GB" in result["message"]
    assert opener.requests == []


def test_unknown_mode_falls_back_to_ask(make_hook):
    ctx = FakeCtx({"sender_country": "GB", "mode": "loud"})
    hook, _ = make_hook(ctx, by_email({"x@acme.co.uk": "blocked"}))
    assert hook(tool_name=SEND, args={"to": "x@acme.co.uk"})["action"] == "approve"


def test_other_tools_and_replies_are_left_alone(ctx, make_hook):
    hook, opener = make_hook(ctx, by_email({"x@acme.co.uk": "blocked"}))
    assert hook(tool_name="web_search", args={"query": "x@acme.co.uk"}) is None
    assert hook(tool_name="read_file", args={"path": "/tmp/x"}) is None
    assert hook(tool_name="mcp__gmail__send_message", args={"to": "x@acme.co.uk", "threadId": "t"}) is None
    assert hook(tool_name="terminal", args={"command": "ls"}) is None
    assert hook(tool_name="mcp__mailchimp__send_campaign", args={"campaign_id": "c1"}) is None
    assert opener.requests == []


def test_test_and_ignored_domains_are_not_checked(make_hook):
    ctx = FakeCtx({"sender_country": "GB", "ignore_domains": ["mine.com"]})
    hook, opener = make_hook(ctx, by_email({}))
    assert hook(tool_name=SEND, args={"to": ["boss@mine.com", "dev@example.com", "a@team.mine.com"]}) is None
    assert opener.requests == []


def test_terminal_sends_are_checked(ctx, make_hook):
    hook, opener = make_hook(ctx, by_email({"lead@acme.co.uk": "consent_needed"}))
    command = "cat << 'EOF' | himalaya template send\nFrom: me@mine.com\nTo: lead@acme.co.uk\nSubject: Hi\n\nHello\nEOF"
    result = hook(tool_name="terminal", args={"command": command})
    assert result["action"] == "approve"
    assert [r["body"]["recipient_email"] for r in opener.requests] == ["lead@acme.co.uk"]


def test_verdicts_are_cached(ctx, make_hook):
    hook, opener = make_hook(ctx, by_email({"held@acme.co.uk": "consent_needed"}))
    args = {"to": ["ok@acme.co.uk", "held@acme.co.uk"]}
    first = hook(tool_name=SEND, args=args)
    second = hook(tool_name=SEND, args=args)
    assert len(opener.requests) == 2  # both answered from the cache the second time
    assert first == second
    stored = ctx.state.data[cache.STATE_KEY]
    assert all("@" not in key for key in stored)


def test_changing_the_facts_means_a_fresh_check(ctx, make_hook):
    hook, opener = make_hook(ctx, by_email({}))
    hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    ctx.config["pitch"] = "payroll software"
    hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    assert len(opener.requests) == 2


def test_cache_expires_after_30_days(ctx, make_hook):
    now = [1_800_000_000.0]
    hook, opener = make_hook(ctx, by_email({}), clock=lambda: now[0])
    hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    now[0] += 29 * 86400
    hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    assert len(opener.requests) == 1
    now[0] += 2 * 86400
    hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    assert len(opener.requests) == 2


def test_failed_checks_hold_the_email_and_are_not_cached(ctx, make_hook):
    def limited(body, request):
        return 429, {"error": "daily_limit", "message": "Keyless use is over for today.",
                     "get_key_url": "https://precleared.dev/get-key?ref=limit"}

    hook, opener = make_hook(ctx, limited)
    result = hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    assert result["action"] == "approve"
    assert "- a@acme.co.uk: not checked. Keyless use is over for today." in result["message"]
    assert "https://precleared.dev/get-key?ref=limit" in result["message"]
    hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    assert len(opener.requests) == 2


def test_the_key_is_sent_but_never_shown(ctx, make_hook):
    key = "pc_live_supersecret"

    def bad_key(body, request):
        return 401, {"error": "invalid_key", "message": f"key {key} is not valid"}

    hook, opener = make_hook(ctx, bad_key, key=key)
    result = hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    assert opener.requests[0]["headers"]["authorization"] == f"Bearer {key}"
    assert key not in result["message"]
    assert "rejected the API key" in result["message"]


def test_too_many_new_recipients_are_not_all_checked(ctx, make_hook):
    hook, opener = make_hook(ctx, by_email({}))
    recipients = [f"p{i}@acme.co.uk" for i in range(13)]
    result = hook(tool_name=SEND, args={"to": recipients})
    assert len(opener.requests) == hooks.MAX_CHECKS_PER_SEND
    assert result["action"] == "approve"
    assert "3 of the 13 recipients" in result["message"]
    assert result["message"].count("smaller batches") == 1
    assert "p10@acme.co.uk, p11@acme.co.uk, p12@acme.co.uk" in result["message"]


def test_slow_checks_are_cut_off_at_the_deadline(ctx, make_hook, monkeypatch):
    monkeypatch.setattr(hooks, "DEADLINE", 0.3)
    release = threading.Event()

    def slow(body, request):
        release.wait(5)
        return 200, verdict_payload()

    hook, _ = make_hook(ctx, slow)
    started = time.monotonic()
    result = hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    release.set()
    assert time.monotonic() - started < 3
    assert result["action"] == "approve"
    assert "did not answer in time" in result["message"]


def test_a_crash_while_inspecting_lets_the_tool_run(ctx, make_hook, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(hooks.gate, "detect", boom)
    hook, _ = make_hook(ctx, by_email({}))
    assert hook(tool_name="web_search", args={}) is None


def test_a_crash_while_checking_holds_the_email(ctx, make_hook, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(hooks, "run_checks", boom)
    hook, _ = make_hook(ctx, by_email({}))
    result = hook(tool_name=SEND, args={"to": "a@acme.co.uk"})
    assert result["action"] == "approve"
    assert "unexpected error" in result["message"]


def test_broken_state_does_not_stop_checks(ctx, make_hook):
    class BrokenState:
        def get(self, key, default=None):
            raise RuntimeError("Cannot parse plugin state")

        def set(self, key, value):
            raise RuntimeError("disk full")

    ctx.state = BrokenState()
    hook, opener = make_hook(ctx, by_email({}))
    assert hook(tool_name=SEND, args={"to": "a@acme.co.uk"}) is None
    assert len(opener.requests) == 1


def test_messages_are_bounded(ctx, make_hook):
    hook, _ = make_hook(ctx, by_email({f"p{i}@acme.co.uk": "blocked" for i in range(10)}))
    result = hook(tool_name=SEND, args={"to": [f"p{i}@acme.co.uk" for i in range(10)]})
    assert "any of the 10 recipients" in result["message"]
    assert "- And 5 more." in result["message"]
    assert len(result["message"]) <= hooks.MAX_MESSAGE + len(hooks.ASK_TAIL) + 1


def test_rule_key_covers_exactly_the_held_recipients_and_verdicts():
    O = hooks.Outcome
    a, b, c = O("a@x.com", verdict="blocked"), O("b@x.com", verdict="consent_needed"), O("c@x.com", verdict="blocked")
    assert hooks.rule_key_for([b, a]) == hooks.rule_key_for([a, b])
    assert hooks.rule_key_for([a, b]) != hooks.rule_key_for([a, c])
    assert hooks.rule_key_for([O("a@x.com", error="limit")]) != hooks.rule_key_for([a])
    assert "@" not in hooks.rule_key_for([a])


def test_mode_off_stored_as_yaml_false_is_off(make_hook):
    ctx = FakeCtx({"sender_country": "GB", "mode": False})
    hook, opener = make_hook(ctx, by_email({"x@acme.co.uk": "blocked"}))
    assert hook(tool_name=SEND, args={"to": "x@acme.co.uk"}) is None
    assert opener.requests == []


def test_prospect_country_norway_stored_as_yaml_false(make_hook):
    ctx = FakeCtx({"sender_country": "GB", "prospect_country": False})
    hook, opener = make_hook(ctx, by_email({}))
    hook(tool_name=SEND, args={"to": "x@acme.no"})
    assert opener.requests[0]["body"]["default_recipient_country"] == "NO"


def test_block_message_tells_the_agent_what_to_do(make_hook):
    ctx = FakeCtx({"sender_country": "GB", "mode": "block"})
    hook, _ = make_hook(ctx, by_email({"x@acme.co.uk": "consent_needed"}))
    command = "cat << 'EOF' | himalaya template send\nFrom: me@mine.com\nTo: x@acme.co.uk\nSubject: Intro\n\nHi\nEOF"
    message = hook(tool_name="terminal", args={"command": command})["message"]
    assert message.startswith('Precleared hasn\'t cleared this email (via himalaya, subject "Intro").')
    assert message.endswith(hooks.BLOCK_TAIL)
