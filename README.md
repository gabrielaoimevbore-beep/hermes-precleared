# Precleared for Hermes Agent

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that checks cold email against the law before it goes out.

Whether a business can send someone a cold email depends on facts you can't see in the address. A UK sole trader needs consent first where a limited company doesn't. Germany wants prior consent even for business email. Canada's CASL depends on whether the address was published. [Precleared](https://precleared.dev) looks those facts up for each recipient and applies UK PECR and UK GDPR, US CAN-SPAM, Canada's CASL, and the German, French, Australian, Irish and Dutch rules. It answers `ok_to_email`, `consent_needed`, `blocked` or `unknown`, with the rule that decided it, its source and a signed receipt.

This plugin puts that check in front of your agent's email.

## What it adds

- **A pre-send check.** When the agent sends an email (see [which sends it checks](#which-sends-it-checks)), the plugin checks each new recipient first. If every recipient comes back `ok_to_email`, the email goes out untouched. If not, the call goes to Hermes's own approval prompt with the verdict, the rule and the receipt, so you decide. In `block` mode it is refused instead.
- **Two tools.** `precleared_check` checks one address and returns the verdict, the footer requirements and the next step, so the agent can check before it writes. `precleared_verify_receipt` fetches a signed receipt to show a client or an auditor.
- **A skill**, `precleared:cold-email-check`, that tells the agent when to check, which facts to pass and what to do with each verdict.

The plugin never sends email, never approves anything itself, and never changes what the agent sends.

## Which sends it checks

- **Email tools from plugins and MCP servers** whose name says they send email, for example `inkbox_send_email`, `openmail_send`, `bento_email_send` or `mcp__gmail__send_message`. Recipients are read from the `to`, `cc`, `bcc` and similar arguments; the sender's own fields are skipped.
- **Terminal commands from the email skills that come with Hermes:** himalaya (`himalaya template send`), agentmail (`agentmail inboxes:messages send`) and the Google Workspace script (`gmail send`). Also `swaks`, `sendmail`, `msmtp`, `mail`, `mutt` and `hermes send --to email:...`.
- **`curl` calls to the send endpoints of common email APIs:** Resend, SendGrid, Postmark, Mailgun, Brevo, Mailjet, AgentMail, Microsoft Graph and Amazon SES.
- **Any other tool you name** in the `extra_tools` setting.

Replies in an existing thread aren't checked: a message with an `In-Reply-To` header, a send with a thread id, or himalaya's reply command. Tools whose names mark drafts, forwards, test sends or transactional mail (receipts, invoices, password resets) are skipped too. A transactional email sent another way, with himalaya or curl say, is checked like any other, and if it's held you can approve it. Addresses at your own domains (the `ignore_domains` setting) and test domains like `example.com` are never checked. In shell commands, text inside quotes, comments and files being written isn't mistaken for a send.

What it can't see:

- an email whose recipients aren't in the tool call itself, such as a message read from a file, one sent from inside a script, or a command wrapped in `bash -c` or `ssh`. The skill tells the agent to call `precleared_check` before those.
- campaign and sequence tools, such as adding leads to an Instantly or Apollo sequence. They aren't treated as sends unless you list them in `extra_tools`.

## Install

1. Install and enable the plugin:

   ```bash
   hermes plugins install gabrielaoimevbore-beep/hermes-precleared --enable
   ```

2. Tell it where your business is based, `GB` or `US` (not where your prospects are):

   ```bash
   hermes config set plugins.entries.precleared.settings.sender_country GB
   ```

3. Recommended: say where your addresses come from and what your emails offer, as both change the verdict:

   ```bash
   hermes config set plugins.entries.precleared.settings.address_source published
   hermes config set plugins.entries.precleared.settings.pitch "bookkeeping software for dental practices"
   ```

4. Optional: add a free API key from [precleared.dev/get-key](https://precleared.dev/get-key) for your own daily allowance. In the Hermes desktop app it goes in **Settings → Plugins → Precleared**. Otherwise add a line `PRECLEARED_API_KEY=your-key` to the `.env` file that `hermes config env-path` shows.

Start a new Hermes session and the check is on. In the desktop app, every setting is also on the plugin's page under Settings → Plugins.

## Settings

All under `plugins.entries.precleared.settings`:

| Setting | Default | What it does |
| --- | --- | --- |
| `sender_country` | (none, required) | Where the sending business is based: `GB` or `US`. Until it's set, every email send stops and asks you to set it. |
| `mode` | `ask` | `ask`: emails that aren't cleared go to Hermes's approval prompt. `block`: they're refused. `off`: no pre-send check (the tools still work). |
| `address_source` | `unknown` | Where your addresses come from: `published` (their website, a profile or a directory), `bought`, `scraped`, `given` or `unknown`. |
| `pitch` | (empty) | What your emails offer, in a few words. Used to judge relevance to the recipient's job. |
| `prospect_country` | (empty) | Two-letter code, if all your prospects are in one country. Used only when the address, company records and website don't show it. |
| `ignore_domains` | `[]` | Domains not to check, such as your own: `'["mycompany.com"]'`. Subdomains are included. |
| `extra_tools` | `[]` | Other tools that send email, by name or pattern: `'["mcp__instantly__create_lead"]'`. |

The API key is a secret stored by Hermes as `PRECLEARED_API_KEY`, never in `config.yaml`.

### How `ask` works with Hermes's approvals

`ask` uses Hermes's normal approval prompt, so Hermes's approval settings apply:

- **Approving.** Approve once, for the session, or always. "Always" covers those exact recipients with those verdicts, never the whole tool, so if a verdict changes you're asked again.
- **Unattended runs.** Cron jobs and messaging sessions with nobody to answer refuse the email, unless you've set `approvals.cron_mode` or `approvals.unattended_mode` to `approve`.
- **Approvals switched off.** If approvals are off, or you run Hermes in YOLO mode, Hermes approves these prompts automatically. Use `mode: block` if you want a hard stop.

## Cost

Checks use your Precleared allowance. Without a key you get 10 checks a day, shared by everyone on the same network. A free key gives you 25 a day of your own. Packs of checks that don't expire are on [precleared.dev](https://precleared.dev/#pricing). Each address is checked once and the answer is reused for 30 days, so a follow-up email to the same person doesn't use another check (an `unknown` answer is asked again next time). When the allowance runs out, emails to unchecked addresses are held (sent to the approval prompt, or refused in `block` mode) with a link to get a key.

A check usually takes a second or two, and the plugin always finishes within 26 seconds, inside Hermes's 30-second limit for hooks. If you've lowered `plugins.hook_callback_timeout`, keep it at 30 or more.

## Data and network

What a user should know before installing:

- **Network calls go to one host, `precleared.dev`, over HTTPS.** A check sends the recipient's email address, your sender country, the address source, your pitch and, if set, your prospects' country, plus any facts the agent passes to `precleared_check` (recipient country, company number, whether they're an existing customer, whether the pitch is relevant). `precleared_verify_receipt` sends a receipt id. Nothing else about you, your messages or the email's content is sent.
- **Your API key**, if you add one, is read through Hermes's secret store and sent only in the `Authorization` header to `precleared.dev`. It never appears in messages, logs or files the plugin writes.
- **A local cache** in Hermes's per-plugin data folder keeps each verdict for 30 days (at most 2,000 entries): a hash of the address, the verdict, a short reason and the receipt id. No addresses are stored in it, and approvals you save with "always" are kept by Hermes under a hash too.
- **No shell commands, no background processes, no telemetry.** The plugin reads the tool calls Hermes shows its hook, and nothing outside its own data folder.

Precleared's privacy policy: [precleared.dev/privacy](https://precleared.dev/privacy).

## Development

```bash
python -m pip install "pytest>=8,<10" "pyyaml>=6,<7"
python -m pytest -q
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

The tests use a fake network and a fake Hermes context, so they need neither. `tests/test_live.py` makes one real check when `PRECLEARED_LIVE=1` is set.

## Good to know

Precleared reports what the rules say for the facts given, with sources. It is decision support, not legal advice. The rules and their sources are at [precleared.dev/rules](https://precleared.dev/rules).

## Licence

MIT. See [LICENSE](LICENSE).
