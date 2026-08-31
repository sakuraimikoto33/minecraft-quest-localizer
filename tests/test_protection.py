from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import TranslationError  # noqa: E402
from mq_localizer.protection import (  # noqa: E402
    TermReplacement,
    TokenProtector,
    looks_like_raw_json_text,
    protected_layout_signature,
    protected_syntax_signature,
    should_translate,
    special_tokens,
    terminology_literal_skeleton,
)


_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")


class TokenProtectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protector = TokenProtector()

    def test_round_trip_preserves_codes_newline_kinds_and_placeholders(self) -> None:
        source = (
            "§aGreen &lBold §x§1§2§3§4§5§6Hex &#A1B2C3Rgb\r\n"
            "actual LF\nliteral \\n and tab \\t; %1$s %02d %% "
            "{quest:ABCDEF0123456789} {@page} ${name} {{count}} "
            "https://example.invalid/a?q=1 minecraft:diamond /ABCDEF0123456789/2"
        )

        protected = self.protector.protect(source)

        self.assertNotEqual(protected.protected, source)
        self.assertGreaterEqual(len(protected.replacements), 16)
        for special in ("§a", "&l", "\r\n", "\n", "\\n", "%1$s", "${name}"):
            self.assertNotIn(special, protected.protected)
        self.assertEqual(protected.restore(protected.protected), source)

    def test_missing_extra_or_substituted_token_is_rejected(self) -> None:
        protected = self.protector.protect("§aTranslate this\nNext %s")
        tokens = _TOKEN.findall(protected.protected)
        self.assertGreaterEqual(len(tokens), 3)

        invalid_outputs = [
            protected.protected.replace(tokens[0], "", 1),
            protected.protected + tokens[0],
            protected.protected.replace(tokens[0], tokens[1], 1),
            protected.protected.replace(tokens[0], tokens[0].lower(), 1),
        ]
        for output in invalid_outputs:
            with self.subTest(output=output):
                with self.assertRaises(TranslationError):
                    protected.restore(output)

    def test_explicit_term_spans_fail_closed_when_they_overlap(self) -> None:
        cases = (
            (
                "Rainbow Sword",
                (
                    TermReplacement(0, 7, "Rainbow"),
                    TermReplacement(0, 13, "虹の剣"),
                ),
            ),
            (
                "Rainbow Sword",
                (
                    TermReplacement(0, 13, "虹の剣"),
                    TermReplacement(0, 13, "虹の剣"),
                ),
            ),
            (
                "&aSword&r",
                (TermReplacement(0, 7, "剣"),),
            ),
        )
        for source, spans in cases:
            with self.subTest(source=source, spans=spans):
                with self.assertRaisesRegex(TranslationError, "重複"):
                    self.protector.protect(source, term_spans=spans)

    def test_term_fully_inside_an_immutable_resource_id_is_redundant(self) -> None:
        source = "Any #elementalcraft:gems/fine_water"
        protected = self.protector.protect(
            source,
            term_spans=(TermReplacement(5, 19, "エレメンタルクラフト"),),
        )

        self.assertEqual(protected.term_placeholders, ())
        self.assertEqual(len(protected.special_placeholders), 1)
        self.assertEqual(protected.restore(protected.protected), source)

    def test_terms_inside_both_resource_namespace_and_path_are_redundant(self) -> None:
        source = "Any #waystones:waystones"
        protected = self.protector.protect(
            source,
            term_spans=(
                TermReplacement(5, 14, "Waystones"),
                TermReplacement(15, 24, "Waystones"),
            ),
        )

        self.assertEqual(protected.term_placeholders, ())
        self.assertEqual(protected.restore(protected.protected), source)

    def test_reordered_format_and_newline_tokens_are_rejected(self) -> None:
        protected = self.protector.protect("A§aB\nC§rD")
        format_token = next(
            token for token, value in protected.replacements.items() if value == "§a"
        )
        newline_token = next(
            token for token, value in protected.replacements.items() if value == "\n"
        )
        swapped = protected.protected.replace(format_token, "__SWAP__", 1)
        swapped = swapped.replace(newline_token, format_token, 1).replace(
            "__SWAP__", newline_token, 1
        )

        with self.assertRaises(TranslationError):
            protected.restore(swapped)

    def test_layout_token_cannot_move_to_another_text_segment(self) -> None:
        for source, fixed_value in (
            ("Left\nRight", "\n"),
            (r"Left\nRight", r"\n"),
            (r"Literal \&a text", r"\&"),
        ):
            with self.subTest(source=source):
                protected = self.protector.protect(source)
                fixed = next(
                    token
                    for token, value in protected.replacements.items()
                    if value == fixed_value
                )
                without = protected.protected.replace(fixed, "", 1)
                for moved in (fixed + without, without + fixed):
                    with self.subTest(moved=moved), self.assertRaises(TranslationError):
                        protected.restore(moved)

        paragraph = self.protector.protect("A\nB")
        newline = next(
            token for token, value in paragraph.replacements.items() if value == "\n"
        )
        with self.assertRaises(TranslationError):
            paragraph.restore("AB" + newline)

    def test_closed_formatting_groups_may_reorder_for_japanese_grammar(self) -> None:
        source = "Put §aA§r before §bB§r"
        protected = self.protector.protect(source)
        starts = {
            value: placeholder
            for placeholder, value in protected.replacements.items()
            if value in {"§a", "§b"}
        }
        resets = [
            placeholder
            for placeholder, value in protected.replacements.items()
            if value == "§r"
        ]
        translated = (
            starts["§b"]
            + "B訳"
            + resets[1]
            + "の前に"
            + starts["§a"]
            + "A訳"
            + resets[0]
            + "を置く"
        )

        self.assertEqual(
            protected.restore(translated),
            "§bB訳§rの前に§aA訳§rを置く",
        )
        self.assertEqual(
            protected_layout_signature(source),
            protected_layout_signature("§bB訳§rの前に§aA訳§rを置く"),
        )

    def test_reported_elementalcraft_description_allows_styled_group_reorder(self) -> None:
        source = (
            "To do so you need an &5Infuser&r on top of a &3Container&r. "
            "Don't forget to connect it to the &3Extractor's Container&r "
            "with a &3pipe&r."
        )
        protected = self.protector.protect(source)
        tokens = list(protected.replacements)
        translated = (
            "これを行うには、"
            + tokens[2]
            + "容器"
            + tokens[3]
            + "の上に"
            + tokens[0]
            + "注入器"
            + tokens[1]
            + "を置く必要があります。"
            + tokens[4]
            + "抽出器の容器"
            + tokens[5]
            + "に"
            + tokens[6]
            + "パイプ"
            + tokens[7]
            + "で接続するのを忘れないでください。"
        )

        self.assertEqual(
            protected.restore(translated),
            (
                "これを行うには、&3容器&rの上に&5注入器&rを置く必要があります。"
                "&3抽出器の容器&rに&3パイプ&rで接続するのを忘れないでください。"
            ),
        )

    def test_reported_elementalcraft_reorder_keeps_protected_term_in_its_group(self) -> None:
        source = (
            "To do so you need an &5Infuser&r on top of a &3Container&r. "
            "Don't forget to connect it to the &3Extractor's Container&r "
            "with a &3pipe&r."
        )
        protected = self.protector.protect(source, {"Extractor": "Extractor"})
        tokens = list(protected.replacements)
        translated = (
            "そのためには、"
            + tokens[2]
            + "Container"
            + tokens[3]
            + "の上に"
            + tokens[0]
            + "Infuser"
            + tokens[1]
            + "を設置する必要があります。"
            + tokens[7]
            + "pipe"
            + tokens[8]
            + "を使って、それを"
            + tokens[4]
            + tokens[5]
            + "のContainer"
            + tokens[6]
            + "に接続するのを忘れないでください。"
        )

        self.assertEqual(
            protected.restore(translated),
            (
                "そのためには、&3Container&rの上に&5Infuser&rを設置する必要があります。"
                "&3pipe&rを使って、それを&3ExtractorのContainer&rに接続するのを"
                "忘れないでください。"
            ),
        )

        term_outside_group = translated.replace(
            tokens[4] + tokens[5] + "のContainer" + tokens[6],
            tokens[5] + tokens[4] + "のContainer" + tokens[6],
        )
        with self.assertRaisesRegex(TranslationError, "装飾コードで囲まれた"):
            protected.restore(term_outside_group)

    def test_formatting_group_open_reset_or_body_cannot_be_split(self) -> None:
        protected = self.protector.protect("§aGreen§r and §bBlue§r")
        tokens = list(protected.replacements)
        invalid = (
            tokens[0] + tokens[1] + "Green and Blue" + tokens[2] + tokens[3],
            tokens[0] + "Green " + tokens[2] + "Blue" + tokens[1] + tokens[3],
        )
        for translated in invalid:
            with self.subTest(translated=translated), self.assertRaises(TranslationError):
                protected.restore(translated)

    def test_orphan_reset_unclosed_and_complex_formatting_remain_fixed(self) -> None:
        cases = (
            "A §rreset",
            "A §aunclosed",
            "§aFirst §bSecond§r",
        )
        for source in cases:
            with self.subTest(source=source):
                protected = self.protector.protect(source)
                first = next(iter(protected.replacements))
                moved = first + protected.protected.replace(first, "", 1)
                if moved == protected.protected:
                    moved = protected.protected.replace(first, "", 1) + first

                with self.assertRaises(TranslationError):
                    protected.restore(moved)

    def test_stacked_mixed_and_identical_formatting_groups_may_reorder(self) -> None:
        cases = (
            (
                "&l&5Bold&r then §aGreen§r",
                lambda tokens: (
                    tokens[3]
                    + "緑"
                    + tokens[4]
                    + "の後に"
                    + tokens[0]
                    + tokens[1]
                    + "太字"
                    + tokens[2]
                ),
                "§a緑§rの後に&l&5太字&r",
            ),
            (
                "&3First&r and &3Second&r",
                lambda tokens: (
                    tokens[2]
                    + "二番"
                    + tokens[3]
                    + "の後に"
                    + tokens[0]
                    + "一番"
                    + tokens[1]
                ),
                "&3二番&rの後に&3一番&r",
            ),
        )
        for source, translate, expected in cases:
            with self.subTest(source=source):
                protected = self.protector.protect(source)
                translated = translate(list(protected.replacements))
                self.assertEqual(protected.restore(translated), expected)

    def test_formatting_groups_cannot_cross_physical_or_escaped_newlines(self) -> None:
        for separator in ("\n", r"\n"):
            with self.subTest(separator=repr(separator)):
                protected = self.protector.protect(
                    "&aGreen&r" + separator + "&bBlue&r"
                )
                tokens = list(protected.replacements)
                crossed = (
                    tokens[3]
                    + "青"
                    + tokens[4]
                    + tokens[2]
                    + tokens[0]
                    + "緑"
                    + tokens[1]
                )
                with self.assertRaises(TranslationError):
                    protected.restore(crossed)

    def test_printf_placeholder_must_move_with_its_formatting_group(self) -> None:
        protected = self.protector.protect("&aUse %s&r and &bOther&r")
        tokens = list(protected.replacements)
        translated = (
            tokens[3]
            + "その他"
            + tokens[4]
            + "の後に"
            + tokens[0]
            + tokens[1]
            + "を使う"
            + tokens[2]
        )
        self.assertEqual(
            protected.restore(translated),
            "&bその他&rの後に&a%sを使う&r",
        )

        placeholder_outside = translated.replace(
            tokens[0] + tokens[1] + "を使う" + tokens[2],
            tokens[1] + tokens[0] + "を使う" + tokens[2],
        )
        with self.assertRaises(TranslationError):
            protected.restore(placeholder_outside)

    def test_printf_and_template_tokens_may_reorder_inside_one_segment(self) -> None:
        source = "Give %1$s to {player}; date %1$tY, newline %n, count %02d and %%"
        protected = self.protector.protect(source)
        printf = next(
            token for token, value in protected.replacements.items() if value == "%1$s"
        )
        template = next(
            token for token, value in protected.replacements.items() if value == "{player}"
        )
        swapped = protected.protected.replace(printf, "__SWAP__", 1)
        swapped = swapped.replace(template, printf, 1).replace("__SWAP__", template, 1)

        restored = protected.restore(swapped)

        self.assertEqual(special_tokens(restored).count("%1$s"), 1)
        self.assertEqual(special_tokens(restored).count("{player}"), 1)
        for token in ("%1$tY", "%n", "%02d", "%%"):
            self.assertIn(token, special_tokens(restored))

    def test_url_and_id_tokens_allow_japanese_text_on_both_sides(self) -> None:
        tokens = (
            "https://example.invalid/path?q=1",
            "minecraft:stone",
            "ABCDEF0123456789/2",
            "/give",
        )
        for token in tokens:
            with self.subTest(token=token):
                protected = self.protector.protect(f"Use {token} now")
                placeholder = next(
                    placeholder
                    for placeholder, value in protected.replacements.items()
                    if value == token
                )

                restored = protected.restore(f"前{placeholder}を使う")

                self.assertEqual(restored, f"前{token}を使う")

    def test_url_and_id_tokens_reject_new_ascii_attachment(self) -> None:
        tokens = (
            "https://example.invalid/path?q=1",
            "minecraft:stone",
            "ABCDEF0123456789/2",
            "/give",
        )
        for token in tokens:
            protected = self.protector.protect(f"Use {token} now")
            placeholder = next(
                placeholder
                for placeholder, value in protected.replacements.items()
                if value == token
            )
            for translated in (f"Extra{placeholder}", f"{placeholder}Extra"):
                with (
                    self.subTest(token=token, translated=translated),
                    self.assertRaisesRegex(TranslationError, "原文にない装飾コード"),
                ):
                    protected.restore(translated)

    def test_partial_technical_suffixes_cannot_extend_a_protected_token(self) -> None:
        cases = (
            ("Use /give now", "/give", "/kill"),
            ("Use ABCDEF0123456789/2 now", "ABCDEF0123456789/2", "/kill"),
            ("Use minecraft:stone now", "minecraft:stone", ":x"),
        )
        for source, value, suffix in cases:
            protected = self.protector.protect(source)
            placeholder = next(
                placeholder
                for placeholder, replacement in protected.replacements.items()
                if replacement == value
            )
            translated = protected.protected.replace(
                placeholder,
                placeholder + suffix,
            )

            with (
                self.subTest(source=source, suffix=suffix),
                self.assertRaisesRegex(TranslationError, "原文にない装飾コード"),
            ):
                protected.restore(translated)

    def test_adjacent_source_tokens_are_all_protected_and_round_trip(self) -> None:
        cases = (
            ("§aminecraft:stone", ("§a", "minecraft:stone")),
            ("&a/give", ("&a", "/give")),
            (r"\nABCDEF0123456789/2", (r"\n", "ABCDEF0123456789/2")),
            ("%sminecraft:stone", ("%s", "minecraft:stone")),
        )
        for source, expected_tokens in cases:
            with self.subTest(source=source):
                protected = self.protector.protect(source)

                self.assertEqual(special_tokens(source), expected_tokens)
                self.assertEqual(len(protected.special_placeholders), 2)
                self.assertEqual(protected.restore(protected.protected), source)

    def test_term_and_literal_adjacent_technical_source_round_trip(self) -> None:
        cases = (
            ("__MQP_1234__/give", None, "__MQP_1234__/give"),
            (
                "__MQP_1234__ABCDEF0123456789/2",
                None,
                "__MQP_1234__ABCDEF0123456789/2",
            ),
            ("Create/give", {"Create": "Create"}, "Create/give"),
            ("Iron/give", {"Iron": "鉄"}, "鉄/give"),
        )
        for source, terminology, expected in cases:
            with self.subTest(source=source):
                protected = self.protector.protect(source, terminology)

                self.assertEqual(protected.restore(protected.protected), expected)

    def test_source_slash_between_immutable_values_round_trips(self) -> None:
        cases = (
            ("Create/Create", {"Create": "Create"}, "Create/Create"),
            (
                "Create/__MQP_0000__",
                {"Create": "Create"},
                "Create/__MQP_0000__",
            ),
            (
                "Create/minecraft:stone",
                {"Create": "Create"},
                "Create/minecraft:stone",
            ),
            (
                "Iron/minecraft:stone",
                {"Iron": "鉄"},
                "鉄/minecraft:stone",
            ),
            (
                "__MQP_0000__/https://x.invalid",
                None,
                "__MQP_0000__/https://x.invalid",
            ),
            (
                "__MQP_0000__/ABCDEF0123456789/2",
                None,
                "__MQP_0000__/ABCDEF0123456789/2",
            ),
        )
        for source, terminology, expected in cases:
            with self.subTest(source=source):
                protected = self.protector.protect(source, terminology)

                self.assertEqual(protected.restore(protected.protected), expected)

    def test_relaxed_source_slash_cannot_be_promoted_to_a_command(self) -> None:
        protected = self.protector.protect(
            "Create/Create",
            {"Create": "Create"},
        )
        first, second = protected.term_placeholders

        for translated in (first + " /" + second, first + "・/" + second):
            with (
                self.subTest(translated=translated),
                self.assertRaisesRegex(TranslationError, "原文にない装飾コード"),
            ):
                protected.restore(translated)

    def test_glossary_source_ascii_attachment_across_formatting_round_trip(self) -> None:
        sources = (
            "A§aCreate",
            "Create§aA",
            "Create§aCreate",
            "%s§aCreate",
            "minecraft:stone§aCreate",
        )
        for source in sources:
            with self.subTest(source=source):
                protected = self.protector.protect(source, {"Create": "Create"})

                self.assertEqual(protected.restore(protected.protected), source)

    def test_overlapping_source_grammar_uses_one_maximal_trusted_span(self) -> None:
        source = "ABCDEF0123456789:x"
        protected = self.protector.protect(source)

        self.assertEqual(special_tokens(source), (source,))
        self.assertEqual(tuple(protected.replacements.values()), (source,))
        self.assertEqual(protected.restore(protected.protected), source)

    def test_cross_placeholder_syntax_construction_is_rejected(self) -> None:
        cases: list[tuple[object, str]] = []

        hex_id = self.protector.protect("Use ABCDEF0123456789 now")
        hex_token = next(
            token
            for token, value in hex_id.replacements.items()
            if value == "ABCDEF0123456789"
        )
        cases.append((hex_id, hex_id.protected.replace(hex_token, hex_token + ":x")))

        namespace = self.protector.protect(
            "Use minecraft",
            {"minecraft": "minecraft"},
        )
        namespace_token = next(iter(namespace.term_placeholders))
        cases.append(
            (
                namespace,
                namespace.protected.replace(
                    namespace_token,
                    namespace_token + ":stone",
                ),
            )
        )

        two_terms = self.protector.protect(
            "minecraft and stone",
            {"minecraft": "minecraft", "stone": "stone"},
        )
        minecraft_token, stone_token = two_terms.term_placeholders
        cases.append((two_terms, minecraft_token + ":" + stone_token))

        command_terms = self.protector.protect(
            "Create and give",
            {"Create": "Create", "give": "give"},
        )
        create_token, give_token = command_terms.term_placeholders
        cases.append((command_terms, create_token + "/" + give_token))

        format_term = self.protector.protect("a", {"a": "a"})
        cases.append((format_term, "§" + format_term.term_placeholders[0]))

        url_term = self.protector.protect(
            "example.invalid",
            {"example.invalid": "example.invalid"},
        )
        cases.append((url_term, "https://" + url_term.term_placeholders[0]))

        for protected, translated in cases:
            with (
                self.subTest(translated=translated),
                self.assertRaisesRegex(TranslationError, "原文にない装飾コード"),
            ):
                protected.restore(translated)  # type: ignore[attr-defined]

    def test_unsafe_glossary_value_falls_back_to_source_term(self) -> None:
        targets = (
            "§aCreate",
            "%s",
            "{player}",
            "https://x.invalid",
            "minecraft:stone",
            "\x00",
            "\u200b",
            "Create\u202e",
            " \t ",
            "\u0301",
        )
        for target in targets:
            with self.subTest(target=target):
                protected = self.protector.protect("Use TERM", {"TERM": target})

                self.assertEqual(protected.restore(protected.protected), "Use TERM")

    def test_balanced_parentheses_inside_url_are_preserved(self) -> None:
        url = "https://en.wikipedia.org/wiki/Function_(mathematics)"
        protected = self.protector.protect(f"See {url} now")

        self.assertIn(url, protected.replacements.values())
        self.assertEqual(protected.restore(protected.protected), f"See {url} now")

    def test_new_syntax_cannot_hide_immediately_after_another_placeholder(self) -> None:
        cases = (
            ("§aText", "§a", "minecraft:dirt", None),
            ("%s Text", "%s", "minecraft:dirt", None),
            (r"\nText", r"\n", "minecraft:dirt", None),
            ("Use Create", "Create", "/kill", {"Create": "Create"}),
            ("Keep __MQP_0000__ here", "__MQP_0000__", "/kill", None),
        )
        for source, value, suffix, terminology in cases:
            protected = self.protector.protect(source, terminology)
            placeholder = next(
                placeholder
                for placeholder, replacement in protected.replacements.items()
                if replacement == value
            )
            translated = protected.protected.replace(
                placeholder,
                placeholder + suffix,
            )

            with (
                self.subTest(source=source, suffix=suffix),
                self.assertRaisesRegex(TranslationError, "原文にない装飾コード"),
            ):
                protected.restore(translated)

    def test_multiple_urls_and_protected_terms_allow_cjk_separators(self) -> None:
        protected = self.protector.protect(
            "See https://a.invalid and https://b.invalid with Create",
            {"Create": "Create"},
        )
        first = next(
            placeholder
            for placeholder, value in protected.replacements.items()
            if value == "https://a.invalid"
        )
        second = next(
            placeholder
            for placeholder, value in protected.replacements.items()
            if value == "https://b.invalid"
        )
        term = next(
            placeholder
            for placeholder, value in protected.replacements.items()
            if value == "Create"
        )

        restored = protected.restore(f"前{first}・{second}で{term}を使う。")

        self.assertEqual(
            restored,
            "前https://a.invalid・https://b.invalidでCreateを使う。",
        )

    def test_url_does_not_swallow_adjacent_layout_template_or_cjk_text(self) -> None:
        cases = (
            ("https://x.invalid§aGreen", ("https://x.invalid", "§a")),
            ("https://x.invalid{0}", ("https://x.invalid", "{0}")),
            ("https://x.invalidを確認", ("https://x.invalid",)),
        )
        for source, expected_tokens in cases:
            with self.subTest(source=source):
                protected = self.protector.protect(source)

                self.assertEqual(special_tokens(source), expected_tokens)
                for token in expected_tokens:
                    self.assertNotIn(token, protected.protected)
                if source.endswith("を確認"):
                    self.assertIn("を確認", protected.protected)
                self.assertEqual(protected.restore(protected.protected), source)

    def test_provider_cannot_introduce_new_protected_syntax(self) -> None:
        protected = self.protector.protect("Translate this text")
        introduced = (
            "§a",
            "\n",
            r"\n",
            "%s",
            "{player}",
            "https://changed.invalid/path",
            "minecraft:diamond",
            "ABCDEF0123456789",
            "/give",
        )
        for syntax in introduced:
            with (
                self.subTest(syntax=syntax),
                self.assertRaisesRegex(TranslationError, "原文にない装飾コード"),
            ):
                protected.restore(f"翻訳{syntax}本文")

    def test_natural_percent_prose_is_not_a_printf_placeholder(self) -> None:
        source = "Progress is 50% complete and 80% done"
        protected = self.protector.protect(source)

        self.assertEqual(protected.protected, source)
        self.assertEqual(special_tokens(source), ())
        for token in ("%s", "%1$s", "%02d", "%%", "%n", "%1$tY", "%tF"):
            with self.subTest(token=token):
                self.assertEqual(special_tokens(token), (token,))

    def test_numeric_fraction_suffix_is_not_a_command_but_real_paths_remain_protected(self) -> None:
        for ordinary in ("スタック数減少/8", "/8", "容量/16"):
            with self.subTest(ordinary=ordinary):
                self.assertEqual(protected_syntax_signature(ordinary), ())
        for protected in ("/s", "/give", "/abc/2", "日本語/give"):
            with self.subTest(protected=protected):
                self.assertTrue(protected_syntax_signature(protected))

    def test_terminology_skeleton_removes_only_real_printf_arguments(self) -> None:
        self.assertEqual(
            terminology_literal_skeleton("%1$s%2$sNetherite Chest"),
            ("        Netherite Chest", True),
        )
        for dynamic in ("Percent %% Widget", "Line %n Widget", "Magic {0} Manual"):
            with self.subTest(dynamic=dynamic):
                self.assertIsNone(terminology_literal_skeleton(dynamic))

    def test_public_syntax_signatures_include_literal_tokens_in_source_order(self) -> None:
        source = "A§a __MQP_00AF__ {0} ${__MQP_0001__}"

        self.assertEqual(
            protected_syntax_signature(source),
            (
                ("special", "§a"),
                ("literal_placeholder", "__MQP_00AF__"),
                ("special", "{0}"),
                ("special", "${__MQP_0001__}"),
            ),
        )
        self.assertNotEqual(
            protected_layout_signature("§aHello"),
            protected_layout_signature("こんにちは§a"),
        )
        self.assertNotEqual(
            protected_layout_signature("A\nB"),
            protected_layout_signature("AB\n"),
        )
        self.assertEqual(
            protected_layout_signature("Use %s and {0}"),
            protected_layout_signature("{0}を%sで使う"),
        )

    def test_literal_placeholder_shaped_text_does_not_collide(self) -> None:
        source = "Keep literal __MQP_0000__ and §bthis code"
        protected = self.protector.protect(source)

        self.assertNotIn("__MQP_0001__", source)
        self.assertEqual(protected.restore(protected.protected), source)

    def test_glossary_longest_match_and_word_boundaries(self) -> None:
        source = "Use Iron Ingot, then Iron; SuperIron is not the item Iron."
        protected = self.protector.protect(
            source,
            {
                "Iron": "鉄",
                "Iron Ingot": "鉄インゴット",
            },
        )

        restored = protected.restore(protected.protected)

        self.assertEqual(restored, "Use 鉄インゴット, then 鉄; SuperIron is not the item 鉄.")

    def test_glossary_term_immediately_after_formatting_code_is_protected(self) -> None:
        cases = (
            "&bMachine&r",
            "§aMachine§r",
            "&#12AB34Machine&r",
            "&x&1&2&A&B&3&4Machine&r",
        )
        for source in cases:
            with self.subTest(source=source):
                protected = self.protector.protect(source, {"Machine": "機械"})
                self.assertNotIn("Machine", protected.protected)
                self.assertEqual(
                    protected.restore(protected.protected),
                    source.replace("Machine", "機械"),
                )

    def test_glossary_placeholder_rejects_ascii_attachment_but_allows_formatting_and_cjk(self) -> None:
        source = "§aCreateを使う"
        protected = self.protector.protect(source, {"Create": "Create"})
        term = next(
            placeholder
            for placeholder, value in protected.replacements.items()
            if value == "Create"
        )

        self.assertEqual(protected.restore(protected.protected), source)
        with self.assertRaisesRegex(TranslationError, "文字が連結"):
            protected.restore(protected.protected.replace(term, "Super" + term))
        with self.assertRaisesRegex(TranslationError, "文字が連結"):
            protected.restore(protected.protected.replace(term, term + "Machine"))

    def test_should_translate_ignores_only_protected_syntax(self) -> None:
        self.assertFalse(should_translate("§a\n\\n %1$s {quest:ABC} minecraft:stone"))
        self.assertFalse(should_translate("12345 ---"))
        self.assertTrue(should_translate("Craft minecraft:stone now"))
        self.assertTrue(should_translate("ダイヤモンドを入手"))

    def test_raw_json_text_prefix_accepts_component_arrays_not_bracketed_prose(self) -> None:
        for source in (
            '{"text":"Hello"}',
            '[{"text":"Hello"}]',
            ' [ [{"text":"Nested"}] ]',
            '["Literal", {"text":"component"}]',
        ):
            with self.subTest(source=source):
                self.assertTrue(looks_like_raw_json_text(source))
        for source in ("[Optional] objective", "[Chapter 1] Start", "[]", "[not-json]"):
            with self.subTest(source=source):
                self.assertFalse(looks_like_raw_json_text(source))


if __name__ == "__main__":
    unittest.main()
