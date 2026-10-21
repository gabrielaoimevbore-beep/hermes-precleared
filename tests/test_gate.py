import time

import pytest

from conftest import gate


def detect_cmd(command):
    return gate.detect("terminal", {"command": command})


def recipients_of(command):
    detection = detect_cmd(command)
    return None if detection is None else detection.recipients


# --- tool names ---------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "inkbox_send_email", "openmail_send", "bento_email_send", "mcp__gmail__send_message",
    "mcp__agentmail__send_message", "mcp__outlook__sendMail", "mcp__resend__send_email",
    "mcp__smtp__send", "mcp__my_mail_server__send_bulk_email", "sendgrid_send", "GMAIL_SEND_EMAIL",
    "mcp__postmark__sendEmailWithTemplate", "send_template_email",
])
def test_send_tools_are_recognised(name):
    assert gate.is_email_send_tool(name)


@pytest.mark.parametrize("name", [
    "openmail_reply", "mcp__gmail__create_draft", "mcp__gmail__send_draft", "mcp__slack__send_message",
    "inkbox_send_sms", "mcp__mailgun__update_sender_identity", "mcp__gmail__list_messages",
    "mcp__mailerlite__send_test_automation", "mcp__gmail__forward_message", "send_receipt_email",
    "web_search", "terminal", "precleared_check", "", "mcp__email__search", "GMAIL_REPLY_TO_THREAD",
])
def test_other_tools_are_not(name):
    assert not gate.is_email_send_tool(name)


def test_extra_tools_match_names_and_patterns():
    assert gate.detect("mcp__instantly__create_lead", {"email": "lead@acme.co.uk"}, ["mcp__instantly__create_lead"])
    assert gate.detect("mcp__smtp__deliver", {"to": "a@acme.co.uk"}, ["mcp__smtp__*"])
    assert gate.detect("mcp__instantly__create_lead", {"email": "lead@acme.co.uk"}, []) is None


def test_own_tools_are_never_detected():
    assert gate.detect("precleared_check", {"email": "a@acme.co.uk"}, ["*"]) is None


# --- structured arguments -----------------------------------------------------

def test_recipients_come_from_recipient_fields_only():
    d = gate.detect("inkbox_send_email", {
        "to": ["Jane Doe <Jane@Acme.co.uk>"], "cc": "bob@acme.co.uk, carol@acme.co.uk",
        "from": "me@mine.com", "reply_to": "replies@mine.com", "subject": "Quick question",
        "body": "Write to info@other.com or To: z@z.com",
    })
    assert d.recipients == ["jane@acme.co.uk", "bob@acme.co.uk", "carol@acme.co.uk"]
    assert not d.reply
    assert d.subject == "Quick question"


def test_nested_recipient_shapes():
    graph = {"message": {"toRecipients": [{"emailAddress": {"address": "t@acme.co.uk", "name": "T"}}],
                         "ccRecipients": [{"emailAddress": {"address": "c@acme.co.uk"}}]}}
    assert gate.detect("mcp__outlook__sendMail", graph).recipients == ["t@acme.co.uk", "c@acme.co.uk"]
    sendgrid = {"personalizations": [{"to": [{"email": "p@acme.co.uk"}]}], "from": {"email": "me@mine.com"}}
    assert gate.detect("sendgrid_send", sendgrid).recipients == ["p@acme.co.uk"]
    ses = {"Destination": {"ToAddresses": ["s@acme.co.uk"]}, "Source": "me@mine.com"}
    assert gate.detect("mcp__ses__send_email", ses).recipients == ["s@acme.co.uk"]


def test_composio_style_tools():
    d = gate.detect("GMAIL_SEND_EMAIL", {"recipient_email": "lead@acme.co.uk", "extra_recipients": ["two@acme.co.uk"],
                                         "subject": "Hi", "body": "Hello"})
    assert d.recipients == ["lead@acme.co.uk", "two@acme.co.uk"]


def test_sender_account_fields_are_skipped():
    d = gate.detect("mcp__agentmail__send_message", {"inbox_id": "me@agentmail.to", "to": ["x@acme.co.uk"],
                                                     "account": {"email": "me@mine.com"}})
    assert d.recipients == ["x@acme.co.uk"]


def test_thread_ids_mark_a_reply_but_references_do_not():
    assert gate.detect("mcp__gmail__send_message", {"to": "a@acme.co.uk", "threadId": "18c2"}).reply
    assert gate.detect("mcp__gmail__send_message", {"to": "a@acme.co.uk", "in_reply_to": "<id@x>"}).reply
    assert not gate.detect("mcp__gmail__send_message", {"to": "a@acme.co.uk", "threadId": ""}).reply
    assert not gate.detect("mcp__any__send_email", {"to": "a@acme.co.uk", "references": ["https://acme.co.uk/case"]}).reply


def test_raw_message_text_is_read_for_headers():
    d = gate.detect("mcp__any__send_email", {"raw": "From: me@mine.com\nTo: r@acme.co.uk\nSubject: x\n\nTo: no@body.com"})
    assert d.recipients == ["r@acme.co.uk"] and d.subject == "x"


def test_a_send_with_no_visible_recipients_has_none():
    d = gate.detect("mcp__mailchimp__send_campaign", {"campaign_id": "abc"})
    assert d is not None and d.recipients == []


def test_huge_arguments_are_bounded():
    args = {"to": [f"user{i}@acme.co.uk" for i in range(1000)]}
    assert len(gate.detect("inkbox_send_email", args).recipients) <= gate.MAX_RECIPIENTS


# --- terminal: the email skills that ship with Hermes ---------------------------

def test_himalaya_new_message():
    d = detect_cmd(
        "cat << 'EOF' | himalaya template send\n"
        "From: you@sender.co.uk\n"
        "To: Jane Doe <jane@acme.co.uk>, bob@widgets.de\n"
        "Cc: carol@example.fr\n"
        "Subject: Test Message\n"
        "\n"
        "To: notme@body.com is body text\n"
        "EOF"
    )
    assert d.channel == "himalaya"
    assert d.recipients == ["jane@acme.co.uk", "bob@widgets.de", "carol@example.fr"]
    assert d.subject == "Test Message"


def test_himalaya_folded_header_and_two_messages():
    assert recipients_of(
        "cat <<EOF | himalaya template send\nFrom: me@m.com\nTo: one@acme.co.uk,\n two@acme.co.uk\nSubject: a\n\nbody\nEOF\n"
        "cat <<'MSG' | himalaya template send\nFrom: me@m.com\nTo: three@acme.co.uk\n\nTo: body@x.com\nMSG\n"
    ) == ["one@acme.co.uk", "two@acme.co.uk", "three@acme.co.uk"]


def test_himalaya_replies_and_forwards_have_no_recipients_to_check():
    assert recipients_of("cat << 'EOF' | himalaya template send\nFrom: me@m.com\nTo: s@acme.com\n"
                         "Subject: Re: x\nIn-Reply-To: <original-message-id>\n\nThanks\nEOF") == []
    assert recipients_of("himalaya template reply 42 | sed 's/^$/\\nYour reply\\n/' | himalaya template send") == []
    assert recipients_of("himalaya template forward 42 | sed 's/^To:.*/To: new@acme.com/' | himalaya template send") == []


def test_a_reply_in_a_batch_does_not_exempt_the_other_messages():
    command = (
        "cat <<EOF | himalaya template send\nFrom: me@m.com\nTo: old@acme.com\nIn-Reply-To: <x@y>\n\nThanks\nEOF\n"
        "cat <<EOF | himalaya template send\nFrom: me@m.com\nTo: new@acme.com\n\nHello\nEOF\n"
    )
    assert recipients_of(command) == ["new@acme.com"]


@pytest.mark.parametrize("command, expected", [
    ("printf 'To: lead@acme.co.uk\\nSubject: Hi\\n\\nHello' | himalaya template send", ["lead@acme.co.uk"]),
    ("printf 'From: me@mine.com\\nTo: lead@acme.co.uk\\nSubject: Hi\\n\\nBody' | himalaya template send", ["lead@acme.co.uk"]),
    ("printf 'To: a@acme.co.uk\\nCc: b@acme.co.uk\\n\\nHi' | himalaya template send", ["a@acme.co.uk", "b@acme.co.uk"]),
    ('echo -e "To: lead@acme.co.uk\\nSubject: Hi\\n\\nHello" | sendmail -t', ["lead@acme.co.uk"]),
    ("printf 'To: lead@acme.co.uk\\n\\nHi' | msmtp -t", ["lead@acme.co.uk"]),
])
def test_messages_written_with_printf_or_echo(command, expected):
    assert recipients_of(command) == expected


def test_himalaya_header_flags():
    assert recipients_of('himalaya message write -H "To:recipient@acme.co.uk" -H "Subject:Test" "Message body"') == ["recipient@acme.co.uk"]


def test_himalaya_reading_mail_is_not_a_send():
    assert detect_cmd("himalaya envelope list --page-size 20") is None
    assert detect_cmd("himalaya message read 42") is None


def test_agentmail_send_with_line_continuations():
    d = detect_cmd(
        "agentmail inboxes:messages send \\\n  --inbox-id support@agentmail.to \\\n  --to customer@acme.io \\\n"
        "  --cc 'boss@acme.io' \\\n  --subject \"Hello\" \\\n  --text \"Plain-text body.\" \\\n  --format json"
    )
    assert d.channel == "agentmail"
    assert d.recipients == ["customer@acme.io", "boss@acme.io"]
    assert d.subject == "Hello"


def test_agentmail_reply_is_not_a_send():
    assert detect_cmd("agentmail inboxes:messages reply --inbox-id a@agentmail.to --message-id m1 --text hi") is None


def test_google_workspace_gmail_send():
    d = detect_cmd('$GAPI gmail send --to user@acme.co.uk --subject "Hello" '
                   "--from '\"Research Agent\" <me@mine.com>' --body \"Message text\"")
    assert d.channel == "gmail send" and d.recipients == ["user@acme.co.uk"] and d.subject == "Hello"
    assert recipients_of('$GAPI gmail send --to user@acme.co.uk --subject "Re: x" --body hi --thread-id 18c2') == []
    assert detect_cmd('$GAPI gmail reply MESSAGE_ID --body "Thanks"') is None
    assert detect_cmd('$GAPI gmail search "is:unread" --max 10') is None


def test_quoted_display_names_in_flags():
    assert recipients_of('$GAPI gmail send --to "\\"Jane Doe\\" <jane@acme.co.uk>" --subject s --body b') == ["jane@acme.co.uk"]


# --- terminal: other ways of sending --------------------------------------------

def test_unix_mailers():
    assert recipients_of('echo hi | mail -s "Hello there" -r me@mine.com prospect@acme.co.uk') == ["prospect@acme.co.uk"]
    d = detect_cmd("/usr/sbin/sendmail -t <<EOF\nFrom: me@mine.com\nTo: x@acme.co.uk\nSubject: s\n\nb\nEOF")
    assert d.channel == "sendmail" and d.recipients == ["x@acme.co.uk"]
    assert recipients_of("swaks --to a@acme.co.uk --from me@mine.com --server smtp.x.com") == ["a@acme.co.uk"]
    assert recipients_of("sudo -u mailer mail -s hi lead@acme.co.uk < body.txt") == ["lead@acme.co.uk"]
    assert recipients_of("LANG=C mail -s hi lead@acme.co.uk < body.txt") == ["lead@acme.co.uk"]


def test_shell_variables_are_not_read_as_addresses():
    assert recipients_of("for x in a b; do echo body | mail -s hi $x@acme.com; done") == []


def test_hermes_send_to_email():
    assert recipients_of('hermes send --to email:jane@acme.co.uk "hello"') == ["jane@acme.co.uk"]
    assert recipients_of('hermes send -t email:jane@acme.co.uk "hello"') == ["jane@acme.co.uk"]
    assert detect_cmd('hermes send --to telegram "hello"') is None


def test_curl_to_email_apis():
    resend = ("curl -s https://api.resend.com/emails -H 'Content-Type: application/json' "
              "-d '{\"from\": \"me@mine.com\", \"to\": [\"a@acme.co.uk\", \"b@acme.co.uk\"], \"subject\": \"hi\"}'")
    assert recipients_of(resend) == ["a@acme.co.uk", "b@acme.co.uk"]
    sendgrid = ("curl https://api.sendgrid.com/v3/mail/send -d '{\"personalizations\":[{\"to\":[{\"email\":\"p@acme.co.uk\"}]}],"
                "\"from\":{\"email\":\"me@mine.com\"}}'")
    assert recipients_of(sendgrid) == ["p@acme.co.uk"]
    mailgun = ("curl -s --user 'api:KEY' https://api.mailgun.net/v3/mg.mine.com/messages "
               "-F from='Me <me@mine.com>' -F to='lead@acme.co.uk' -F subject='Hi'")
    assert recipients_of(mailgun) == ["lead@acme.co.uk"]
    brevo = ("curl https://api.brevo.com/v3/smtp/email --data-binary @- <<EOF\n"
             "{\"sender\": {\"email\": \"me@mine.com\"}, \"to\": [{\"email\": \"lead@acme.co.uk\"}]}\nEOF")
    assert recipients_of(brevo) == ["lead@acme.co.uk"]
    agentmail_eu = ("curl -X POST \"https://api.agentmail.eu/v0/inboxes/me@agentmail.eu/messages/send\" "
                    "-d '{\"to\": [\"lead@acme.de\"], \"subject\": \"Hallo\"}'")
    assert recipients_of(agentmail_eu) == ["lead@acme.de"]


def test_a_reply_beside_a_cold_send_does_not_exempt_it():
    command = ("himalaya template reply 42 | himalaya template send && "
               "curl https://api.resend.com/emails -d '{\"from\": \"me@mine.com\", \"to\": \"lead@acme.co.uk\"}'")
    assert recipients_of(command) == ["lead@acme.co.uk"]


def test_curl_elsewhere_is_not_a_send():
    assert detect_cmd("curl -s https://api.github.com/users/octocat -d '{\"to\": \"a@acme.co.uk\"}'") is None


@pytest.mark.parametrize("command", [
    "ls -la && grep -r foo@bar.com .",
    "git log --author=me@mine.com",
    "python3 script.py",
    "cat emails.txt | wc -l",
    "echo 'mail me at a@b.com'",
    'git commit -m "Add contact page (mail hello@acme.co.uk)"',
    "cat > settings.py << 'EOF'\nmail = \"ops@acme.co.uk\"\nsendmail -t ops@acme.co.uk\nEOF",
    "python3 -c \"import x; mail = 'ops@acme.co.uk'\"",
    "grep -rn 'gmail send --to lead@acme.co.uk' docs/",
    "# mail lead@acme.co.uk later\nls",
    "",
])
def test_ordinary_commands_hold_nothing(command):
    detection = detect_cmd(command)
    assert detection is None or detection.recipients == [], command


# --- speed: this runs on every terminal call --------------------------------------

def _timed(command):
    started = time.perf_counter()
    detect_cmd(command)
    return time.perf_counter() - started


@pytest.mark.parametrize("command", [
    "cat > out.txt << 'EOF'\n" + "\n" * 400_000 + "EOF",
    "\n" * 400_000 + "mail",
    "cat << 'EOF' | himalaya template send\nTo: a@acme.co.uk\n\n" + "std::cout << value << std::endl;\n" * 12_000 + "EOF",
    "curl https://api.resend.com/emails -d '{\"to\": \"a@acme.co.uk\", \"attachments\": [{\"content\": \""
    + "QUJD" * 75_000 + "\"}]}'",
    "a" * 400_000,
    "mail " * 80_000,
    "echo " + "x@" * 100_000,
    "(" * 100_000 + "mail",
])
def test_detection_stays_fast_on_huge_commands(command):
    assert _timed(command) < 1.5


# --- addresses -------------------------------------------------------------------

def test_test_domains_and_ignored_domains_are_skipped():
    assert gate.is_skipped_address("x@example.com")
    assert gate.is_skipped_address("x@shop.test")
    assert gate.is_skipped_address("x@sub.mine.com", ["mine.com"])
    assert gate.is_skipped_address("x@mine.com", ["@mine.com"])
    assert gate.is_skipped_address("x@mine.com", ["*.mine.com"])
    assert not gate.is_skipped_address("x@notmine.com", ["mine.com"])
    assert not gate.is_skipped_address("x@acme.co.uk", [])


def test_emails_in_handles_display_names_and_punctuation():
    assert gate.emails_in('"Jane" <Jane@Acme.co.uk>, bob@acme.co.uk.') == ["jane@acme.co.uk", "bob@acme.co.uk"]
    assert gate.emails_in("no addresses here") == []
    assert gate.emails_in(None) == []


def test_skeleton_blanks_strings_comments_and_heredoc_bodies():
    command = "echo 'mail a@b.com' # mail c@d.com\ncat <<EOF\nmail e@f.com\nEOF\nmail g@h.com"
    skel = gate.skeleton(command)
    assert len(skel) == len(command)
    assert "a@b.com" not in skel and "c@d.com" not in skel and "e@f.com" not in skel
    assert skel.endswith("\nEOF\nmail g@h.com")


# --- second review: shapes that used to slip through --------------------------------

def test_line_breaks_inside_quoted_arguments_do_not_end_the_command():
    assert recipients_of('$GAPI gmail send --to lead@acme.co.uk --subject Intro --body "Hi,\n\nIntro." '
                         "--cc boss@acme.co.uk") == ["lead@acme.co.uk", "boss@acme.co.uk"]
    assert recipients_of("agentmail inboxes:messages send --inbox-id me@agentmail.to --to a@acme.io "
                         "--text 'Hello\n\nThere' --bcc b@acme.io") == ["a@acme.io", "b@acme.io"]
    assert recipients_of('swaks --body "line one\nline two" --to c@acme.co.uk') == ["c@acme.co.uk"]


def test_flags_inside_a_quoted_body_are_not_flags():
    assert recipients_of('$GAPI gmail send --to lead@acme.co.uk --body "reply with --cc fake@other.com"') == ["lead@acme.co.uk"]


def test_curl_bodies_and_urls_set_earlier_in_the_command():
    from_file = ("cat > /tmp/e.json << 'EOF'\n{\"from\": \"me@mine.com\", \"to\": [\"lead@acme.co.uk\"]}\nEOF\n"
                 "curl https://api.resend.com/emails -H 'Content-Type: application/json' -d @/tmp/e.json")
    assert recipients_of(from_file) == ["lead@acme.co.uk"]
    from_variable = ("PAYLOAD=$(cat <<'EOF'\n{\"from\": \"me@mine.com\", \"to\": \"lead@acme.co.uk\"}\nEOF\n)\n"
                     "curl https://api.resend.com/emails -d \"$PAYLOAD\"")
    assert recipients_of(from_variable) == ["lead@acme.co.uk"]
    url_variable = ("URL=https://api.resend.com/emails; curl \"$URL\" "
                    "-d '{\"from\": \"me@mine.com\", \"to\": \"lead@acme.co.uk\"}'")
    assert recipients_of(url_variable) == ["lead@acme.co.uk"]


def test_a_curl_without_a_body_sends_no_one():
    assert recipients_of("curl -s https://api.resend.com/emails && echo '{\"to\":\"note@acme.co.uk\"}' >> log") == []


@pytest.mark.parametrize("command", [
    "timeout 60 curl https://api.resend.com/emails -d '{\"to\": \"lead@acme.co.uk\"}'",
    "if true; then mail -s hi lead@acme.co.uk < b.txt; fi",
    "for f in 1; do mail -s hi lead@acme.co.uk < b.txt; done",
    "{ mail -s hi lead@acme.co.uk < b.txt; }",
    "~/.local/bin/himalaya message write -H 'To: lead@acme.co.uk' body",
    "$HOME/.local/bin/himalaya message write -H 'To: lead@acme.co.uk' body",
    "printf $'To: lead@acme.co.uk\\nSubject: Don\\'t miss\\n\\nHi' | himalaya template send",
    "tr a-z A-Z <<< hello\nswaks --to lead@acme.co.uk --server smtp.x.com",
    "echo $((1<<n))\nswaks --to lead@acme.co.uk --server smtp.x.com",
    "out=\"$(printf 'To: lead@acme.co.uk\\n\\nHi' | himalaya template send)\"",
    "out=\"`swaks --to lead@acme.co.uk`\"",
    "mail<<EOF lead@acme.co.uk\nhi\nEOF",
    "printf 'To: lead@acme.co.uk\\n\\nHi' | msmtp --read-recipients",
])
def test_more_command_shapes(command):
    assert recipients_of(command) == ["lead@acme.co.uk"], command


def test_many_curls_stay_fast():
    command = "\n".join("curl -s https://api.resend.com/emails -d @f%d.json" % i for i in range(4000))
    assert _timed(command) < 1.5


def test_a_reply_in_a_batch_of_tool_messages_does_not_exempt_the_others():
    d = gate.detect("mcp__any__send_email", {"messages": [{"to": "old@acme.co.uk", "thread_id": "t1"},
                                                         {"to": "new@acme.co.uk"}]})
    assert d.recipients == ["new@acme.co.uk"] and not d.reply


# --- third review -------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "RESP=$(curl -s -X POST https://api.resend.com/emails -H \"Authorization: Bearer $K\" -d '{\"to\":\"lead@acme.co.uk\"}')",
    "RESP=`curl -s -X POST https://api.resend.com/emails -d '{\"to\":\"lead@acme.co.uk\"}'`",
    "curl https://api.resend.com/emails -d \"{\\\"to\\\": \\\"lead@acme.co.uk\\\", \\\"subject\\\": \\\"Hi $NAME\\\"}\"",
    "if curl -sf https://api.resend.com/emails -d '{\"to\":\"lead@acme.co.uk\"}'; then echo sent; fi",
    "if ! swaks --to lead@acme.co.uk --server smtp.x.com; then echo failed; fi",
    "until swaks --to lead@acme.co.uk --server smtp.x.com; do sleep 5; done",
    "cat > notes.txt << 'END-MSG'\ndon't forget\nEND-MSG\nswaks --to lead@acme.co.uk",
    "cat > notes.txt <<\"END MSG\"\ndon't forget\nEND MSG\nswaks --to lead@acme.co.uk",
    "(cd x)# don't\nswaks --to lead@acme.co.uk",
    "echo \"$(echo \"it's\")\"\nswaks --to lead@acme.co.uk",
])
def test_third_review_shapes(command):
    assert recipients_of(command) == ["lead@acme.co.uk"], command


def test_escaped_substitutions_in_double_quotes_are_text():
    assert recipients_of('echo "Run \\$(mail lead@acme.co.uk) later"') is None


def test_an_email_api_url_in_a_comment_does_not_make_other_curls_sends():
    command = ("# like https://api.resend.com/emails\n"
               "curl https://api.hubapi.com/contacts -d \"{\\\"email\\\": \\\"$E\\\", \\\"to\\\": \\\"x@acme.co.uk\\\"}\"")
    assert detect_cmd(command) is None


@pytest.mark.parametrize("command", [
    "{a}/x" * 80_000,
    "$(swaks --to a@acme.co.uk)" * 4000,
    "curl https://api.resend.com/emails -d '{\"to\": \"a@acme.co.uk\", \"attachments\": [{\"content\": \""
    + "QUJD" * 75_000 + "\"}]}'",
    "mail " + '"$(' * 120_000,
])
def test_more_huge_commands_stay_fast(command):
    assert _timed(command) < 1.5


# --- fourth review ---------------------------------------------------------------

@pytest.mark.parametrize("command", [
    'mail -s "Report $(date +%F)" lead@acme.co.uk < body.txt',
    "mail -s $(date +%F) lead@acme.co.uk < body.txt",
    "mail -s \"Report `date +%F`\" lead@acme.co.uk < body.txt",
    '$GAPI gmail send --subject Hi --body "$(cat /tmp/body.txt)" --to lead@acme.co.uk',
    'agentmail inboxes:messages send --inbox-id me@agentmail.to --text "$(cat body.txt)" --to lead@acme.co.uk',
    'swaks --body "$(cat body.txt)" --to lead@acme.co.uk',
    'hermes send -s "Update $(date)" --to email:lead@acme.co.uk "hi"',
    "BODY=\"$(cat <<'EOF'\nI'm reaching out about (your) site\nEOF\n)\"\n$GAPI gmail send --to lead@acme.co.uk --body \"$BODY\"",
    "agentmail inboxes:messages send --inbox-id me@agentmail.to --html \"" + "<p>hello</p>" * 2200 + "\" --to lead@acme.co.uk",
])
def test_substitutions_and_long_bodies_in_arguments(command):
    assert recipients_of(command) == ["lead@acme.co.uk"], command[:120]


def test_flags_inside_a_substitution_are_not_the_outer_commands():
    assert recipients_of('mail -s "$(grep -c --to other@acme.co.uk log)" lead@acme.co.uk') == ["lead@acme.co.uk"]


def test_nested_unclosed_substitutions_stay_fast():
    assert _timed("mail " + '"$(' * 120_000) < 1.5
    assert _timed("mail " + "$(" * 200_000) < 1.5
    assert _timed("mail " + "`" * 400_000) < 1.5
