"""Precleared for Hermes Agent: check cold email against the law before it goes out.

Registers two tools (precleared_check, precleared_verify_receipt), a
pre_tool_call hook that checks the recipients of every email send the agent
makes, and the cold-email-check skill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

TOOLSET = "precleared"
SKILL_NAME = "cold-email-check"


def register(ctx: Any) -> None:
    # Imported here so this file also imports cleanly on its own (test runners do that).
    from . import hooks, schemas, tools

    ctx.register_tool(
        name="precleared_check",
        toolset=TOOLSET,
        schema=schemas.PRECLEARED_CHECK,
        handler=tools.make_check_handler(ctx),
        description="Check whether a cold email to one address is allowed before sending it",
    )
    ctx.register_tool(
        name="precleared_verify_receipt",
        toolset=TOOLSET,
        schema=schemas.PRECLEARED_VERIFY_RECEIPT,
        handler=tools.make_receipt_handler(ctx),
        description="Fetch a signed Precleared receipt by id",
    )
    ctx.register_hook("pre_tool_call", hooks.make_pre_tool_call(ctx))
    skill = Path(__file__).parent / "skills" / SKILL_NAME / "SKILL.md"
    if skill.is_file():
        ctx.register_skill(
            SKILL_NAME,
            skill,
            description="Check each cold email recipient with Precleared before the first send, and act on the verdict",
        )
