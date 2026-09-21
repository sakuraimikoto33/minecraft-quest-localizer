from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from tests.test_quota import MemoryAdapter, exhausted
from mq_localizer.adapters.ftb_modern import FtbModernSnbtAdapter
from mq_localizer.adapters.ftb_split_json5 import FtbSplitJson5Adapter
from mq_localizer.adapters.ftb_split_snbt import FtbSplitSnbtAdapter
from mq_localizer.domain import CancelledError, TranslationError, TranslationProject, TranslationUnit
from mq_localizer.glossary import GlossaryCatalog
from mq_localizer.openai_client import OpenAIAPIError, OpenAIRefusalError, OpenAIResponseProtocolError
from mq_localizer.snbt import parse_lang_snbt
from mq_localizer.translator import TranslationOptions, TranslationService
from mq_localizer.ui import _format_translation_completion, _partial_confirmation_text, _translation_completion_title


class ScriptedClient:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def translate_batch(self, key, model, items, source, target, cancel=None):
        self.calls.append(items)
        if not self.steps:
            raise AssertionError("Unexpected API call")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(items)
        return {item["id"]: "翻訳" for item in items}


def project_for(sources=("Hello", "Goodbye")):
    return TranslationProject(
        "test", "test", Path("source"), Path("target"), "en_us", "ja_jp",
        [TranslationUnit(str(i), str(i), text) for i, text in enumerate(sources)],
    )


class PartialFailureTests(unittest.TestCase):
    def run_job(self, client, *, project=None, confirm=lambda _: True, batch_size=1, guard=None, cancel=None):
        self.adapter = MemoryAdapter()
        return TranslationService(client).translate(
            project or project_for(), self.adapter, Path("target"), "secret-test-key", "test-model",
            GlossaryCatalog(), TranslationOptions(batch_size=batch_size), confirm_partial=confirm,
            pre_write_guard=guard, cancel=cancel,
        )

    def test_api_failures_offer_safe_completed_results_and_show_error_reason(self):
        for failure in (OpenAIAPIError("request timed out", kind="timeout"),
                        OpenAIAPIError("HTTP 500", status=500),
                        OpenAIRefusalError("refused"), TranslationError("invalid response")):
            with self.subTest(failure=failure):
                states, guards = [], []
                def confirm(state):
                    self.assertEqual(self.adapter.writes, [])
                    states.append(state)
                    return True
                outcome = self.run_job(ScriptedClient(True, failure), confirm=confirm, guard=lambda: guards.append(True))
                self.assertTrue(outcome.partial and outcome.written)
                self.assertEqual(outcome.stop_reason, "error")
                self.assertEqual(outcome.completed, 1)
                self.assertEqual(self.adapter.writes, [({"0": "翻訳"}, {"selected_unit_ids": frozenset({"0"})})])
                self.assertEqual(guards, [True])
                self.assertIsNone(states[0].quota)
                self.assertIn(str(failure), _partial_confirmation_text(states[0]))
                self.assertNotIn("無料枠", _partial_confirmation_text(states[0]))
                self.assertIn("翻訳エラー", _format_translation_completion(outcome))
                self.assertIn("翻訳エラー", _translation_completion_title(outcome))

    def test_validation_failure_in_same_batch_excludes_failed_unit(self):
        client = ScriptedClient(lambda _: {"0": "安全な訳", "1": ""}, lambda _: {"1": ""})
        outcome = self.run_job(client, batch_size=24)
        self.assertEqual(outcome.completed, 1)
        self.assertEqual(self.adapter.writes[0][0], {"0": "安全な訳"})
        self.assertEqual(len(client.calls), 2)
        self.assertIn("翻訳本文が失われました", outcome.error_message)
        self.assertNotIn("書き込んでいません", outcome.error_message)

    def test_protocol_fallback_keeps_verified_individual_retries(self):
        client = ScriptedClient(OpenAIResponseProtocolError("bad batch"), True, OpenAIAPIError("timeout"))
        outcome = self.run_job(client, batch_size=24)
        self.assertEqual(outcome.completed, 1)
        self.assertEqual([len(items) for items in client.calls], [2, 1, 1])
        self.assertEqual(self.adapter.writes[0][0], {"0": "翻訳"})

    def test_incomplete_json_unit_and_styled_child_are_excluded(self):
        sources = (
            ('Hello', '{"text":"One","hoverEvent":{"action":"show_text","contents":"Two"}}'),
            ('Hello', 'Use &6Magic label&r.'),
        )
        for values in sources:
            with self.subTest(values=values):
                # One JSON leaf can be complete; a styled body alone cannot
                # complete its owning unit either.
                if values[1].startswith('{'):
                    client = ScriptedClient(True, True, TranslationError("failure"))
                else:
                    client = ScriptedClient(True, lambda items: {i["id"]: "翻訳" for i in items if "::styled::" in i["id"]}, TranslationError("failure"))
                outcome = self.run_job(client, project=project_for(values))
                self.assertEqual(outcome.completed, 1)
                self.assertEqual(self.adapter.writes[0][0], {"0": "翻訳"})

    def test_assembly_verification_failure_keeps_other_completed_units(self):
        with patch("mq_localizer.translator._assembled_translation_preserves_terms",
                   side_effect=lambda source, *_args, **_kwargs: source != "Goodbye"):
            outcome = self.run_job(ScriptedClient(True, True, True))
        self.assertEqual(outcome.completed, 1)
        self.assertEqual(self.adapter.writes[0][0], {"0": "翻訳"})

    def test_invalid_cross_unit_group_is_excluded_before_confirmation(self):
        project = project_for(("One", "Two", "Three", "Four"))
        project.metadata["terminology_groups"] = [("1", "2")]
        project.atomic_output_groups = (("2", "3"),)
        def unsafe(_project, resolved, *_args):
            return ("1", "2") if "1" in resolved else None
        with patch("mq_localizer.translator._first_unsafe_terminology_group", side_effect=unsafe):
            outcome = self.run_job(ScriptedClient(True), project=project, batch_size=24)
        self.assertEqual(outcome.completed, 1)
        self.assertEqual(self.adapter.writes[0][0], {"0": "翻訳"})

    def test_decline_returns_partial_outcome_without_writing(self):
        outcome = self.run_job(ScriptedClient(True, TranslationError("failure")), confirm=lambda _: False)
        self.assertEqual((outcome.partial, outcome.written, outcome.completed), (True, False, 1))
        self.assertEqual(self.adapter.writes, [])
        self.assertIn("書き込んでいません", _format_translation_completion(outcome))

    def test_no_callback_or_no_safe_units_still_raises_original_error(self):
        for steps, confirm in (
            ((True, TranslationError("original error")), None),
            ((TranslationError("original error"),), lambda _: self.fail("Nothing safe to save")),
        ):
            with self.subTest(steps=steps), self.assertRaisesRegex(TranslationError, "original error"):
                self.run_job(ScriptedClient(*steps), confirm=confirm)
            self.assertEqual(self.adapter.writes, [])

    def test_cancel_and_output_changes_do_not_become_partial_save_failures(self):
        with self.assertRaises(CancelledError):
            self.run_job(ScriptedClient(True, CancelledError("cancel")), confirm=lambda _: self.fail("Cancelled"))
        self.assertEqual(self.adapter.writes, [])
        def guard():
            raise TranslationError("output changed")
        with self.assertRaisesRegex(TranslationError, "output changed"):
            self.run_job(ScriptedClient(True, TranslationError("failure")), guard=guard)
        self.assertEqual(self.adapter.writes, [])
        cancel = Event()
        def confirm(_):
            cancel.set()
            return True
        with self.assertRaises(CancelledError):
            self.run_job(ScriptedClient(True, TranslationError("failure")), confirm=confirm, cancel=cancel)
        self.assertEqual(self.adapter.writes, [])

    def test_output_writer_failure_does_not_prompt_again(self):
        decisions = []
        def confirm(_):
            decisions.append(True)
            return True
        with patch.object(MemoryAdapter, "write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.run_job(ScriptedClient(True, TranslationError("failure")), confirm=confirm)
        self.assertEqual(decisions, [True])

    def test_error_reason_is_masked_and_dialog_is_bounded(self):
        messages = []
        failure = TranslationError("secret-test-key\n" + "detail\n" * 1000)
        def confirm(state):
            self.assertNotIn("secret-test-key", state.error_message)
            messages.append(_partial_confirmation_text(state))
            return True
        outcome = self.run_job(ScriptedClient(True, failure), confirm=confirm)
        self.assertNotIn("secret-test-key", outcome.error_message)
        self.assertGreater(len(outcome.error_message), 5000)
        self.assertLess(len(messages[0]), 1500)
        self.assertIn("詳細はログ", messages[0])

    def test_existing_and_code_only_units_can_be_saved_after_api_error(self):
        project = project_for(("Hello", "Goodbye", "&a&r"))
        project.existing["0"] = "既存訳"
        outcome = self.run_job(ScriptedClient(TranslationError("failure")), project=project)
        self.assertEqual((outcome.reused, outcome.copied_without_translation), (1, 1))
        self.assertEqual(set(self.adapter.writes[0][0]), {"0", "2"})


class PartialFailureArrayTests(unittest.TestCase):
    def test_real_locale_adapters_never_write_incomplete_arrays_after_error(self):
        for adapter, suffix, split in ((FtbModernSnbtAdapter(), ".snbt", False),
                                       (FtbSplitSnbtAdapter(), ".snbt", True),
                                       (FtbSplitJson5Adapter(), ".json5", True)):
            for accept in (True, False):
                with self.subTest(adapter=adapter.id, accept=accept), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "config/ftbquests/quests/lang"
                    source = root / "en_us" / ("chapter" + suffix) if split else root / ("en_us" + suffix)
                    source.parent.mkdir(parents=True)
                    source.write_text(json.dumps({"quest.A.quest_desc": ["First", ""],
                                                  "quest.B.quest_desc": ["Second", "", "Third"]}), encoding="utf-8")
                    project = adapter.load(source.parent if split else source, "en_us", "ja_jp", "1.21.1")
                    before = source.read_bytes()
                    outcome = TranslationService(ScriptedClient(True, True, TranslationError("timeout"))).translate(
                        project, adapter, project.default_output, "test", "test", GlossaryCatalog(),
                        TranslationOptions(batch_size=1), confirm_partial=lambda _: accept,
                    )
                    self.assertEqual(outcome.completed, 2)
                    output = project.default_output / ("chapter" + suffix) if split else project.default_output
                    self.assertEqual(output.exists(), accept)
                    if accept:
                        raw = output.read_text(encoding="utf-8")
                        values = json.loads(raw) if suffix == ".json5" else parse_lang_snbt(raw)
                        self.assertEqual(values, {"quest.A.quest_desc": ["翻訳", ""]})
                    self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
