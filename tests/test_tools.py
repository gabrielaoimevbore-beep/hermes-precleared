import json

from conftest import RECEIPT, FakeCtx, FakeOpener, by_email, hooks, tools, verdict_payload


def run_check(ctx, args, answer, key=None):
    opener = FakeOpener(answer)
    handler = tools.make_check_handler(ctx, opener=opener, api_key_reader=lambda: key)
    return json.loads(handler(args)), opener


def test_check_returns_the_verdict_and_what_to_do(ctx):
    result, opener = run_check(ctx, {"email": "jane@acme.co.uk"}, lambda b, r: (200, verdict_payload("consent_needed")))
    assert result["verdict"] == "consent_needed" and result["send"] is False
    assert result["what_to_do"].startswith("Don't send")
    assert result["source"] == "PECR 2003 regulation 22(2)"
    assert result["receipt_url"] == f"https://precleared.dev/r/{RECEIPT}"
    assert result["checked_with"]["sender_country"] == "GB"
    assert result["allowance"] == {"free_remaining_today": 9, "credits_remaining": 0}
    assert "footer_text" not in result  # null in the API answer
    assert opener.requests[0]["body"]["address_source"] == "published"


def test_ok_answer_includes_the_footer(ctx):
    result, _ = run_check(ctx, {"email": "a@acme.co.uk"}, lambda b, r: (200, verdict_payload()))
    assert result["send"] is True and result["footer_text"].startswith("Say who is sending")


def test_arguments_override_the_defaults(ctx):
    _, opener = run_check(ctx, {
        "email": "a@acme.co.uk", "address_source": "bought", "pitch": "payroll", "existing_customer": True,
        "recipient_country": "de", "company_number": "01234567", "pitch_relevant": False,
    }, lambda b, r: (200, verdict_payload()))
    body = opener.requests[0]["body"]
    assert body["address_source"] == "bought" and body["pitch"] == "payroll"
    assert body["existing_customer"] is True and body["recipient_country"] == "DE"
    assert body["company_number"] == "01234567" and body["pitch_relevant"] is False


def test_check_needs_the_sender_country():
    result, opener = run_check(FakeCtx({}), {"email": "a@acme.co.uk"}, lambda b, r: (200, verdict_payload()))
    assert "sender_country GB" in result["error"] and opener.requests == []


def test_check_reports_bad_arguments_and_api_errors(ctx):
    assert "email address" in run_check(ctx, {}, lambda b, r: (200, {}))[0]["error"]
    assert "address_source" in run_check(ctx, {"email": "a@acme.co.uk", "address_source": "x"}, lambda b, r: (200, {}))[0]["error"]
    limited = run_check(ctx, {"email": "a@acme.co.uk"},
                        lambda b, r: (402, {"error": "payment_required", "buy_url": "https://precleared.dev/buy?k=abc"}))[0]
    assert "https://precleared.dev/buy?k=abc" in limited["error"]


def test_check_never_raises(ctx, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(tools.client, "check", boom)
    result, _ = run_check(ctx, {"email": "a@acme.co.uk"}, lambda b, r: (200, {}))
    assert "unexpectedly" in result["error"]


def test_a_check_with_default_facts_is_reused_by_the_pre_send_check(ctx):
    run_check(ctx, {"email": "a@acme.co.uk"}, lambda b, r: (200, verdict_payload()))
    opener = FakeOpener(by_email({}))
    hook = hooks.make_pre_tool_call(ctx, opener=opener, api_key_reader=lambda: None)
    assert hook(tool_name="inkbox_send_email", args={"to": "a@acme.co.uk"}) is None
    assert opener.requests == []


def test_a_check_with_other_facts_is_not_reused(ctx):
    run_check(ctx, {"email": "a@acme.co.uk", "existing_customer": True}, lambda b, r: (200, verdict_payload()))
    opener = FakeOpener(by_email({"a@acme.co.uk": "consent_needed"}))
    hook = hooks.make_pre_tool_call(ctx, opener=opener, api_key_reader=lambda: None)
    assert hook(tool_name="inkbox_send_email", args={"to": "a@acme.co.uk"})["action"] == "approve"
    assert len(opener.requests) == 1


def run_receipt(ctx, args, answer):
    opener = FakeOpener(answer)
    handler = tools.make_receipt_handler(ctx, opener=opener, api_key_reader=lambda: None)
    return json.loads(handler(args)), opener


def test_receipt_is_fetched_and_trimmed(ctx):
    payload = {"id": RECEIPT, "verdict": "consent_needed", "signature": "abc==", "algorithm": "ed25519",
               "rules_applied": ["gb_individual_consent"], "unexpected": {"deep": "x"}}
    result, opener = run_receipt(ctx, {"receipt_id": RECEIPT.lower().replace("cc_rcpt_", "CC_RCPT_")}, lambda b, r: (200, payload))
    assert opener.requests[0]["url"].endswith(f"id={RECEIPT}")
    assert result["verdict"] == "consent_needed" and result["algorithm"] == "ed25519"
    assert result["receipt_url"] == f"https://precleared.dev/r/{RECEIPT}"
    assert "unexpected" not in result


def test_receipt_errors(ctx):
    assert "cc_rcpt_" in run_receipt(ctx, {"receipt_id": "nope"}, lambda b, r: (200, {}))[0]["error"]
    missing = run_receipt(ctx, {"receipt_id": RECEIPT}, lambda b, r: (404, {"error": "not found"}))[0]
    assert "no receipt" in missing["error"]
