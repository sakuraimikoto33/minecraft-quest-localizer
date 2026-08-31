from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.snbt import (  # noqa: E402
    SnbtCompound,
    SnbtList,
    SnbtParseError,
    SnbtScalar,
    SnbtString,
    dump_lang_snbt,
    parse_lang_snbt,
    parse_snbt,
)


class ParseSnbtTests(unittest.TestCase):
    def test_compound_list_comments_optional_commas_and_spans(self) -> None:
        source = """# file comment
{
  title: "Hello\\nWorld" // line comment
  count: 2b,
  values: ['one' bare "three",] # trailing comma is accepted
}
// trailing comment
"""

        root = parse_snbt(source)

        self.assertIsInstance(root, SnbtCompound)
        assert isinstance(root, SnbtCompound)
        self.assertEqual(root.span.extract(source), source[source.index("{") : source.index("}") + 1])
        self.assertEqual((root.span.start_line, root.span.start_column), (2, 1))
        self.assertEqual([entry.key for entry in root.entries], ["title", "count", "values"])

        title = root["title"]
        self.assertIsInstance(title, SnbtString)
        assert isinstance(title, SnbtString)
        self.assertEqual(title.value, "Hello\nWorld")
        self.assertEqual(title.quote, '"')
        self.assertEqual(title.span.extract(source), '"Hello\\nWorld"')
        self.assertEqual((title.span.start_line, title.span.start_column), (3, 10))

        count = root["count"]
        self.assertEqual(count, SnbtScalar(span=count.span, value="2b"))
        self.assertEqual(count.span.extract(source), "2b")

        values = root["values"]
        self.assertIsInstance(values, SnbtList)
        assert isinstance(values, SnbtList)
        self.assertEqual([item.value for item in values], ["one", "bare", "three"])

        title_entry = root.entries[0]
        self.assertEqual(title_entry.key_span.extract(source), "title")
        self.assertEqual(title_entry.span.extract(source), 'title: "Hello\\nWorld"')

    def test_nested_compounds_and_lists(self) -> None:
        root = parse_snbt('{outer: {enabled: true items: [1, {name: "x"}]}}')

        self.assertIsInstance(root, SnbtCompound)
        assert isinstance(root, SnbtCompound)
        outer = root["outer"]
        self.assertIsInstance(outer, SnbtCompound)
        assert isinstance(outer, SnbtCompound)
        self.assertEqual(outer["enabled"].value, "true")
        items = outer["items"]
        self.assertIsInstance(items, SnbtList)
        assert isinstance(items, SnbtList)
        self.assertEqual(items[0].value, "1")
        self.assertIsInstance(items[1], SnbtCompound)

    def test_quoted_string_escapes_and_unicode(self) -> None:
        root = parse_snbt(
            r'''{"double": "quote: \" slash: \\ tab:\t \u65e5 \uD83D\uDE00", 'single': 'it\'s'}'''
        )

        self.assertIsInstance(root, SnbtCompound)
        assert isinstance(root, SnbtCompound)
        self.assertEqual(root["double"].value, 'quote: " slash: \\ tab:\t 日 😀')
        self.assertEqual(root["single"].value, "it's")

    def test_comment_markers_inside_values_are_not_comments(self) -> None:
        root = parse_snbt('{url: https://example.invalid/a#part text: "# not // comments"}')

        self.assertIsInstance(root, SnbtCompound)
        assert isinstance(root, SnbtCompound)
        self.assertEqual(root["url"].value, "https://example.invalid/a#part")
        self.assertEqual(root["text"].value, "# not // comments")

    def test_crlf_span_coordinates(self) -> None:
        source = "// heading\r\n{\r\n  key: 'value'\r\n}\r\n"
        root = parse_snbt(source)

        self.assertIsInstance(root, SnbtCompound)
        assert isinstance(root, SnbtCompound)
        value = root["key"]
        self.assertEqual((root.span.start_line, root.span.start_column), (2, 1))
        self.assertEqual((value.span.start_line, value.span.start_column), (3, 8))
        self.assertEqual(value.span.extract(source), "'value'")

    def test_generic_compound_preserves_duplicate_entries(self) -> None:
        root = parse_snbt("{a: one, a: two}")

        self.assertIsInstance(root, SnbtCompound)
        assert isinstance(root, SnbtCompound)
        self.assertEqual([entry.value.value for entry in root.entries], ["one", "two"])

    def test_syntax_errors_include_locations(self) -> None:
        cases = [
            ("", "Expected an SNBT value"),
            ("{key value}", "Expected ':'"),
            ('["one""two"]', "Expected ',', whitespace, or ']"),
            ('{key: "bad\\q"}', "Unsupported escape sequence"),
            ("{key: [one", "Unterminated list"),
            ("{} trailing", "Unexpected trailing content"),
        ]

        for source, message in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(SnbtParseError, message) as raised:
                    parse_snbt(source)
                self.assertGreaterEqual(raised.exception.offset, 0)
                self.assertGreaterEqual(raised.exception.line, 1)
                self.assertGreaterEqual(raised.exception.column, 1)

    def test_non_string_source_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            parse_snbt(b"{}")  # type: ignore[arg-type]


class LangSnbtTests(unittest.TestCase):
    def test_parse_language_strings_and_lists(self) -> None:
        source = """
        {
          quest.title: "A Title"
          "quest.description": [
            "First line",
            'Second line'
            bare_line
          ]
          empty: []
          unquoted: bare_value
        }
        """

        self.assertEqual(
            parse_lang_snbt(source),
            {
                "quest.title": "A Title",
                "quest.description": ["First line", "Second line", "bare_line"],
                "empty": [],
                "unquoted": "bare_value",
            },
        )

    def test_language_shape_and_duplicate_errors(self) -> None:
        invalid_documents = [
            '["not", "a", "compound"]',
            "{duplicate: one duplicate: two}",
            "{nested: {value: nope}}",
            "{nested_list: [[nope]]}",
        ]

        for source in invalid_documents:
            with self.subTest(source=source):
                with self.assertRaises(SnbtParseError):
                    parse_lang_snbt(source)

    def test_dump_is_deterministic_and_round_trips(self) -> None:
        values = {
            "quest.title": '日本語 "Title"',
            "quest.description": ["line 1\ncontinued", "path\\name", ""],
            "empty": [],
            "bare-looking": "true",
        }

        dumped = dump_lang_snbt(values)

        self.assertEqual(
            dumped,
            """{
  "quest.title": "日本語 \\"Title\\"",
  "quest.description": [
    "line 1\\ncontinued",
    "path\\\\name",
    ""
  ],
  "empty": [],
  "bare-looking": "true"
}
""",
        )
        self.assertEqual(parse_lang_snbt(dumped), values)

    def test_dump_empty_mapping(self) -> None:
        self.assertEqual(dump_lang_snbt({}), "{\n}\n")

    def test_dump_accepts_tuple_values(self) -> None:
        dumped = dump_lang_snbt({"key": ("one", "two")})
        self.assertEqual(parse_lang_snbt(dumped), {"key": ["one", "two"]})

    def test_dump_rejects_invalid_values(self) -> None:
        cases = [
            [("not", "a mapping")],
            {1: "value"},
            {"key": 1},
            {"key": ["valid", 2]},
            {"key": b"bytes"},
        ]

        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(TypeError):
                    dump_lang_snbt(values)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
