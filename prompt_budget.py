"""System-prompt budgeting.

The policy is fixed and must never be truncated: the EMAIL, GITHUB and
emergency sections live at the end of ``ALEX_INSTRUCTIONS``.  Persistent
memory is optional context, so when the prompt is too large the memory block
is trimmed instead.  Keeping this pure (no logging setup, no dotenv) makes it
safe to import from tests.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("alex")


def compose_instructions(
    policy: str,
    memory_block: str,
    *,
    max_instructions: int,
    max_memory_chars: int,
) -> str:
    """Return ``memory + policy`` within ``max_instructions``.

    Raises ``RuntimeError`` if the policy alone does not fit, rather than
    silently deleting rules.
    """
    instructions = policy

    if memory_block:
        if len(memory_block) > max_memory_chars:
            memory_block = memory_block[:max_memory_chars].rsplit("\n", 1)[0]
            memory_block += "\n[...memory trimmed to fit the prompt budget]"
            logger.warning("Memory block trimmed to %d chars", len(memory_block))
        instructions = f"{memory_block}\n\n{policy}"

    if len(instructions) > max_instructions:
        # Last resort: trim memory, never the policy.
        overflow = len(instructions) - max_instructions
        keep = max(0, len(memory_block) - overflow - 48)
        memory_block = (
            memory_block[:keep].rsplit("\n", 1)[0] + "\n[...memory trimmed]"
            if memory_block
            else ""
        )
        instructions = f"{memory_block}\n\n{policy}" if memory_block else policy
        logger.warning(
            "Instructions trimmed by memory only (%d chars)", len(instructions)
        )
        if len(instructions) > max_instructions:
            raise RuntimeError(
                "The system policy alone exceeds the prompt budget; "
                "raise the cap instead of truncating policy."
            )

    return instructions
