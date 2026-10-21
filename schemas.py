"""Tool schemas: what the model reads to decide when to call each tool."""

from . import client

PRECLEARED_CHECK = {
    "name": "precleared_check",
    "description": (
        "Check whether a cold or marketing email to one address is allowed before you send it. "
        "Covers recipients in the UK (PECR, UK GDPR), the US (CAN-SPAM), Canada (CASL), Germany, "
        "France, Australia, Ireland and the Netherlands, for businesses based in the UK or the US. "
        "Call it once per new recipient who is not already a customer, before the first email, "
        "including every address in a lead list. Act on the verdict: ok_to_email means send, with "
        "a footer that meets footer_text; consent_needed and blocked mean don't send and follow "
        "next_step; unknown means treat it as consent_needed. Returns the rule that decided it with "
        "its source and a signed receipt id. Not needed for transactional email (receipts, "
        "invoices, password resets) or replies to someone who wrote first. The sending business's "
        "country and the default facts come from the plugin settings. Nothing is sent to the "
        "recipient. For the full workflow, load the skill precleared:cold-email-check."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "email": {
                "type": "string",
                "description": "The recipient's email address, one per call.",
            },
            "address_source": {
                "type": "string",
                "enum": list(client.ADDRESS_SOURCES),
                "description": (
                    "Where the address came from: published (on their website, a profile or a "
                    "directory), bought (a purchased list), scraped (collected by a tool), given "
                    "(they gave it to the sender) or unknown. It changes the verdict, so never "
                    "assume published. Leave it out to use the plugin's default."
                ),
            },
            "pitch": {
                "type": "string",
                "description": (
                    "What the email offers, in a few words, for example 'bookkeeping software for "
                    "dental practices'. Leave it out to use the plugin's default."
                ),
            },
            "existing_customer": {
                "type": "boolean",
                "description": "True if the recipient has bought from the sender before.",
            },
            "recipient_country": {
                "type": "string",
                "description": (
                    "The recipient's two-letter country code if you know it, for example GB, US or "
                    "DE. Otherwise Precleared works it out from the address and company records."
                ),
            },
            "company_number": {
                "type": "string",
                "description": "The recipient's UK company number, if known.",
            },
            "pitch_relevant": {
                "type": "boolean",
                "description": (
                    "True if the offer relates to the recipient's job or business, false if it "
                    "doesn't. Leave it out if unsure."
                ),
            },
        },
        "required": ["email"],
    },
}

PRECLEARED_VERIFY_RECEIPT = {
    "name": "precleared_verify_receipt",
    "description": (
        "Fetch a Precleared receipt by its id (cc_rcpt_ followed by 16 capital letters or digits) "
        "to show what was checked, when, the verdict, and the hashes and Ed25519 signature that "
        "prove it. Use it when the user, a client or an auditor wants to see an earlier check."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "receipt_id": {
                "type": "string",
                "description": "The receipt id, for example cc_rcpt_0123456789ABCDEF.",
            },
        },
        "required": ["receipt_id"],
    },
}
