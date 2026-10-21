---
name: cold-email-check
description: "Check whether a cold or marketing email to a specific address is allowed before sending it, with Precleared (UK PECR and GDPR, US CAN-SPAM, Canada CASL, Germany, France, Australia, Ireland, the Netherlands). Use before first-touch outbound or prospecting email to people who aren't already customers, or to vet a lead list. Not for transactional email, replies or opted-in newsletters."
version: 0.1.0
author: Precleared
license: MIT
metadata:
  hermes:
    tags: [Email, Compliance, Outbound, Sales, GDPR, PECR, CAN-SPAM, CASL]
    homepage: https://precleared.dev
---

# Cold email check

Before the first cold email to a new address, ask Precleared whether that email is allowed. Each check returns a verdict, the rule that decided it with its source, what the footer must include and a signed receipt. Nothing is sent to the recipient.

This plugin also checks recipients on its own: when you send an email with a send tool, one of the email skills (himalaya, agentmail, Google Workspace) or an email API, an email to anyone Precleared hasn't cleared is held. It goes to the user for approval, or is refused if the plugin is set to block. Checking first with `precleared_check` is still better: you get the footer requirements and next step before you write the email, and the pre-send check reuses your answer instead of checking again.

## When to check

Check each new recipient once, before the first cold email, when:

- the email markets or sells something (a product, a service, a partnership) to someone who hasn't asked to hear from the sender and isn't already a customer. Having someone's address from a business card, a form or a directory is not the same as them asking to hear from the sender, and
- there is a specific address to check. A template with no recipients needs no check.

Precleared covers senders based in the UK or the US. It has full rules for recipients in the UK, the US, Canada, Germany, France, Australia, Ireland and the Netherlands; for many other European countries it answers `consent_needed`, and elsewhere `unknown`.

Skip transactional email (receipts, invoices, password resets, delivery updates), replies in a thread the recipient started, and newsletters to people who opted in. Check again if the pitch, or where the address came from, changes.

## Get the facts right

The verdict depends on the facts. Take them from the user or the lead data, and never fill a gap with a guess that makes the answer more convenient.

| Argument | What to pass |
| --- | --- |
| `email` | One address per call. |
| `address_source` | Where the address came from: `published` (their website, a profile or a directory), `bought` (a purchased list), `scraped` (collected by a tool), `given` (they gave it to the sender) or `unknown`. Ask the user if it isn't clear. Never assume `published`. Leave it out to use the plugin's default. |
| `pitch` | What the email offers, in a few words, for example "bookkeeping software for dental practices". Leave it out to use the plugin's default. |
| `recipient_country` | Two-letter code, if known. Otherwise Precleared works it out from the domain and company records. |
| `company_number` | UK company number, if known. |
| `pitch_relevant` | `true` if the offer relates to the recipient's job or business, `false` if it doesn't. Leave it out if unsure. |
| `existing_customer` | `true` if they have bought from the sender before. |

The sending business's country (GB or US) comes from the plugin settings. If `precleared_check` says it isn't set, ask the user where their business is based and give them the command from the error to set it.

## Act on the verdict

| Verdict | What to do |
| --- | --- |
| `ok_to_email` | Allowed. Send it with a footer that meets `footer_text`: usually who is sending, a postal address and a simple, free way to opt out. `footer_text` describes the requirement; it isn't wording to paste. |
| `consent_needed` | Don't send the cold email. Follow `next_step`, for example writing to the company's general address (check that address first) or getting consent through a channel the person opened, and tell the user. |
| `blocked` | Don't send. Follow `next_step` and tell the user. |
| `unknown` | Treat it as `consent_needed`. If a missing fact would settle it, such as the recipient's country, a UK company number or where the address came from, get that fact from the user or a record, not a guess, and check once more. |
| an error | Don't send to that address yet. Tell the user what happened and carry on with other work. |

Never change the facts to get a different verdict, and never send a cold email to an address whose check failed.

If the pre-send check holds an email you were about to send, don't try another tool or another way of sending it. Tell the user what Precleared said and let them decide.

## Lists

Check each distinct address once with `precleared_check`, one call per address, before loading the list into a campaign or sending. Keep a note of each verdict and receipt id beside the lead, and send only to the addresses that came back `ok_to_email`.

## Keep the receipt

Save the receipt id with the lead, in a CRM note or a column of the sheet. The receipt records what was asked, what was answered and when. Anyone with the id can open it at `https://precleared.dev/r/<receipt_id>`, and `precleared_verify_receipt` returns it with its signature.

## Limits and keys

- Without a key, Precleared gives a small free daily allowance, shared by everyone on the same network.
- A free key from https://precleared.dev/get-key gives the user their own allowance. They add it as `PRECLEARED_API_KEY` in the plugin's settings. Never ask for the key in the chat and never write it into files or messages.
- Paid packs add checks that don't expire: https://precleared.dev/#pricing

If a check says the allowance is used up or the key has no checks left, stop checking, tell the user how many addresses are left unchecked and pass on the link from the error. Don't try to get round the limit.

## Good to know

- Precleared reports what the rules say for the facts given, with sources. It is decision support, not legal advice.
- It doesn't write or send the email.
- The rules and their sources: https://precleared.dev/rules
