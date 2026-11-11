"""The system prompt must keep Alex's J.A.R.V.I.S. personality contract.

These assertions read the prompt source rather than importing ``main`` so the
suite stays free of LiveKit/logging side effects, matching the existing
prompt-policy test.
"""

from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
PROMPT = SOURCE.split('ALEX_INSTRUCTIONS = f"""', 1)[1].split('"""', 1)[0]


class PersonalityTests(unittest.TestCase):
    def test_core_personality_is_declared(self) -> None:
        self.assertIn("CORE PERSONALITY (J.A.R.V.I.S.)", PROMPT)

    def test_four_jarvais_traits_are_present(self) -> None:
        for trait in (
            "Calm and collected",
            "Polite and formal",
            "Dry wit and subtle sarcasm",
            "Protective ethics",
        ):
            with self.subTest(trait=trait):
                self.assertIn(trait, PROMPT)

    def test_formal_address_defaults_to_sir(self) -> None:
        self.assertIn('Address the user as "Sir"', PROMPT)
        self.assertIn('("Boss", a name)', PROMPT)

    def test_humor_is_suppressed_in_a_crisis(self) -> None:
        self.assertIn("set humor aside completely", PROMPT)

    def test_safety_outranks_agreeableness(self) -> None:
        self.assertIn("ahead of convenience and ahead of being agreeable", PROMPT)
        self.assertIn("If someone's safety is at risk, say that first", PROMPT)

    def test_it_does_not_claim_to_be_the_fictional_character(self) -> None:
        self.assertIn("Never claim to literally be Marvel's J.A.R.V.I.S.", PROMPT)


if __name__ == "__main__":
    unittest.main()
