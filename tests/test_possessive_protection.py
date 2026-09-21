from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import TranslationError
from mq_localizer.protection import (
    TokenProtector,
    has_protected_possessive_suffix,
    protected_layout_signature,
    protected_syntax_signature,
    special_tokens,
)


class PossessiveProtectionTests(unittest.TestCase):
    def test_styled_bare_possessive_can_be_translated(self) -> None:
        for apostrophe in ("'", "\u2019"):
            with self.subTest(apostrophe=apostrophe):
                protected = TokenProtector().protect(
                    f"&bStorage Bus{apostrophe}&r", {"Storage Bus": "ストレージバス"},
                )
                candidate = protected.protected.replace(apostrophe, "の")
                self.assertEqual(protected.restore(candidate), "&bストレージバスの&r")
                with self.assertRaises(TranslationError):
                    protected.restore(protected.protected.replace(apostrophe, ""))

    def test_bare_possessive_has_matching_canonical_reuse_signature(self) -> None:
        for opening, closing in (("&b", "&r"), ("&b", "&f"), ("&b", "")):
            with self.subTest(opening=opening, closing=closing):
                source = f"{opening}Storage Bus'{closing}"
                target = f"{opening}ストレージバスの{closing}"
                terminology = {"Storage Bus": "ストレージバス", "ストレージバス": "ストレージバス"}
                self.assertEqual(
                    protected_layout_signature(source, terminology),
                    protected_layout_signature(target, terminology),
                )
                self.assertNotEqual(
                    protected_layout_signature(source, terminology),
                    protected_layout_signature(target.replace("の", ""), terminology),
                )

    def test_name_and_possessive_stay_in_original_formatting_scope(self) -> None:
        protected = TokenProtector().protect(
            "Use &bStorage Bus'&r with &aInterface&r.",
            {"Storage Bus": "Storage Bus", "Interface": "Interface"},
        )
        candidate = protected.protected.replace("'", "の")
        self.assertIn("&bStorage Busの&r", protected.restore(candidate))
        for bad in (
            protected.protected.replace("'", ""),
            protected.protected.replace("'", "").replace("with", "with の"),
            candidate.replace(protected.term_placeholders[0], ""),
            candidate.replace(protected.term_placeholders[0], protected.term_placeholders[1]),
        ):
            with self.subTest(candidate=bad), self.assertRaises(TranslationError):
                protected.restore(bad)

    def test_layout_and_unclosed_tail_allow_grammatical_suffix(self) -> None:
        for source in (
            "Storage Bus'\nNext",
            "&bStorage Bus'",
            "Use &aInterface&r and &bStorage Bus'",
            "&aStorage Bus' \\&\\ Interface&r",
        ):
            with self.subTest(source=source):
                protected = TokenProtector().protect(
                    source, {"Storage Bus": "Storage Bus", "Interface": "Interface"},
                )
                self.assertEqual(
                    protected.restore(protected.protected.replace("'", "の")),
                    source.replace("'", "の"),
                )
                with self.assertRaises(TranslationError):
                    protected.restore(protected.protected.replace("'", ""))

    def test_classifier_requires_exact_term_and_single_attached_apostrophe(self) -> None:
        term = "__MQP_0001__"
        for text in (term + "'", term + "\u2019", "__MQP_0000__" + term + "'__MQP_0002__"):
            with self.subTest(text=text):
                self.assertTrue(has_protected_possessive_suffix(text, (term,)))
        for text in (
            "'", "\u2019", "'" + term + "'", "\u2018" + term + "\u2019",
            "'" + term, term + " '", term + "'!", "__MQP_0002__'",
            "'" + term + " __MQP_0002__'",
        ):
            with self.subTest(text=text):
                self.assertFalse(has_protected_possessive_suffix(text, (term,)))

    def test_quoted_names_and_technical_labels_are_not_possessives(self) -> None:
        for source, terminology in (
            ("&b'Storage Bus'&r", {"Storage Bus": "Storage Bus"}),
            ("&b%s'&r", {}),
            ("&bminecraft:stone'&r", {}),
            ("&b__MQP_1234__'&r", {}),
            ("&b'&r", {}),
            ("&b+&r", {}),
        ):
            with self.subTest(source=source):
                protected = TokenProtector().protect(source, terminology)
                self.assertEqual(protected.restore(protected.protected), source)
                replacement = protected.protected.replace("'", "の").replace("+", "の")
                with self.assertRaises(TranslationError):
                    protected.restore(replacement)


class StyledCommandProtectionTests(unittest.TestCase):
    def test_actual_commands_include_all_arguments_in_one_token(self) -> None:
        commands = (
            "/ftbteams party create", "/ftbteams party leave",
            "/ftbteams party invite <username>", "/talisman gui", "/talisman toggle",
            "/spark tps", "/neoforge generate", "/neoforge generate start 0 0 0 250 true",
        )
        for command in commands:
            with self.subTest(command=command):
                source = f"Run &a{command}&f to continue."
                protected = TokenProtector().protect(source)
                self.assertIn(command, protected.special_values)
                self.assertEqual(protected.restore(protected.protected), source)
                translated = protected.protected.replace("Run ", "実行: ").replace(" to continue.", "で続行します。")
                self.assertEqual(protected.restore(translated), f"実行: &a{command}&fで続行します。")

    def test_existing_translation_cannot_modify_command_arguments(self) -> None:
        source = "&a/ftbteams party invite <username>&f"
        for target in (
            "&a/ftbteams パーティ 招待 <名前>&f",
            "&a/ftbteams party invite <name>&f",
            "&a/ftbteams party&r",
            "&a/ftbteams party invite <username>,&f",
        ):
            with self.subTest(target=target):
                self.assertNotEqual(protected_syntax_signature(source), protected_syntax_signature(target))
                self.assertNotEqual(protected_layout_signature(source), protected_layout_signature(target))

    def test_live_translation_cannot_edit_drop_or_extend_command(self) -> None:
        protected = TokenProtector().protect("&a/ftbteams party invite <username>&r")
        command_token = next(
            token for token, value in protected.replacements.items()
            if value == "/ftbteams party invite <username>"
        )
        for candidate in (
            protected.protected.replace(command_token, ""),
            protected.protected.replace(command_token, "/ftbteams party invite <name>"),
            protected.protected.replace(command_token, command_token + " changed"),
            protected.protected.replace(command_token, command_token + ","),
            protected.protected.replace(command_token, command_token + " "),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(TranslationError):
                protected.restore(candidate)

    def test_command_scanner_does_not_capture_surrounding_or_multiline_prose(self) -> None:
        for source in (
            "&aRun /ftbteams party invite <username>&r",
            "&a/ftbteams party invite <username> without a reset",
            "&a/ftbteams party\ninvite <username>&r",
            "&a/ftbteams party\\ninvite <username>&r",
            "&a/ftbteams party\tinvite <username>&r",
            "&a/ftbteams &cparty invite <username>&r",
        ):
            with self.subTest(source=source):
                self.assertNotIn("/ftbteams party invite <username>", special_tokens(source))
                protected = TokenProtector().protect(source)
                self.assertEqual(protected.restore(protected.protected), source)

    def test_consecutive_format_stack_and_section_codes_are_supported(self) -> None:
        source = "§a§l/ftbteams party invite <username>§r and &b/spark tps&r"
        self.assertIn("/ftbteams party invite <username>", special_tokens(source))
        self.assertIn("/spark tps", special_tokens(source))
        protected = TokenProtector().protect(source)
        self.assertEqual(protected.restore(protected.protected), source)

    def test_style_looking_text_inside_other_syntax_does_not_start_command(self) -> None:
        for source, unchanged_special in (
            ("https://example.invalid/?x=&a/foo bar&r", "https://example.invalid/?x=&a/foo"),
            ("{command:/say &a/foo bar&r}", "{command:/say &a/foo bar&r}"),
            (r"\&a/foo bar&r", r"\&"),
        ):
            with self.subTest(source=source):
                self.assertNotIn("/foo bar", special_tokens(source))
                self.assertIn(unchanged_special, special_tokens(source))
                protected = TokenProtector().protect(source)
                self.assertEqual(protected.restore(protected.protected), source)


if __name__ == "__main__":
    unittest.main()
