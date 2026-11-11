"""The prompt must keep Hindi in a modern, casual, friend-like register.

Reads the prompt source so the suite stays free of LiveKit/logging side
effects, like the other prompt-contract tests.
"""

from __future__ import annotations

import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
PROMPT = SOURCE.split('ALEX_INSTRUCTIONS = f"""', 1)[1].split('"""', 1)[0]


class HindiStyleTests(unittest.TestCase):
    def test_hindi_section_exists(self) -> None:
        self.assertIn("HINDI / HINGLISH (spoken, not literary)", PROMPT)

    def test_register_is_casual_and_friend_like(self) -> None:
        self.assertIn("the way a close friend talks", PROMPT)
        self.assertIn("Formal, Sanskritised Hindi is wrong for voice", PROMPT)

    def test_sir_survives_the_casual_tone(self) -> None:
        self.assertIn('Keep "Sir" as the respectful address', PROMPT)
        self.assertIn('"Sir, ho gaya"', PROMPT)

    def test_hinglish_and_gen_z_phrasing_are_allowed(self) -> None:
        self.assertIn("Hinglish is the default", PROMPT)
        self.assertIn("Gen Z phrasing", PROMPT)
        self.assertIn('"kya scene hai"', PROMPT)

    def test_literary_words_are_named_as_forbidden(self) -> None:
        for word in ("कृपया", "अवश्य", "तत्काल", "अतः", "तथापि"):
            with self.subTest(word=word):
                self.assertIn(word, PROMPT)

    def test_casual_does_not_cancel_the_jarvais_manner(self) -> None:
        self.assertIn("Casual never means sloppy or disrespectful", PROMPT)
        self.assertIn("J.A.R.V.I.S. manner still applies", PROMPT)

    def test_slang_is_suppressed_in_serious_moments(self) -> None:
        self.assertIn("Never force slang into serious news, an emergency", PROMPT)

    def test_personality_words_stay_english(self) -> None:
        self.assertIn(
            "Hindi carries the grammar, English carries the modern vocabulary", PROMPT
        )
        self.assertIn('"calm", not "शांत"', PROMPT)
        self.assertIn('"intelligent", not "बुद्धिमान"', PROMPT)
        self.assertIn('"important", not "महत्वपूर्ण"', PROMPT)

    def test_core_personality_forbids_literary_translations(self) -> None:
        self.assertIn("never their literary Hindi equivalents", PROMPT)

    def test_spoken_example_uses_english_traits(self) -> None:
        self.assertIn("main calm hoon, plan clear hai", PROMPT)


if __name__ == "__main__":
    unittest.main()
