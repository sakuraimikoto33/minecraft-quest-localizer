from __future__ import annotations

import json
import unittest

from tests.test_translator import PrefixClient  # Initializes the source path.
from mq_localizer.domain import TranslationError
from mq_localizer.openai_client import _prepare_structured_translation_items
from mq_localizer.protection import TokenProtector
from mq_localizer.translator import (
    _bundle_parts, _make_prepared_part, _provider_item, _restore_bundle_response,
)


class StyledRequestValidationTests(unittest.TestCase):
    def test_protected_and_nested_children_pass_local_request_validation(self) -> None:
        sources = (
            "Use &aStorage Bus'&r network.",
            "Use &6FTB Pyramid's &dDissolved Potential&r reward.",
        )
        terms = {
            "Storage Bus": "Storage Bus", "FTB Pyramid": "FTB Pyramid",
            "Dissolved Potential": "Dissolved Potential",
        }
        for source in sources:
            with self.subTest(source=source):
                protector = TokenProtector()
                root = _make_prepared_part(
                    part_id="u", protected=protector.protect(source, terms),
                    context="description", unit_key="quest.test", source_path="en_us.snbt",
                    protector=protector,
                )
                parts = _bundle_parts(root)
                self.assertGreater(len(parts), 1)
                prepared = _prepare_structured_translation_items([_provider_item(p) for p in parts])
                self.assertEqual(len(prepared), len(parts))
                payload = json.dumps([part.provider_item for part in prepared])
                self.assertNotIn("_source_text", payload)

    def test_protected_child_requires_matching_local_source_evidence(self) -> None:
        parent = {
            "id": "parent", "text": "Use __MQP_FFFF__.",
            "styled_bindings": [{"token": "__MQP_FFFF__", "source_text": "Bus'",
                                 "body_item_id": "child"}],
        }
        child = {"id": "child", "text": "__MQP_0000__'"}
        for evidence in ({}, {"_source_text": "Other"}):
            with self.subTest(evidence=evidence), self.assertRaises(TranslationError):
                _prepare_structured_translation_items([parent, child | evidence])
        self.assertEqual(len(_prepare_structured_translation_items([
            parent, child | {"_source_text": "Bus'"},
        ])), 2)

    def test_nested_style_reference_cycles_are_rejected(self) -> None:
        items = [
            {"id": item_id, "text": "__MQP_0000__", "_source_text": source,
             "styled_bindings": [{"token": "__MQP_0000__", "source_text": child_source,
                                  "body_item_id": child_id}]}
            for item_id, source, child_id, child_source in (
                ("a", "Source A", "b", "Source B"),
                ("b", "Source B", "a", "Source A"),
            )
        ]
        with self.assertRaisesRegex(TranslationError, "循環"):
            _prepare_structured_translation_items(items)

    def test_unchanged_bare_possessive_is_valid_but_missing_suffix_is_not(self) -> None:
        source = "Use &aStorage Bus'&r network."
        protector = TokenProtector()
        root = _make_prepared_part(
            part_id="u", protected=protector.protect(source, {"Storage Bus": "Storage Bus"}),
            context="description", unit_key="quest.test", source_path="en_us.snbt",
            protector=protector,
        )
        parts = _bundle_parts(root)
        response = {p.id: p.provider_projection.text for p in parts}
        restored, failure = _restore_bundle_response(root, response, "en_us", "en_gb", {})
        self.assertIsNone(failure)
        self.assertEqual(restored[root.id], source)
        child = next(p for p in parts if p.id != root.id)
        response[child.id] = response[child.id].replace("'", "")
        restored, failure = _restore_bundle_response(root, response, "en_us", "en_gb", {})
        self.assertEqual(restored, {})
        self.assertIsNotNone(failure)

    def test_closed_command_and_arguments_are_one_immutable_provider_token(self) -> None:
        command = "/ftbteams party invite <username>"
        protector = TokenProtector()
        root = _make_prepared_part(
            part_id="u", protected=protector.protect(f"Run &a{command}&f now."),
            context="description", unit_key="quest.command", source_path="en_us.snbt",
            protector=protector,
        )
        self.assertEqual(_bundle_parts(root), (root,))
        item = _provider_item(root)
        self.assertEqual(len(item["term_bindings"]), 1)
        binding = item["term_bindings"][0]
        self.assertEqual(binding["source_term"], command)
        self.assertEqual(binding["approved_output"], command)
        self.assertNotIn(command, item["text"])
        _prepare_structured_translation_items([item])
        restored, failure = _restore_bundle_response(
            root, {"u": binding["token"] + "を実行してください。"}, "en_us", "ja_jp", {},
        )
        self.assertIsNone(failure)
        self.assertEqual(restored["u"], f"&a{command}&fを実行してください。")


if __name__ == "__main__":
    unittest.main()
