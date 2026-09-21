from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests.test_translator import PrefixClient, RecordingAdapter, _categorized_project
from mq_localizer.domain import TranslationError
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry
from mq_localizer.translator import TranslationOptions, TranslationService


_SOURCE = (
    "Connecting a &bStorage Bus&r to an &aInterface&r will allow the "
    "&bStorage Bus'&r entire storage network to connect to the &aInterface&r "
    "as if it were placed on one big chest."
)
_EXPECTED = (
    "&aInterface&rに&bStorage Bus&rを接続すると、&bStorage Busの&r"
    "ストレージネットワーク全体を、一つの大きなチェストに接続するように"
    "&aInterface&rへ接続できます。"
)
_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")


def _glossary(*pairs: tuple[str, str]) -> GlossaryCatalog:
    return GlossaryCatalog(entries={
        source: GlossaryEntry(
            source=source, target=target, key=f"test.{index}", mod_id="test",
            translated=source != target,
            provenance="test.jar!/assets/test/lang/en_us.json",
        )
        for index, (source, target) in enumerate(pairs)
    })


def _project(root: Path, source: str = _SOURCE, existing: str | None = None):
    return _categorized_project(
        root,
        [("description", "quest.1B1E4B79CB5D4C35.quest_desc[8]", source, "quest_description")],
        existing={"description": existing} if existing is not None else None,
    )


class _StorageClient:
    def __init__(self, damage: str = "") -> None:
        self.damage = damage
        self.calls: list[list[dict[str, Any]]] = []

    def translate_batch(self, api_key, model, items, source_locale, target_locale, cancel=None):
        self.calls.append(items)
        response: dict[str, str] = {}
        moved = ""
        for item in items:
            terms = item.get("term_bindings", [])
            if item["id"] != "description":
                token = terms[0]["token"]
                response[item["id"]] = token + "の"
                if self.damage in {"drop_term", "move_term"}:
                    response[item["id"]] = "の"
                    moved = token if self.damage == "move_term" else ""
                elif self.damage == "duplicate_term":
                    response[item["id"]] += token
                continue
            bus = next(b["token"] for b in terms if b["source_term"] == "Storage Bus")
            interfaces = [b["token"] for b in terms if b["source_term"] == "Interface"]
            styled = item["styled_bindings"][0]["token"]
            response[item["id"]] = (
                interfaces[0] + "に" + bus + "を接続すると、" + styled
                + "ストレージネットワーク全体を、一つの大きなチェストに接続するように"
                + interfaces[1] + "へ接続できます。" + moved
            )
            if self.damage == "drop_scope":
                response[item["id"]] = response[item["id"]].replace(styled, "")
        return response


class MixedStyleRegressionTests(unittest.TestCase):
    def test_actual_storage_bus_possessive_translates_once_and_is_reused(self) -> None:
        glossary = _glossary(("Storage Bus", "Storage Bus"), ("Interface", "Interface"))
        client = _StorageClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(root)
            adapter = RecordingAdapter()
            outcome = TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model", glossary,
                TranslationOptions(),
            )
            self.assertEqual(adapter.calls[0][1]["description"], _EXPECTED)
            self.assertEqual(outcome.translated, 1)
            self.assertEqual(len(client.calls), 1)
            child, parent = client.calls[0]
            self.assertEqual(child["term_bindings"][0]["source_term"], "Storage Bus")
            self.assertEqual(child["text"], child["term_bindings"][0]["token"] + "'")
            self.assertNotIn("'", parent["text"])
            self.assertEqual(parent["styled_bindings"][0]["source_text"], "Storage Bus'")
            self.assertEqual(len(parent["term_bindings"]), 3)

            reuse_client = PrefixClient()
            reused_project = _project(root, existing=_EXPECTED)
            reused_adapter = RecordingAdapter()
            reused = TranslationService(reuse_client).translate(
                reused_project, reused_adapter, reused_project.default_output,
                "test", "model", glossary, TranslationOptions(),
            )
            self.assertEqual(reused.reused, 1)
            self.assertEqual(reuse_client.calls, [])
            self.assertEqual(reused_adapter.calls[0][1]["description"], _EXPECTED)

    def test_repeated_name_occurrences_keep_their_own_styles_and_particles(self) -> None:
        source = "Use &aStorage Bus&r with &bStorage Bus'&r network and &cStorage Bus's&r settings."
        expected = "&cStorage Busの&r設定と&bStorage Busの&rネットワークで&aStorage Bus&rを使います。"

        class RepeatedClient(PrefixClient):
            def translate_batch(self, api_key, model, items, source_locale, target_locale, cancel=None):
                self.calls.append(items)
                result = {}
                for item in items:
                    if item["id"] != "description":
                        result[item["id"]] = item["term_bindings"][0]["token"] + "の"
                    else:
                        styles = {b["source_text"]: b["token"] for b in item["styled_bindings"]}
                        result[item["id"]] = (
                            styles["Storage Bus's"] + "設定と" + styles["Storage Bus'"]
                            + "ネットワークで" + item["term_bindings"][0]["token"] + "を使います。"
                        )
                return result

        client = RepeatedClient()
        with tempfile.TemporaryDirectory() as directory:
            project = _project(Path(directory), source)
            adapter = RecordingAdapter()
            TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model",
                _glossary(("Storage Bus", "Storage Bus")), TranslationOptions(),
            )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.calls[0][1]["description"], expected)
        self.assertEqual(len(client.calls[0]), 3)

    def test_child_or_parent_token_damage_never_reaches_writer(self) -> None:
        for damage in ("drop_term", "move_term", "duplicate_term", "drop_scope"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                project = _project(Path(directory))
                client = _StorageClient(damage)
                adapter = RecordingAdapter(write_file=True)
                with self.assertRaises(TranslationError):
                    TranslationService(client).translate(
                        project, adapter, project.default_output, "test", "model",
                        _glossary(("Storage Bus", "Storage Bus"), ("Interface", "Interface")),
                        TranslationOptions(),
                    )
                self.assertEqual(adapter.calls, [])
                self.assertFalse(project.default_output.exists())
                self.assertEqual(len(client.calls), 2)

    def test_mixed_child_cannot_lose_meaningful_modifier(self) -> None:
        class MissingBodyClient(PrefixClient):
            def translate_batch(self, api_key, model, items, source_locale, target_locale, cancel=None):
                self.calls.append(items)
                return {
                    item["id"]: (
                        item["term_bindings"][0]["token"] if item.get("term_bindings")
                        else item["styled_bindings"][0]["token"] + "を使います。"
                    ) for item in items
                }

        with tempfile.TemporaryDirectory() as directory:
            project = _project(Path(directory), "Use &bupgraded Storage Bus&r now.")
            adapter = RecordingAdapter(write_file=True)
            client = MissingBodyClient()
            with self.assertRaisesRegex(TranslationError, "翻訳本文が失われました"):
                TranslationService(client).translate(
                    project, adapter, project.default_output, "test", "model",
                    _glossary(("Storage Bus", "Storage Bus")), TranslationOptions(),
                )
            self.assertEqual(adapter.calls, [])
            self.assertFalse(project.default_output.exists())

    def test_mixed_child_maps_duplicate_translated_values_by_occurrence(self) -> None:
        class SameTargetClient(PrefixClient):
            def translate_batch(self, api_key, model, items, source_locale, target_locale, cancel=None):
                self.calls.append(items)
                results = {}
                for item in items:
                    terms = item.get("term_bindings", [])
                    if terms:
                        results[item["id"]] = terms[1]["token"] + "と" + terms[0]["token"] + "を比較"
                    else:
                        results[item["id"]] = item["styled_bindings"][0]["token"] + "します。"
                return results

        glossary = _glossary(("Copper Gear", "歯車"), ("Iron Gear", "歯車"))
        client = SameTargetClient()
        with tempfile.TemporaryDirectory() as directory:
            project = _project(Path(directory), "Now &aCompare Copper Gear with Iron Gear&r.")
            adapter = RecordingAdapter()
            TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model", glossary,
                TranslationOptions(),
            )
        self.assertEqual(adapter.calls[0][1]["description"], "&a歯車と歯車を比較&rします。")
        terms = client.calls[0][0]["term_bindings"]
        self.assertEqual([term["approved_output"] for term in terms], ["歯車", "歯車"])
        self.assertNotEqual(terms[0]["token"], terms[1]["token"])

    def test_nested_compound_style_maps_term_and_translated_possessive(self) -> None:
        class NestedClient(PrefixClient):
            def translate_batch(self, api_key, model, items, source_locale, target_locale, cancel=None):
                self.calls.append(items)
                result = {}
                for item in items:
                    if item["id"] == "description":
                        result[item["id"]] = item["styled_bindings"][0]["token"] + "を作ります。"
                    else:
                        result[item["id"]] = item["text"].replace("FTB Pyramid's ", "FTB Pyramidの")
                return result

        client = NestedClient()
        with tempfile.TemporaryDirectory() as directory:
            project = _project(Path(directory), "Make &6FTB Pyramid's &dDissolved Potential&r.")
            adapter = RecordingAdapter()
            TranslationService(client).translate(
                project, adapter, project.default_output, "test", "model",
                _glossary(("Dissolved Potential", "Dissolved Potential")), TranslationOptions(),
            )
        self.assertEqual(adapter.calls[0][1]["description"], "&6FTB Pyramidの&dDissolved Potential&rを作ります。")
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("FTB Pyramid", client.calls[0][-1]["text"])
        self.assertNotIn("Dissolved Potential", client.calls[0][-1]["text"])
        self.assertEqual(len(_TOKEN.findall(client.calls[0][-1]["text"])), 1)


if __name__ == "__main__":
    unittest.main()
