from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.test_translator import PrefixClient, RecordingAdapter, _categorized_project
from mq_localizer.domain import TranslationError, TranslationProject
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry
from mq_localizer.protection import (
    ProtectedText,
    TokenProtector,
    protected_layout_signature,
    special_tokens,
)
from mq_localizer.translator import TranslationOptions, TranslationService


_SOURCE = (
    "First, you must place the necessary items on the &aAltar&r, and ignite "
    "the necessary &2Incense&r items with a &3Flint \\&\\ Steel&r. Then "
    "sneak-right-click the &aAltar&r with an empty hand to begin the &bRitual&r."
)
_TRANSLATION = (
    "まず、&aAltar&rに必要なアイテムを置き、&3火打石 \\&\\ 打ち金&rで必要な"
    "&2お香&rに火をつけます。次に、何も持たずに&aAltar&rをスニーク右クリックして"
    "&b儀式&rを開始します。"
)


def _glossary() -> GlossaryCatalog:
    return GlossaryCatalog(
        entries={
            name: GlossaryEntry(
                source=name,
                target=target,
                key=f"test.{name.lower()}",
                mod_id="test",
                translated=name != target,
                provenance="test.jar!/assets/test/lang/en_us.json",
            )
            for name, target in (("Altar", "Altar"), ("Ritual", "儀式"))
        }
    )


def _ritual_project(
    directory: Path, *, existing: dict[str, str] | None = None,
) -> TranslationProject:
    return _categorized_project(
        directory,
        [("unit-1", "quest.0B1BDF71E95BC9D3.quest_desc[4]", _SOURCE, "quest_description")],
        existing=existing,
    )


def _token(protected: ProtectedText, value: str) -> str:
    return next(token for token, literal in protected.replacements.items() if literal == value)


def _scope(protected: ProtectedText, opening: str) -> str:
    start = protected.protected.index(_token(protected, opening))
    for match in re.finditer(r"__MQP_[0-9A-F]{4}__", protected.protected[start:]):
        if protected.replacements[match.group()] == "&r":
            return protected.protected[start : start + match.end()]
    raise AssertionError("Expected reset-closed scope")


class _RitualClient:
    def __init__(self, *, damage_escape: bool = False) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.damage_escape = damage_escape

    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, Any]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        result: dict[str, str] = {}
        for item in items:
            if item["id"] != "unit-1":
                translated = item["text"].replace("Incense", "お香")
                translated = translated.replace("Flint", "火打石").replace("Steel", "打ち金")
                if self.damage_escape and "Flint" in item["text"]:
                    translated = re.sub(r"__MQP_[0-9A-F]{4}__", "", translated)
                result[item["id"]] = translated
                continue
            altars = [b["token"] for b in item["term_bindings"] if b["source_term"] == "Altar"]
            ritual = next(b["token"] for b in item["term_bindings"] if b["source_term"] == "Ritual")
            styled = {b["source_text"]: b["token"] for b in item["styled_bindings"]}
            incense = styled["Incense"]
            flint = styled[r"Flint \&\ Steel"]
            result[item["id"]] = (
                "まず、" + altars[0] + "に必要なアイテムを置き、" + flint + "で必要な"
                + incense + "に火をつけます。次に、何も持たずに" + altars[1]
                + "をスニーク右クリックして" + ritual + "を開始します。"
            )
        return result


class StyledEscapesTests(unittest.TestCase):
    def test_reported_ritual_description_translates_without_retry(self) -> None:
        client = _RitualClient()
        with tempfile.TemporaryDirectory() as directory:
            project = _ritual_project(Path(directory))
            adapter = RecordingAdapter()
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test-key", "test-model",
                _glossary(), TranslationOptions(),
            )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.calls[0][1]["unit-1"], _TRANSLATION)
        parent = next(item for item in client.calls[0] if item["id"] == "unit-1")
        self.assertEqual({b["source_term"] for b in parent["term_bindings"]}, {"Altar", "Ritual"})
        self.assertEqual({b["source_text"] for b in parent["styled_bindings"]}, {"Incense", r"Flint \&\ Steel"})

    def test_reported_safe_reordered_translation_is_reused(self) -> None:
        client = PrefixClient()
        with tempfile.TemporaryDirectory() as directory:
            project = _ritual_project(Path(directory), existing={"unit-1": _TRANSLATION})
            adapter = RecordingAdapter()
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test-key", "test-model",
                _glossary(), TranslationOptions(),
            )
        self.assertEqual((outcome.translated, outcome.reused), (0, 1))
        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls[0][1]["unit-1"], _TRANSLATION)

    def test_missing_escape_never_writes_partial_translation(self) -> None:
        client = _RitualClient(damage_escape=True)
        with tempfile.TemporaryDirectory() as directory:
            project = _ritual_project(Path(directory))
            adapter = RecordingAdapter(write_file=True)
            with self.assertRaises(TranslationError):
                TranslationService(client).translate(
                    project, adapter, project.default_output, "test-key", "test-model",
                    _glossary(), TranslationOptions(),
                )
            self.assertEqual(adapter.calls, [])
            self.assertFalse(project.default_output.exists())
        self.assertEqual(len(client.calls), 2)

    def test_scope_with_either_escape_spelling_moves_as_one_unit(self) -> None:
        for escaped in (r"\&", "\\&\\"):
            with self.subTest(escaped=escaped):
                source = "Use &3Flint " + escaped + " Steel&r with &aAltar&r."
                protected = TokenProtector().protect(source, {"Altar": "Altar"})
                flint = _scope(protected, "&3").replace("Flint", "火打石").replace("Steel", "打ち金")
                altar = _scope(protected, "&a")
                candidate = altar + "には" + flint + "を使います。"
                restored = protected.restore(candidate)
                self.assertEqual(restored, "&aAltar&rには&3火打石 " + escaped + " 打ち金&rを使います。")
                self.assertEqual(
                    protected_layout_signature(source, {"Altar": "Altar"}),
                    protected_layout_signature(restored, {"Altar": "Altar"}),
                )

    def test_escaped_literal_cannot_move_to_scope_edges_or_outside(self) -> None:
        protected = TokenProtector().protect(r"Use &3Flint \&\ Steel&r now")
        escaped = _token(protected, "\\&\\")
        opening = _token(protected, "&3")
        reset = _token(protected, "&r")
        without = protected.protected.replace(escaped, "", 1)
        candidates = (
            without.replace(opening, opening + escaped, 1),
            without.replace(reset, escaped + reset, 1),
            escaped + without,
            without + escaped,
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(TranslationError):
                protected.restore(candidate)

    def test_escape_cannot_move_to_a_different_color_interval(self) -> None:
        protected = TokenProtector().protect(r"Use &3Flint \&\ Steel &aAltar&r now")
        escaped = _token(protected, "\\&\\")
        candidate = protected.protected.replace(escaped, "", 1).replace("Altar", "Al" + escaped + "tar")
        with self.assertRaises(TranslationError):
            protected.restore(candidate)

    def test_protected_terms_cannot_cross_an_escaped_literal(self) -> None:
        protected = TokenProtector().protect(
            r"Use &3Flint \&\ Steel&r now", {"Flint": "火打石", "Steel": "打ち金"},
        )
        left, right = protected.term_placeholders
        candidate = protected.protected.replace(left, "SWAP").replace(right, left).replace("SWAP", right)
        with self.assertRaises(TranslationError):
            protected.restore(candidate)

    def test_missing_or_duplicated_escape_is_rejected(self) -> None:
        protected = TokenProtector().protect(r"Use &3Flint \&\ Steel&r now")
        escaped = _token(protected, "\\&\\")
        for replacement in ("", escaped + escaped):
            with self.subTest(replacement=replacement), self.assertRaises(TranslationError):
                protected.restore(protected.protected.replace(escaped, replacement))

    def test_closed_scope_cannot_cross_physical_or_escaped_newlines(self) -> None:
        for newline in ("\n", "\r\n", r"\n", r"\r\n"):
            source = r"Use &3Flint \&\ Steel&r" + newline + "Then &aAltar&r"
            protected = TokenProtector().protect(source)
            flint, altar = _scope(protected, "&3"), _scope(protected, "&a")
            candidate = protected.protected.replace(flint, "SWAP").replace(altar, flint).replace("SWAP", altar)
            with self.subTest(newline=newline), self.assertRaises(TranslationError):
                protected.restore(candidate)

    def test_escape_in_unclosed_or_unstyled_text_remains_a_boundary(self) -> None:
        for source in (r"Use &3Flint \&\ Steel", r"Use Flint \&\ Steel"):
            protected = TokenProtector().protect(source)
            escaped = _token(protected, "\\&\\")
            self.assertIn(escaped, protected.structural_placeholders)
            without = protected.protected.replace(escaped, "", 1)
            for candidate in (escaped + without, without + escaped):
                with self.subTest(source=source, candidate=candidate), self.assertRaises(TranslationError):
                    protected.restore(candidate)

    def test_unclosed_style_does_not_gain_movable_scope_behavior(self) -> None:
        protected = TokenProtector().protect(r"Use &3Flint \&\ Steel")
        opening = _token(protected, "&3")
        candidate = opening + protected.protected.replace(opening, "", 1)
        with self.assertRaises(TranslationError):
            protected.restore(candidate)

    def test_escape_outside_closed_scopes_remains_a_boundary(self) -> None:
        protected = TokenProtector().protect(r"Use &aAltar&r \&\ &bRitual&r now")
        escaped = _token(protected, "\\&\\")
        self.assertIn(escaped, protected.structural_placeholders)
        altar, ritual = _scope(protected, "&a"), _scope(protected, "&b")
        candidate = protected.protected.replace(altar, "SWAP").replace(ritual, altar).replace("SWAP", ritual)
        with self.assertRaises(TranslationError):
            protected.restore(candidate)

    def test_escape_match_does_not_consume_next_escape_prefix(self) -> None:
        cases = (
            (r"\&\n", (r"\&", r"\n")),
            (r"\&\r\n", (r"\&", r"\r\n")),
            (r"\&\t", (r"\&", r"\t")),
            (r"\&\&a", (r"\&", r"\&")),
            (r"Flint \&\ Steel", ("\\&\\",)),
        )
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(tuple(special_tokens(source)), expected)
                protected = TokenProtector().protect(source)
                self.assertEqual(protected.restore(protected.protected), source)

    def test_symbol_only_scope_cannot_change_or_lose_its_symbol(self) -> None:
        source = "Click &a+&r beside the card."
        protected = TokenProtector().protect(source)
        for replacement in ("", "-", "×", "++"):
            with self.subTest(replacement=replacement):
                with self.assertRaises(TranslationError):
                    protected.restore(protected.protected.replace("+", replacement))
                self.assertNotEqual(
                    protected_layout_signature(source),
                    protected_layout_signature(source.replace("+", replacement)),
                )


if __name__ == "__main__":
    unittest.main()
