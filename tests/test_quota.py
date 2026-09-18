from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.config import AppSettings, SettingsStore
from mq_localizer.domain import CancelledError, TranslationError, TranslationProject, TranslationUnit
from mq_localizer.glossary import GlossaryCatalog
from mq_localizer.openai_client import OpenAIAPIError, OpenAIClient, OpenAIResponseProtocolError
from mq_localizer.quota import ComplimentaryQuotaExhausted, QuotaLedger, QuotaStatus, group_for_model, limits_for_usage_tier
from mq_localizer.translator import TranslationOptions, TranslationService
from mq_localizer.ui import _format_translation_completion, _partial_confirmation_text


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "openai-usage.json"
        self.now = datetime(2026, 9, 18, 23, 59, tzinfo=timezone.utc)
        self.ledger = QuotaLedger(self.path, now=lambda: self.now)

    def test_groups_and_tiers(self):
        for model in ("gpt-5.6-sol", "gpt-5.4", "gpt-4o-2024-08-06", "o3"):
            self.assertEqual(group_for_model(model), "1m")
        for model in ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-5-mini", "gpt-4o-mini"):
            self.assertEqual(group_for_model(model), "10m")
        for model in ("ft:gpt-5-mini:a", "gpt-5.6-sol-2099-01-01", "gpt-5-pro", "gpt-6-astra", "provider/gpt-5"):
            self.assertIsNone(group_for_model(model))
        for tier in range(1, 6):
            self.assertEqual(limits_for_usage_tier(tier)["1m"], 250_000 if tier < 3 else 1_000_000)
        for tier in (0, 6, True, "1"):
            with self.assertRaises(ValueError):
                limits_for_usage_tier(tier)

    def test_reserve_settle_persist_and_exact_boundary(self):
        reservation = self.ledger.reserve("1m", 1, 250_000)
        self.assertEqual(self.ledger.statuses(1)["1m"].remaining_tokens, 0)
        with self.assertRaises(ComplimentaryQuotaExhausted):
            QuotaLedger(self.path, now=lambda: self.now).reserve("1m", 1, 1)
        self.ledger.settle(reservation, 1234)
        status = self.ledger.statuses(1)["1m"]
        self.assertEqual((status.used_tokens, status.reserved_tokens, status.remaining_tokens), (1234, 0, 248766))
        self.assertEqual(self.ledger.statuses(1)["10m"].used_tokens, 0)
        self.assertNotIn("api_key", self.path.read_text())

    def test_utc_rollover_keeps_unknown_reservations(self):
        self.ledger.record("1m", 500)
        reservation = self.ledger.reserve("1m", 1, 1000)
        self.now += timedelta(minutes=2)
        status = self.ledger.statuses(1)["1m"]
        self.assertEqual((status.utc_date, status.used_tokens, status.reserved_tokens), ("2026-09-19", 0, 1000))
        self.ledger.settle(reservation, 600)
        self.assertEqual(self.ledger.statuses(1)["1m"].used_tokens, 600)

    def test_clock_rollback_and_corrupt_file_fail_closed(self):
        self.ledger.record("1m", 5)
        self.now -= timedelta(days=1)
        with self.assertRaises(TranslationError):
            self.ledger.reserve("1m", 1, 100)
        self.path.write_text("{}", encoding="utf-8")
        with self.assertRaises(TranslationError):
            self.ledger.reserve("1m", 1, 100)

    def test_lock_prevents_competing_reservations(self):
        with self.ledger._locked():
            with self.assertRaises(TranslationError):
                QuotaLedger(self.path, now=lambda: self.now).reserve("1m", 1, 10)

    def test_write_failure_prevents_reservation(self):
        with patch("mq_localizer.quota.atomic_write_text", side_effect=OSError("full")):
            with self.assertRaises(TranslationError):
                self.ledger.reserve("1m", 1, 10)

    def test_settings_roundtrip_validation_and_fast_mode_exclusion(self):
        store = SettingsStore(Path(self.temp.name) / "settings.json")
        store.save(AppSettings(free_tokens_only=True, usage_tier=3, fast_mode=True))
        settings = store.load()
        self.assertTrue(settings.free_tokens_only)
        self.assertEqual(settings.usage_tier, 3)
        self.assertFalse(settings.fast_mode)
        store.path.write_text('{"free_tokens_only":"yes","usage_tier":true}', encoding="utf-8")
        settings = store.load()
        self.assertFalse(settings.free_tokens_only)
        self.assertEqual(settings.usage_tier, 1)


def response(text="翻訳", status="completed", usage=True):
    result = {"status": status, "model": "gpt-5.6-sol", "output_text": json.dumps({"translations": {
        "item_0000": {"fragments": {"fragment_0000": text}, "token_positions": {}}
    }})}
    if usage:
        result["usage"] = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    return result


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def request(self, method, url, headers, payload, timeout):
        self.calls.append((url.rsplit("/", 1)[-1], payload))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class ClientQuotaTests(LedgerTests):
    def client(self, transport, **kwargs):
        return OpenAIClient(transport=transport, quota_ledger=self.ledger, free_tokens_only=True, **kwargs)

    def call(self, client, model="gpt-5.6-sol", **kwargs):
        return client.translate_batch("sk-test-secret", model, [{"id": "a", "text": "Hello", "context": "title"}], "en_us", "ja_jp", **kwargs)

    def test_count_matches_actual_payload_and_usage_is_settled(self):
        transport = Transport({"input_tokens": 100}, response())
        result = self.call(self.client(transport))
        self.assertEqual(result, {"a": "翻訳"})
        self.assertEqual([call[0] for call in transport.calls], ["input_tokens", "responses"])
        counted, generated = [call[1] for call in transport.calls]
        self.assertEqual(counted, {k: generated[k] for k in ("model", "instructions", "input", "text")})
        self.assertEqual(generated["service_tier"], "default")
        self.assertGreater(generated["max_output_tokens"], 4096)
        status = self.ledger.statuses(1)["1m"]
        self.assertEqual((status.used_tokens, status.reserved_tokens), (120, 0))

    def test_insufficient_quota_never_generates(self):
        self.ledger.record("1m", 249_999)
        transport = Transport({"input_tokens": 100})
        with self.assertRaises(ComplimentaryQuotaExhausted):
            self.call(self.client(transport))
        self.assertEqual(len(transport.calls), 1)

    def test_timeout_is_not_retried_and_reservation_survives(self):
        transport = Transport({"input_tokens": 100}, OpenAIAPIError("timeout", retryable=True, kind="timeout"))
        with self.assertRaises(OpenAIAPIError):
            self.call(self.client(transport, max_retries=10))
        self.assertEqual(len(transport.calls), 2)
        self.assertGreater(self.ledger.statuses(1)["1m"].reserved_tokens, 100)

    def test_missing_or_invalid_usage_keeps_reservation(self):
        for bad in (None, {"input_tokens": 100, "output_tokens": 20, "total_tokens": 1}):
            with self.subTest(bad=bad):
                reply = response(usage=False)
                reply["usage"] = bad
                with self.assertRaises(TranslationError):
                    self.call(self.client(Transport({"input_tokens": 100}, reply)))
        self.assertGreater(self.ledger.statuses(1)["1m"].reserved_tokens, 0)

    def test_incomplete_and_invalid_json_still_count_usage(self):
        for reply in (response(status="incomplete"), {**response(), "output_text": "broken"}):
            with self.assertRaises(TranslationError):
                self.call(self.client(Transport({"input_tokens": 100}, reply)))
        self.assertEqual(self.ledger.statuses(1)["1m"].used_tokens, 240)

    def test_rejects_unknown_custom_priority_before_network(self):
        for model, kwargs, client_kwargs in (
            ("gpt-6-astra", {}, {}),
            ("gpt-5.6-sol", {"service_tier": "priority"}, {}),
            ("gpt-5.6-sol", {}, {"base_url": "https://example.com/v1"}),
        ):
            transport = Transport()
            with self.assertRaises(TranslationError):
                self.call(self.client(transport, **client_kwargs), model, **kwargs)
            self.assertEqual(transport.calls, [])

    def test_invalid_count_no_generation_or_reservation(self):
        for value in (True, -1, "100", None):
            with self.assertRaises(TranslationError):
                self.call(self.client(Transport({"input_tokens": value})))
        self.assertEqual(self.ledger.statuses(1)["1m"].reserved_tokens, 0)

    def test_normal_mode_records_known_usage_without_counting_request(self):
        transport = Transport(response())
        self.call(OpenAIClient(transport, quota_ledger=self.ledger))
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("max_output_tokens", transport.calls[0][1])
        self.assertEqual(self.ledger.statuses(1)["1m"].used_tokens, 120)


def exhausted():
    return ComplimentaryQuotaExhausted(QuotaStatus("1m", 1, 250_000, 249_999, 0, "2026-09-18"), 5000)


class BudgetClient:
    def __init__(self, successful=1, max_batch=1, failure=None):
        self.successful = successful
        self.max_batch = max_batch
        self.failure = failure
        self.calls = []

    def translate_batch(self, key, model, items, source, target, cancel=None):
        self.calls.append(items)
        if len(items) > self.max_batch or self.successful <= 0:
            raise self.failure or exhausted()
        self.successful -= 1
        return {item["id"]: "翻訳" for item in items}


class MemoryAdapter:
    def __init__(self):
        self.writes = []

    def write(self, project, resolved, output, **kwargs):
        self.writes.append((dict(resolved), kwargs))


class PartialTranslationTests(unittest.TestCase):
    def project(self, sources=("Hello", "Goodbye")):
        return TranslationProject("test", "test", Path("source"), Path("target"), "en_us", "ja_jp",
                                  [TranslationUnit(str(i), str(i), text) for i, text in enumerate(sources)])

    def run_job(self, client=None, confirm=None, project=None, guard=None, cancel=None):
        adapter = MemoryAdapter()
        self.adapter = adapter
        outcome = TranslationService(client or BudgetClient()).translate(
            project or self.project(), adapter, Path("target"), "test", "gpt-5.6-sol",
            GlossaryCatalog(), TranslationOptions(), confirm_partial=confirm,
            pre_write_guard=guard, cancel=cancel,
        )
        return outcome

    def test_shrink_then_prompt_and_write_only_completed_ids(self):
        states = []
        guards = []
        def confirm(state):
            states.append(state)
            self.assertEqual(self.adapter.writes, [])
            return True
        client = BudgetClient()
        outcome = self.run_job(client, confirm, guard=lambda: guards.append(True))
        self.assertEqual([len(items) for items in client.calls], [2, 1, 1])
        self.assertEqual((outcome.partial, outcome.written, outcome.completed), (True, True, 1))
        self.assertEqual(self.adapter.writes, [({"0": "翻訳"}, {"selected_unit_ids": frozenset({"0"})})])
        self.assertEqual(guards, [True])
        self.assertEqual(states[0].completed, 1)
        self.assertIn("次回最大", _partial_confirmation_text(states[0]))

    def test_decline_or_no_callback_does_not_write(self):
        for callback in (None, lambda _: False):
            outcome = self.run_job(confirm=callback)
            self.assertFalse(outcome.written)
            self.assertEqual(self.adapter.writes, [])
            self.assertIn("書き込んでいません", _format_translation_completion(outcome))

    def test_no_completed_units_no_prompt(self):
        def confirm(_):
            self.fail("No completed units must not prompt")
        outcome = self.run_job(BudgetClient(successful=0), confirm)
        self.assertEqual(outcome.completed, 0)
        self.assertEqual(self.adapter.writes, [])

    def test_incomplete_json_unit_is_not_saved(self):
        project = self.project(('Hello', '{"text":"One","hoverEvent":{"action":"show_text","contents":"Two"}}'))
        states = []
        def confirm(state):
            states.append(state)
            return True
        outcome = self.run_job(BudgetClient(successful=2), confirm, project)
        self.assertEqual(outcome.completed, 1)
        self.assertEqual(set(self.adapter.writes[0][0]), {"0"})

    def test_incomplete_visible_group_excluded(self):
        project = self.project(("Hello", "One", "Two"))
        project.metadata["terminology_groups"] = [("1", "2")]
        outcome = self.run_job(BudgetClient(successful=2), lambda _: True, project)
        self.assertEqual(outcome.completed, 1)
        self.assertEqual(set(self.adapter.writes[0][0]), {"0"})

    def test_normal_error_never_offers_partial_write(self):
        def confirm(_):
            self.fail("Normal error must not offer partial save")
        with self.assertRaises(TranslationError):
            self.run_job(BudgetClient(failure=TranslationError("unsafe")), confirm)
        self.assertEqual(self.adapter.writes, [])

    def test_changed_output_guard_blocks_partial_save(self):
        def guard():
            raise TranslationError("output changed")
        with self.assertRaises(TranslationError):
            self.run_job(confirm=lambda _: True, guard=guard)
        self.assertEqual(self.adapter.writes, [])

    def test_cancel_after_confirmation_blocks_partial_save(self):
        cancel = Event()
        def confirm(_):
            cancel.set()
            return True
        with self.assertRaises(CancelledError):
            self.run_job(confirm=confirm, cancel=cancel)
        self.assertEqual(self.adapter.writes, [])

    def test_all_complete_is_normal_success(self):
        outcome = self.run_job(BudgetClient(successful=2, max_batch=2))
        self.assertFalse(outcome.partial)
        self.assertEqual(outcome.completed, 2)
        self.assertEqual(len(self.adapter.writes), 1)

    def test_existing_and_code_only_count_as_completed(self):
        project = self.project(("Hello", "Goodbye", "&a&r"))
        project.existing["0"] = "既存訳"
        outcome = self.run_job(BudgetClient(successful=0), lambda _: True, project)
        self.assertEqual((outcome.reused, outcome.copied_without_translation), (1, 1))
        self.assertEqual(set(self.adapter.writes[0][0]), {"0", "2"})


if __name__ == "__main__":
    unittest.main()
