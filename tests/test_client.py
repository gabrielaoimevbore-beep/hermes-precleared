import socket
import urllib.error

from conftest import RECEIPT, FakeOpener, client, verdict_payload


def test_build_check_body_uses_settings_and_drops_blanks():
    body, problem = client.build_check_body(
        "Jane Doe <Jane@Acme.co.uk>", "gb", address_source="Published", pitch="  bookkeeping  ",
        prospect_country="uk", existing_customer="no", recipient_country="", company_number=" 01234567 ",
        pitch_relevant="true",
    )
    assert problem is None
    assert body == {
        "recipient_email": "jane@acme.co.uk",
        "sender_country": "GB",
        "address_source": "published",
        "pitch": "bookkeeping",
        "default_recipient_country": "GB",
        "company_number": "01234567",
        "pitch_relevant": True,
        "existing_customer": False,
    }


def test_build_check_body_rejects_bad_input():
    assert client.build_check_body("not an address", "GB")[1]
    assert "GB or US" in client.build_check_body("a@acme.co.uk", "FR")[1]
    assert "address_source" in client.build_check_body("a@acme.co.uk", "GB", address_source="found")[1]
    assert "two-letter" in client.build_check_body("a@acme.co.uk", "US", recipient_country="Germany")[1]


def test_build_check_body_leaves_out_pitch_relevant_when_unsure():
    body, _ = client.build_check_body("a@acme.co.uk", "US", pitch_relevant="maybe")
    assert "pitch_relevant" not in body
    assert body["existing_customer"] is False


def test_check_sends_json_with_user_agent_and_key():
    opener = FakeOpener(lambda body, req: (200, verdict_payload()))
    body, _ = client.build_check_body("a@acme.co.uk", "GB")
    result = client.check(body, "pc_live_secret", timeout=7, opener=opener)
    assert result.status == 200 and client.verdict_of(result.body) == "ok_to_email"
    sent = opener.requests[0]
    assert sent["url"] == "https://precleared.dev/api/v1/check" and sent["method"] == "POST"
    assert sent["headers"]["authorization"] == "Bearer pc_live_secret"
    assert sent["headers"]["user-agent"].startswith("hermes-precleared/")
    assert sent["headers"]["content-type"] == "application/json"
    assert sent["timeout"] == 7
    assert sent["body"]["recipient_email"] == "a@acme.co.uk"


def test_check_without_key_sends_no_authorization():
    opener = FakeOpener(lambda body, req: (200, verdict_payload()))
    client.check({"recipient_email": "a@acme.co.uk", "sender_country": "GB"}, None, opener=opener)
    assert "authorization" not in opener.requests[0]["headers"]


def test_answer_without_verdict_is_an_error():
    opener = FakeOpener(lambda body, req: (200, {"hello": "world"}))
    result = client.check({"recipient_email": "a@acme.co.uk"}, None, opener=opener)
    assert result.error and "not cleared" in result.error


def test_http_errors_come_back_as_results():
    opener = FakeOpener(lambda body, req: (429, {"error": "daily_limit", "message": "Keyless use is over for today.",
                                                  "get_key_url": "https://precleared.dev/get-key?ref=limit"}))
    result = client.check({"recipient_email": "a@acme.co.uk"}, None, opener=opener)
    assert result.status == 429
    message = client.error_message(result)
    assert "Keyless use is over for today." in message
    assert "https://precleared.dev/get-key?ref=limit" in message


def test_operator_message_is_preferred_for_limits():
    result = client.ApiResult(402, {"error": "payment_required", "message": "machine text",
                                    "operator_message": "Your free checks are used. Buy a pack: https://precleared.dev/buy?k=1",
                                    "buy_url": "https://precleared.dev/buy?k=1"})
    message = client.error_message(result)
    assert message == "Your free checks are used. Buy a pack: https://precleared.dev/buy?k=1"


def test_402_falls_back_to_buy_page_for_odd_links():
    result = client.ApiResult(402, {"error": "payment_required", "buy_url": "javascript:alert(1)"})
    assert client.BUY_URL in client.error_message(result)


def test_rate_limited_says_when_to_retry():
    message = client.error_message(client.ApiResult(429, {"error": "rate_limited", "retry_after": 12}))
    assert "too many checks" in message and "12 seconds" in message


def test_401_and_unexpected_status_messages_hide_the_key():
    key = "pc_live_abcdef"
    assert key not in client.error_message(client.ApiResult(401, {"message": f"bad key {key}"}), key)
    assert "HTTP 503" in client.error_message(client.ApiResult(503, None))
    msg = client.error_message(client.ApiResult(400, {"message": f"bad {key}"}), key)
    assert key not in msg and "***" in msg


def test_network_failures_are_plain_messages():
    def timeout(body, req):
        raise socket.timeout("timed out")

    def down(body, req):
        raise urllib.error.URLError("no route")

    assert "in time" in client.check({}, None, opener=FakeOpener(timeout)).error
    assert "Could not reach" in client.check({}, None, opener=FakeOpener(down)).error


def test_non_json_error_body_is_handled():
    result = client.check({}, None, opener=FakeOpener(lambda b, r: (502, b"<html>bad gateway</html>")))
    assert result.status == 502 and result.body is None
    assert "HTTP 502" in client.error_message(result)


def test_reasons_and_receipts_are_parsed():
    payload = verdict_payload("consent_needed")
    rule, source, link = client.first_reason(payload)
    assert rule.startswith("The recipient is a sole trader")
    assert source == "PECR 2003 regulation 22(2)"
    assert link.startswith("https://www.legislation.gov.uk/")
    assert client.receipt_id_of(payload) == RECEIPT
    line = client.one_line_reason(payload)
    assert "(PECR 2003 regulation 22(2))" in line and "Do not cold email" in line


def test_reason_links_must_be_https():
    payload = verdict_payload()
    payload["reasons"][0]["source_url"] = "http://example.com/x"
    assert client.first_reason(payload)[2] == ""


def test_receipt_ids_are_normalised():
    assert client.normalise_receipt_id(" cc_rcpt_0123456789abcdef ") == RECEIPT
    assert client.normalise_receipt_id("cc_rcpt_123") == ""
    assert client.normalise_receipt_id(None) == ""


def test_get_receipt_puts_the_id_in_the_query():
    opener = FakeOpener(lambda body, req: (200, {"id": RECEIPT}))
    client.get_receipt(RECEIPT, None, opener=opener)
    assert opener.requests[0]["url"] == f"https://precleared.dev/api/v1/receipt?id={RECEIPT}"
    assert opener.requests[0]["method"] == "GET"
