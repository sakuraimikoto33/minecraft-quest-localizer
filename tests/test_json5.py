from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.json5 import Json5ParseError, parse_json5_language  # noqa: E402


class ParseJson5LanguageTests(unittest.TestCase):
    def test_comments_identifier_keys_strings_lists_and_trailing_commas(self) -> None:
        source = """\ufeff// heading
        {
          title: 'A title', # FTB-style extension
          $description_2: [
            "First line",
            /* between values */ 'Second line',
          ],
          日本語: "値",
          \\u006bey: "escaped identifier",
          "quest.00000000000000F2.title": "quoted dotted key",
        }
        // trailing comment
        """

        self.assertEqual(
            parse_json5_language(source),
            {
                "title": "A title",
                "$description_2": ["First line", "Second line"],
                "日本語": "値",
                "key": "escaped identifier",
                "quest.00000000000000F2.title": "quoted dotted key",
            },
        )

    def test_json5_string_escapes_and_surrogate_pair(self) -> None:
        source = r'''{
          escapes: "\" \' \\ \/ \b \f \n \r \t \v \0 \x48 \u65e5 \uD83D\uDE00 \q"
        }'''

        self.assertEqual(
            parse_json5_language(source)["escapes"],
            '" \' \\ / \b \f \n \r \t \v \0 H 日 😀 q',
        )

    def test_line_continuations_are_removed(self) -> None:
        source = "{lf: 'one\\\ntwo', crlf: \"three\\\r\nfour\"}"

        self.assertEqual(
            parse_json5_language(source),
            {"lf": "onetwo", "crlf": "threefour"},
        )

    def test_comment_markers_inside_strings_are_literal(self) -> None:
        self.assertEqual(
            parse_json5_language(
                r'''{"text": "# not // comments /* either */", url: "https://example.invalid/a#b"}'''
            ),
            {
                "text": "# not // comments /* either */",
                "url": "https://example.invalid/a#b",
            },
        )

    def test_duplicate_decoded_keys_are_rejected(self) -> None:
        cases = [
            "{key: 'one', key: 'two'}",
            "{key: 'one', \"key\": 'two'}",
            "{key: 'one', \\u006bey: 'two'}",
        ]

        for source in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(Json5ParseError, "Duplicate language key"):
                    parse_json5_language(source)

    def test_unquoted_dotted_or_hyphenated_keys_are_rejected(self) -> None:
        for source in ("{quest.title: 'bad'}", "{quest-title: 'bad'}"):
            with self.subTest(source=source):
                with self.assertRaises(Json5ParseError):
                    parse_json5_language(source)

    def test_commas_are_required(self) -> None:
        cases = [
            "{one: '1' two: '2'}",
            "{lines: ['one' 'two']}",
            "{one: '1',, two: '2'}",
            "{lines: ['one',, 'two']}",
        ]

        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(Json5ParseError):
                    parse_json5_language(source)

    def test_language_shape_is_restricted(self) -> None:
        cases = [
            "['not', 'an', 'object']",
            "{number: 1}",
            "{boolean: true}",
            "{nested: {value: 'no'}}",
            "{mixed: ['yes', 2]}",
        ]

        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(Json5ParseError):
                    parse_json5_language(source)

    def test_invalid_or_unterminated_escapes_comments_and_strings(self) -> None:
        cases = [
            r'''{key: "\xG0"}''',
            r'''{key: "\u12G4"}''',
            r'''{key: "\1"}''',
            r'''{key: "\09"}''',
            "{key: 'line\nbreak'}",
            "{key: 'unterminated}",
            "{key: 'value'} /* unterminated",
        ]

        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(Json5ParseError):
                    parse_json5_language(source)

    def test_errors_have_line_and_column(self) -> None:
        with self.assertRaises(Json5ParseError) as raised:
            parse_json5_language("// heading\r\n{\r\n  key: 1\r\n}\r\n")

        self.assertEqual((raised.exception.line, raised.exception.column), (3, 8))
        self.assertGreaterEqual(raised.exception.offset, 0)

    def test_empty_object_and_empty_list(self) -> None:
        self.assertEqual(parse_json5_language("{}"), {})
        self.assertEqual(parse_json5_language("{empty: []}"), {"empty": []})

    def test_non_string_source_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            parse_json5_language(b"{}")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
