from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.test_translator import PrefixClient, RecordingAdapter, _categorized_project
from mq_localizer.domain import TranslationError, TranslationProject
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry
from mq_localizer.protection import ProtectedText, TokenProtector, protected_layout_signature
from mq_localizer.translator import TranslationOptions, TranslationService


_SOURCE = (
    "Now we can &3Electrolyze&r the purified solution to create &bPlatinum Tiny Dust&r! "
    "With 9 of these, you can create &dPlatinum Dust."
)
_TRANSLATION = (
    "&b白金の小さな粉&rは精製液を&3電気分解&rして得られます！ "
    "9個を組み合わせて作れるもの：&d白金の粉."
)
_TERMS = {"Platinum Tiny Dust": "白金の小さな粉", "Platinum Dust": "白金の粉"}
_PLAIN_SUFFIX_SOURCE = (
    "Use &3Electrolyze&r to create &bPlatinum Tiny Dust&r; remember &dread the safety notes."
)
_PLAIN_SUFFIX_TRANSLATION = (
    "&b白金の小さな粉&rを作るには&3電気分解&rを使います。注意事項：&d安全上の注意を読んでください。"
)


def _glossary(terms: dict[str, str] | None = None) -> GlossaryCatalog:
    return GlossaryCatalog(entries={
        source: GlossaryEntry(
            source=source, target=target, key=f"test.{index}", mod_id="test",
            translated=source != target, provenance="test.jar!/assets/test/lang/en_us.json",
        )
        for index, (source, target) in enumerate((_TERMS if terms is None else terms).items())
    })


def _project(
    directory: Path, source: str = _SOURCE, *, existing: str = "",
) -> TranslationProject:
    return _categorized_project(
        directory,
        [("unit-1", "quest.reported.quest_desc[0]", source, "quest_description")],
        existing={"unit-1": existing} if existing else {},
    )


def _token(protected: ProtectedText, value: str) -> str:
    return next(token for token, literal in protected.replacements.items() if literal == value)


def _scope(protected: ProtectedText, opening: str) -> str:
    start = protected.protected.index(_token(protected, opening))
    for match in re.finditer(r"__MQP_[0-9A-F]{4}__", protected.protected[start:]):
        if protected.replacements[match.group()] == "&r":
            return protected.protected[start:start + match.end()]
    raise AssertionError("Expected a reset-closed scope")


class _PlatinumClient:
    def __init__(self, *, damage: str = "", plain_suffix: bool = False) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.damage = damage
        self.plain_suffix = plain_suffix

    def translate_batch(
        self, api_key: str, model: str, items: list[dict[str, Any]],
        source_locale: str, target_locale: str, cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        result: dict[str, str] = {}
        for item in items:
            if item["id"] != "unit-1":
                if item["text"] == "Electrolyze":
                    result[item["id"]] = "電気分解"
                elif item["text"] == "read the safety notes.":
                    suffix = "安全上の注意を読んでください。"
                    if self.damage == "missing_child":
                        suffix = ""
                    elif self.damage == "suffix_reset":
                        suffix += "&r"
                    elif self.damage == "suffix_newline":
                        suffix += "\n"
                    result[item["id"]] = suffix
                else:
                    raise AssertionError(f"Unexpected child source: {item['text']!r}")
                continue

            term = next(
                binding["token"] for binding in item.get("term_bindings", [])
                if binding["source_term"] == "Platinum Tiny Dust"
            )
            electrolyze = next(
                binding["token"] for binding in item.get("styled_bindings", [])
                if binding["source_text"] == "Electrolyze"
            )
            if self.damage == "lost_token":
                term = ""
            elif self.damage == "ascii_attachment":
                term += "s"
            if self.plain_suffix:
                parent = term + "を作るには" + electrolyze + "を使います。注意事項："
            else:
                parent = term + "は精製液を" + electrolyze + "して得られます！ 9個を組み合わせて作れるもの："
            if self.damage == "injected_terminal_token":
                protected = TokenProtector().protect(_SOURCE, _TERMS)
                parent += _token(protected, "&d")
            result[item["id"]] = parent
        return result


class TerminalStyleRegressionTests(unittest.TestCase):
    def test_reported_platinum_description_translates_without_retry(self) -> None:
        client = _PlatinumClient()
        with tempfile.TemporaryDirectory() as directory:
            project = _project(Path(directory))
            adapter = RecordingAdapter()
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model",
                _glossary(), TranslationOptions(),
            )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.calls[0][1]["unit-1"], _TRANSLATION)
        parent = next(item for item in client.calls[0] if item["id"] == "unit-1")
        self.assertEqual(
            [binding["source_term"] for binding in parent["term_bindings"]],
            ["Platinum Tiny Dust"],
        )
        self.assertEqual(
            [binding["source_text"] for binding in parent["styled_bindings"]],
            ["Electrolyze"],
        )
        self.assertNotIn("Platinum Dust", parent["text"])
        self.assertNotIn("&d", parent["text"])
        self.assertEqual(len(client.calls[0]), 2)

    def test_reported_safe_reordered_translation_is_reused(self) -> None:
        client = PrefixClient()
        with tempfile.TemporaryDirectory() as directory:
            project = _project(Path(directory), existing=_TRANSLATION)
            adapter = RecordingAdapter()
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model",
                _glossary(), TranslationOptions(),
            )
        self.assertEqual((outcome.translated, outcome.reused), (0, 1))
        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls[0][1]["unit-1"], _TRANSLATION)

    def test_original_language_terminal_name_is_also_restored_locally(self) -> None:
        glossary = _glossary(_TERMS | {"Platinum Dust": "Platinum Dust"})
        expected = _TRANSLATION.replace("白金の粉", "Platinum Dust")
        with tempfile.TemporaryDirectory() as directory:
            project = _project(Path(directory))
            adapter = RecordingAdapter()
            client = _PlatinumClient()
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model",
                glossary, TranslationOptions(),
            )
            self.assertEqual(outcome.translated, 1)
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(adapter.calls[0][1]["unit-1"], expected)
            project.existing = {"unit-1": expected}
            no_api = PrefixClient()
            reused = TranslationService(no_api).translate(
                project, RecordingAdapter(), project.default_output, "test", "model",
                glossary, TranslationOptions(),
            )
            self.assertEqual(reused.reused, 1)
            self.assertEqual(no_api.calls, [])

    def test_corrupt_provider_tokens_never_write_partial_translation(self) -> None:
        for damage in ("lost_token", "ascii_attachment", "injected_terminal_token"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                project = _project(Path(directory))
                adapter = RecordingAdapter(write_file=True)
                client = _PlatinumClient(damage=damage)
                glossary = _glossary(
                    {source: source for source in _TERMS}
                    if damage == "ascii_attachment" else None
                )
                with self.assertRaises(TranslationError):
                    TranslationService(client).translate(
                        project, adapter, project.default_output, "test", "model",
                        glossary, TranslationOptions(),
                    )
                self.assertEqual(adapter.calls, [])
                self.assertFalse(project.default_output.exists())
                self.assertEqual(len(client.calls), 2)

    def test_plain_terminal_body_is_translated_separately_and_appended(self) -> None:
        client = _PlatinumClient(plain_suffix=True)
        with tempfile.TemporaryDirectory() as directory:
            project = _project(Path(directory), _PLAIN_SUFFIX_SOURCE)
            adapter = RecordingAdapter()
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model",
                _glossary(), TranslationOptions(),
            )
        self.assertEqual(outcome.translated, 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.calls[0][1]["unit-1"], _PLAIN_SUFFIX_TRANSLATION)
        parent = next(item for item in client.calls[0] if item["id"] == "unit-1")
        self.assertNotIn("read the safety notes", parent["text"])
        self.assertEqual(
            [binding["source_text"] for binding in parent["styled_bindings"]],
            ["Electrolyze"],
        )
        self.assertEqual(len(client.calls[0]), 3)

    def test_corrupted_terminal_child_never_writes_partial_translation(self) -> None:
        for damage in ("missing_child", "suffix_reset", "suffix_newline"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                project = _project(Path(directory), _PLAIN_SUFFIX_SOURCE)
                adapter = RecordingAdapter(write_file=True)
                client = _PlatinumClient(damage=damage, plain_suffix=True)
                with self.assertRaises(TranslationError):
                    TranslationService(client).translate(
                        project, adapter, project.default_output, "test", "model",
                        _glossary(), TranslationOptions(),
                    )
                self.assertEqual(adapter.calls, [])
                self.assertFalse(project.default_output.exists())
                self.assertEqual(len(client.calls), 2)

    def test_closed_prefix_scopes_can_reorder_while_suffix_stays_last(self) -> None:
        protected = TokenProtector().protect(_SOURCE, _TERMS)
        electrolysis = _scope(protected, "&3").replace("Electrolyze", "電気分解")
        tiny_dust = _scope(protected, "&b")
        suffix = protected.protected[protected.protected.index(_token(protected, "&d")):]
        candidate = (
            tiny_dust + "は精製液を" + electrolysis
            + "して得られます！ 9個を組み合わせて作れるもの：" + suffix
        )
        self.assertEqual(protected.restore(candidate), _TRANSLATION)
        self.assertEqual(
            protected_layout_signature(_SOURCE, _TERMS),
            protected_layout_signature(_TRANSLATION, {term: term for term in _TERMS.values()}),
        )

    def test_terminal_scope_cannot_move_before_prefix_scopes(self) -> None:
        protected = TokenProtector().protect(_SOURCE, _TERMS)
        suffix = protected.protected[protected.protected.index(_token(protected, "&d")):]
        prefix = protected.protected[:-len(suffix)]
        tiny_dust = _scope(protected, "&b")
        for candidate in (suffix + prefix, prefix.replace(tiny_dust, suffix + tiny_dust)):
            with self.subTest(candidate=candidate), self.assertRaises(TranslationError):
                protected.restore(candidate)

    def test_term_or_ordinary_text_cannot_cross_terminal_boundary(self) -> None:
        protected = TokenProtector().protect(_SOURCE, _TERMS)
        tiny, dust = (_token(protected, target) for target in _TERMS.values())
        candidates = (
            protected.protected.replace(tiny, "SWAP").replace(dust, tiny).replace("SWAP", dust),
            protected.protected.replace(dust, "追加" + dust),
            protected.protected.replace(dust, dust + "s"),
            protected.protected.replace(dust, dust + "&r"),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(TranslationError):
                protected.restore(candidate)

    def test_complex_unclosed_tail_does_not_allow_color_scope_reordering(self) -> None:
        source = "Use &3Electrolyze&r before &dPlatinum Dust and &eSafety advice"
        protected = TokenProtector().protect(source, _TERMS)
        scope = _scope(protected, "&3")
        candidate = protected.protected.replace(scope, "", 1) + scope
        with self.assertRaises(TranslationError):
            protected.restore(candidate)

    def test_terminal_scope_does_not_allow_crossing_layout_boundaries(self) -> None:
        for boundary in ("\n", "\r\n", r"\n", r"\r\n", "\t", r"\t"):
            source = "Use &3Electrolyze&r" + boundary + "to obtain &dPlatinum Dust."
            protected = TokenProtector().protect(source, _TERMS)
            scope = _scope(protected, "&3")
            candidate = protected.protected.replace(scope, "", 1).replace("to obtain", scope + "to obtain")
            with self.subTest(boundary=boundary), self.assertRaises(TranslationError):
                protected.restore(candidate)

    def test_orphan_reset_remains_a_fixed_boundary(self) -> None:
        source = "First &rUse &3Electrolyze&r before &dPlatinum Dust."
        protected = TokenProtector().protect(source, _TERMS)
        scope = _scope(protected, "&3")
        candidate = scope + protected.protected.replace(scope, "", 1)
        with self.assertRaises(TranslationError):
            protected.restore(candidate)

    def test_escaped_closed_scope_can_move_before_fixed_terminal_name(self) -> None:
        source = r"Use &3Flint \&\ Steel&r with &bTool&r before &dDust."
        terms = {"Tool": "Tool", "Dust": "Dust"}
        protected = TokenProtector().protect(source, terms)
        flint = _scope(protected, "&3").replace("Flint", "火打石").replace("Steel", "打ち金")
        tool = _scope(protected, "&b")
        suffix = protected.protected[protected.protected.index(_token(protected, "&d")):]
        candidate = tool + "には" + flint + "を使います。次は" + suffix
        restored = protected.restore(candidate)
        self.assertEqual(restored, r"&bTool&rには&3火打石 \&\ 打ち金&rを使います。次は&dDust.")
        self.assertEqual(
            protected_layout_signature(source, terms),
            protected_layout_signature(restored, terms),
        )

    def test_color_inheriting_tail_keeps_white_closed_prefix_strict(self) -> None:
        source = "Use &aFoo&f with &bBar&r before &lBold tail"
        protected = TokenProtector().protect(source)
        a, white, b, reset = (_token(protected, value) for value in ("&a", "&f", "&b", "&r"))
        foo, bar = a + "Foo" + white, b + "Bar" + reset
        candidate = protected.protected.replace(foo, "SWAP").replace(bar, foo).replace("SWAP", bar)
        with self.assertRaises(TranslationError):
            protected.restore(candidate)

    def test_explicit_tail_color_stack_preserves_order_and_position(self) -> None:
        source = "Use &aFoo&r then &d&lDust."
        protected = TokenProtector().protect(source, {"Foo": "Foo", "Dust": "Dust"})
        color, modifier = (_token(protected, value) for value in ("&d", "&l"))
        candidate = protected.protected.replace(color + modifier, modifier + color)
        with self.assertRaises(TranslationError):
            protected.restore(candidate)


if __name__ == "__main__":
    unittest.main()
