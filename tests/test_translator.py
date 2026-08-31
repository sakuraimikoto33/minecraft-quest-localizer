from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import (  # noqa: E402
    AdapterError,
    CancelledError,
    TranslationError,
    TranslationProject,
    TranslationUnit,
)
from mq_localizer.glossary import GlossaryCatalog, GlossaryEntry  # noqa: E402
from mq_localizer.openai_client import (  # noqa: E402
    OpenAIAPIError,
    OpenAIClient,
    OpenAIRefusalError,
    OpenAIResponseProtocolError,
)
from mq_localizer.translator import TranslationOptions, TranslationService  # noqa: E402


_PROTECTED_TOKEN = re.compile(r"__MQP_[0-9A-F]{4}__")


def _prefix_inside_protected_segment(text: str) -> str:
    matches = list(_PROTECTED_TOKEN.finditer(text))
    if not matches:
        return "訳:" + text
    cursor = 0
    for match in (*matches, None):
        end = match.start() if match is not None else len(text)
        for index in range(cursor, end):
            if text[index].isalnum():
                return text[:index] + "訳:" + text[index:]
        if match is not None:
            cursor = match.end()
    return text


class PrefixClient:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        return {
            item["id"]: _prefix_inside_protected_segment(item["text"])
            for item in items
        }


class ServiceTierRecordingClient(PrefixClient):
    def __init__(self) -> None:
        super().__init__()
        self.service_tiers: list[str | None] = []

    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
        *,
        service_tier: str | None = None,
    ) -> dict[str, str]:
        self.service_tiers.append(service_tier)
        return super().translate_batch(
            api_key,
            model,
            items,
            source_locale,
            target_locale,
            cancel,
        )


class StructuredSequenceTransport:
    def __init__(self, *responses: dict[str, Any] | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None,
        timeout: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("Unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class TamperingClient:
    def __init__(self) -> None:
        self.calls = 0

    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls += 1
        return {
            item["id"]: re.sub(r"__MQP_[0-9A-F]{4}__", "", item["text"])
            for item in items
        }


class RecoveringTamperingClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        if len(self.calls) == 1:
            return {
                item["id"]: _PROTECTED_TOKEN.sub("", item["text"])
                for item in items
            }
        return {
            item["id"]: _prefix_inside_protected_segment(item["text"])
            for item in items
        }


class RecoveringProtocolClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        del api_key, model, source_locale, target_locale, cancel
        self.calls.append(items)
        if len(self.calls) == 1:
            raise OpenAIResponseProtocolError(
                "token位置が完全な順列ではありません",
                items[-1]["id"],
            )
        return {
            item["id"]: _prefix_inside_protected_segment(item["text"])
            for item in items
        }


class StagedProtectionFailureClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        if len(self.calls) == 1:
            return {
                item["id"]: _PROTECTED_TOKEN.sub("", item["text"])
                for item in items
            }
        return {
            item["id"]: item["text"] + _PROTECTED_TOKEN.search(item["text"]).group(0)
            for item in items
        }


class CjkAdjacentTokenClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        return {
            item["id"]: "使用" + "".join(_PROTECTED_TOKEN.findall(item["text"])) + "を続行"
            for item in items
        }


class PlaceholderOnlyClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        return {
            item["id"]: "".join(re.findall(r"__MQP_[0-9A-F]{4}__", item["text"]))
            for item in items
        }


class FormattingGroupReorderClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        result: dict[str, str] = {}
        for item in items:
            styled = {
                binding["source_text"]: binding["token"]
                for binding in item.get("styled_bindings", [])
            }
            if not styled:
                translations = {
                    "Infuser": "注入器",
                    "Container": "容器",
                    "pipe": "パイプ",
                }
                result[item["id"]] = translations.get(item["text"], item["text"])
                continue
            term = item["term_bindings"][0]["token"]
            physical = [
                token
                for token in _PROTECTED_TOKEN.findall(item["text"])
                if token not in styled.values() and token != term
            ]
            result[item["id"]] = (
                "そのためには、"
                + styled["Container"]
                + "の上に"
                + styled["Infuser"]
                + "を設置する必要があります。"
                + styled["pipe"]
                + "を使って、それを"
                + physical[0]
                + term
                + "のContainer"
                + physical[1]
                + "に接続するのを忘れないでください。"
            )
        return result


class AtomicStyledTermsClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, Any]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        del api_key, model, source_locale, target_locale, cancel
        self.calls.append(items)  # type: ignore[arg-type]
        result: dict[str, str] = {}
        for item in items:
            bindings = {
                binding["source_term"]: binding["token"]
                for binding in item.get("term_bindings", [])
            }
            result[item["id"]] = (
                bindings["Element Binder"]
                + "を使ってこのインゴットを作成しましょう！"
                + bindings["Air"]
                + "を消費します！"
            )
        return result


class GroupedSemanticTermClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, Any]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        del api_key, model, source_locale, target_locale, cancel
        self.calls.append(items)  # type: ignore[arg-type]
        result: dict[str, str] = {}
        for item in items:
            bindings = {
                binding["source_term"]: binding["token"]
                for binding in item.get("term_bindings", [])
            }
            if r"Planets \& Dimensions" in bindings:
                result[item["id"]] = (
                    bindings[r"Planets \& Dimensions"]
                    + "の章で詳しい情報を確認できます！"
                )
            else:
                result[item["id"]] = (
                    bindings["Repair Pylon"]
                    + "と"
                    + bindings["Spirit Crucible"]
                    + "を組み合わせると、アイテムを修復できます。"
                )
        return result


class UnderGardenTitleClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        result: dict[str, str] = {}
        for item in items:
            tokens = _PROTECTED_TOKEN.findall(item["text"])
            result[item["id"]] = tokens[-1] + "を作成する"
        return result


class RecoveringUnderGardenTitleClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        result: dict[str, str] = {}
        for item in items:
            tokens = _PROTECTED_TOKEN.findall(item["text"])
            if len(self.calls) == 1:
                result[item["id"]] = "".join(tokens)
            else:
                result[item["id"]] = "".join(tokens) + "を作成する"
        return result


class UnderGardenDescriptionClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, Any]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        del api_key, model, source_locale, target_locale, cancel
        self.calls.append(items)  # type: ignore[arg-type]
        result: dict[str, str] = {}
        for item in items:
            bindings = {
                binding["source_term"]: binding["token"]
                for binding in item.get("term_bindings", [])
            }
            result[item["id"]] = (
                "Forgotten Blockと"
                + bindings["Carved Gloomgourd"]
                + "を使って"
                + bindings["Forgotten Minion"]
                + "を作成する。"
            )
        return result


class UnicodeFailureClient(PrefixClient):
    def __init__(self, *, persistent: bool = False) -> None:
        super().__init__()
        self.persistent = persistent

    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        del api_key, model, source_locale, target_locale, cancel
        self.calls.append(items)
        bad = self.persistent or len(self.calls) == 1
        return {
            item["id"]: (
                "変成の հնարみや実用用途に適した特別な一品。"
                if bad
                else "変成の手法や実用用途に適した特別な一品。"
            )
            for item in items
        }


class OverlappingTermClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        result: dict[str, str] = {}
        for item in items:
            bindings = {
                binding["source_term"]: binding["token"]
                for binding in item.get("term_bindings", [])
            }
            result[item["id"]] = (
                bindings["Rainbow Sword"]
                + "と"
                + bindings["Sword"]
                + "を使う"
            )
        return result


class GlossaryAffixingClient(PrefixClient):
    def __init__(self, persistent: bool = False) -> None:
        super().__init__()
        self.persistent = persistent

    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        corrupt = self.persistent or len(self.calls) == 1
        return {
            item["id"]: _PROTECTED_TOKEN.sub(
                lambda match: ("Super" if corrupt else "") + match.group(0),
                item["text"],
            )
            for item in items
        }


class SplitGlossaryBreakingClient(PrefixClient):
    def translate_batch(
        self,
        api_key: str,
        model: str,
        items: list[dict[str, str]],
        source_locale: str,
        target_locale: str,
        cancel: object = None,
    ) -> dict[str, str]:
        self.calls.append(items)
        return {
            item["id"]: item["text"] + (" Super" if item["id"].endswith("-p000") else "")
            for item in items
        }


class ExplodingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def translate_batch(self, *args: object, **kwargs: object) -> dict[str, str]:
        self.calls += 1
        raise self.error


class RecordingAdapter:
    def __init__(self, write_file: bool = False) -> None:
        self.calls: list[tuple[TranslationProject, dict[str, str], Path]] = []
        self.selected_calls: list[frozenset[str] | None] = []
        self.write_file = write_file

    def write(
        self,
        project: TranslationProject,
        translations: dict[str, str],
        output_path: Path,
        selected_unit_ids: frozenset[str] | None = None,
    ) -> None:
        self.calls.append((project, dict(translations), output_path))
        self.selected_calls.append(selected_unit_ids)
        if self.write_file:
            output_path.write_text(json.dumps(translations), encoding="utf-8")


class RejectingAdapter(RecordingAdapter):
    def validate_output(self, project: TranslationProject, output_path: Path) -> None:
        raise AdapterError("unsafe output")


class CancellingClient(PrefixClient):
    def __init__(self, cancel: Event) -> None:
        super().__init__()
        self.cancel = cancel

    def translate_batch(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        result = super().translate_batch(*args, **kwargs)
        self.cancel.set()
        return result


def _project(source: str, directory: Path, *, existing: dict[str, str] | None = None) -> TranslationProject:
    return TranslationProject(
        adapter_id="test",
        adapter_label="Test",
        source_path=directory / "source.snbt",
        default_output=directory / "ja_jp.snbt",
        source_locale="en_us",
        target_locale="ja_jp",
        units=[
            TranslationUnit(
                id="unit-1",
                key="quest.test.title",
                source=source,
                context="Quest title",
                source_path=str(directory / "source.snbt"),
                category="quest_title",
            )
        ],
        existing=existing or {},
    )


def _categorized_project(
    directory: Path,
    units: list[tuple[str, str, str, str]],
    *,
    existing: dict[str, str] | None = None,
) -> TranslationProject:
    """Build a small project whose category selection is part of the test input."""

    source_path = directory / "source.snbt"
    return TranslationProject(
        adapter_id="test",
        adapter_label="Test",
        source_path=source_path,
        default_output=directory / "ja_jp.snbt",
        source_locale="en_us",
        target_locale="ja_jp",
        units=[
            TranslationUnit(
                id=unit_id,
                key=key,
                source=source,
                context=category.replace("_", " "),
                source_path=str(source_path),
                ordinal=ordinal,
                category=category,
            )
            for ordinal, (unit_id, key, source, category) in enumerate(units)
        ],
        existing=dict(existing or {}),
    )


class TranslationServiceTests(unittest.TestCase):
    @staticmethod
    def mod_name_glossary(name: str = "Create") -> GlossaryCatalog:
        return GlossaryCatalog(
            entries={
                name: GlossaryEntry(
                    source=name,
                    target=name,
                    key="mod.display_name.create",
                    mod_id="create",
                    translated=False,
                    provenance="create.jar!/META-INF/mods.toml",
                )
            }
        )

    def test_single_unit_over_batch_character_limit_fails_before_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("A" * 600, root)
            client = PrefixClient()
            adapter = RecordingAdapter()

            with self.assertRaisesRegex(TranslationError, "最大文字数"):
                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(batch_char_limit=500),
                )

            self.assertEqual(client.calls, [])
            self.assertEqual(adapter.calls, [])

    def test_fast_mode_passes_priority_service_tier_to_translation_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Translate this", root)
            client = ServiceTierRecordingClient()
            adapter = RecordingAdapter()

            TranslationService(client, fast_mode=True).translate(
                project,
                adapter,
                project.default_output,
                "api-key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
            )

        self.assertEqual(client.service_tiers, ["priority"])
        self.assertEqual(adapter.calls[0][1]["unit-1"], "訳:Translate this")

    def test_batch_protocol_failure_retries_each_item_individually(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    ("unit-1", "quest.one.title", "First title", "quest_title"),
                    ("unit-2", "quest.two.title", "Second title", "quest_title"),
                ],
            )
            client = RecoveringProtocolClient()
            adapter = RecordingAdapter()
            progress: list[str] = []

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(batch_size=2),
                progress=lambda _done, _total, message: progress.append(message),
            )

            self.assertEqual([len(call) for call in client.calls], [2, 1, 1])
            self.assertEqual(
                adapter.calls[0][1],
                {"unit-1": "訳:First title", "unit-2": "訳:Second title"},
            )
            retry_message = next(
                message for message in progress if "1件ずつ再試行" in message
            )
            self.assertIn("unit-2", retry_message)
            self.assertIn("完全な順列", retry_message)

    def test_real_openai_client_protocol_failure_retries_with_fresh_item_aliases(self) -> None:
        def completed(payload: object) -> dict[str, Any]:
            return {
                "status": "completed",
                "output_text": json.dumps(payload, ensure_ascii=False),
            }

        def single_translation(text: str) -> dict[str, Any]:
            return {
                "translations": {
                    "item_0000": {
                        "fragments": {"fragment_0000": text},
                        "token_positions": {},
                    }
                }
            }

        transport = StructuredSequenceTransport(
            completed({"translations": {}}),
            completed(single_translation("最初の題名")),
            completed(single_translation("次の題名")),
        )
        client = OpenAIClient(transport=transport, max_retries=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    ("unit-1", "quest.one.title", "First title", "quest_title"),
                    ("unit-2", "quest.two.title", "Second title", "quest_title"),
                ],
            )
            adapter = RecordingAdapter()

            TranslationService(client, fast_mode=True).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(batch_size=2),
            )

        self.assertEqual(len(transport.calls), 3)
        provider_batches = [
            json.loads(call["payload"]["input"])["items"]
            for call in transport.calls
        ]
        self.assertEqual(
            [[item["id"] for item in batch] for batch in provider_batches],
            [["unit-1", "unit-2"], ["unit-1"], ["unit-2"]],
        )
        self.assertEqual(
            [[item["response_key"] for item in batch] for batch in provider_batches],
            [["item_0000", "item_0001"], ["item_0000"], ["item_0000"]],
        )
        self.assertTrue(
            all(call["payload"]["service_tier"] == "priority" for call in transport.calls)
        )
        self.assertEqual(
            adapter.calls[0][1],
            {"unit-1": "最初の題名", "unit-2": "次の題名"},
        )

    def test_timeout_retries_only_current_batch_and_keeps_completed_batches(self) -> None:
        def completed(text: str) -> dict[str, Any]:
            return {
                "status": "completed",
                "output_text": json.dumps(
                    {
                        "translations": {
                            "item_0000": {
                                "fragments": {"fragment_0000": text},
                                "token_positions": {},
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
            }

        transport = StructuredSequenceTransport(
            completed("最初の題名"),
            OpenAIAPIError(
                "request timed out",
                retryable=True,
                kind="timeout",
            ),
            completed("次の題名"),
            completed("最後の題名"),
        )
        client = OpenAIClient(transport=transport, max_retries=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    ("unit-1", "quest.one.title", "First title", "quest_title"),
                    ("unit-2", "quest.two.title", "Second title", "quest_title"),
                    ("unit-3", "quest.three.title", "Third title", "quest_title"),
                ],
            )
            adapter = RecordingAdapter()

            with patch("mq_localizer.openai_client.time.sleep") as sleep:
                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "sk-test",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(batch_size=1),
                )

        sent_unit_ids = [
            json.loads(call["payload"]["input"])["items"][0]["id"]
            for call in transport.calls
        ]
        self.assertEqual(
            sent_unit_ids,
            ["unit-1", "unit-2", "unit-2", "unit-3"],
        )
        sleep.assert_called_once_with(1.0)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(
            adapter.calls[0][1],
            {
                "unit-1": "最初の題名",
                "unit-2": "次の題名",
                "unit-3": "最後の題名",
            },
        )

    def test_exhausted_timeout_keeps_existing_output_unchanged(self) -> None:
        def completed(text: str) -> dict[str, Any]:
            return {
                "status": "completed",
                "output_text": json.dumps(
                    {
                        "translations": {
                            "item_0000": {
                                "fragments": {"fragment_0000": text},
                                "token_positions": {},
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
            }

        def timeout() -> OpenAIAPIError:
            return OpenAIAPIError(
                "request timed out",
                retryable=True,
                kind="timeout",
            )
        transport = StructuredSequenceTransport(
            completed("最初の題名"),
            timeout(),
            timeout(),
        )
        client = OpenAIClient(transport=transport, max_retries=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    ("unit-1", "quest.one.title", "First title", "quest_title"),
                    ("unit-2", "quest.two.title", "Second title", "quest_title"),
                    ("unit-3", "quest.three.title", "Third title", "quest_title"),
                ],
            )
            project.default_output.write_text("ORIGINAL", encoding="utf-8")
            adapter = RecordingAdapter(write_file=True)

            with (
                patch("mq_localizer.openai_client.time.sleep"),
                self.assertRaisesRegex(OpenAIAPIError, "timed out"),
            ):
                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "sk-test",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(batch_size=1),
                )

            self.assertEqual(project.default_output.read_text(encoding="utf-8"), "ORIGINAL")

        sent_unit_ids = [
            json.loads(call["payload"]["input"])["items"][0]["id"]
            for call in transport.calls
        ]
        self.assertEqual(sent_unit_ids, ["unit-1", "unit-2", "unit-2"])
        self.assertEqual(adapter.calls, [])

    def test_real_openai_client_refusal_does_not_retry_items_or_write(self) -> None:
        transport = StructuredSequenceTransport(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "refusal",
                                "refusal": "I cannot translate this request.",
                            }
                        ],
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            project = _categorized_project(
                root,
                [
                    ("unit-1", "quest.one.title", "First title", "quest_title"),
                    ("unit-2", "quest.two.title", "Second title", "quest_title"),
                ],
            )
            adapter = RecordingAdapter(write_file=True)

            with self.assertRaises(OpenAIRefusalError):
                TranslationService(
                    OpenAIClient(transport=transport, max_retries=0)
                ).translate(
                    project,
                    adapter,
                    output,
                    "sk-test",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(batch_size=2),
                )

            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "ORIGINAL")

    def test_existing_translation_that_changes_mod_name_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Use Create", root, existing={"unit-1": "クリエイトを使う"})
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.mod_name_glossary(),
                TranslationOptions(),
            )

            self.assertEqual(len(client.calls), 1)
            self.assertNotIn("Create", client.calls[0][0]["text"])
            self.assertIn("Create", adapter.calls[0][1]["unit-1"])
            self.assertNotIn("クリエイト", adapter.calls[0][1]["unit-1"])

    def test_provider_cannot_attach_ascii_text_to_a_mod_name_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Use Create machine.", root)
            client = GlossaryAffixingClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.mod_name_glossary(),
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(adapter.calls[0][1]["unit-1"], "Use Create machine.")

    def test_split_raw_json_mod_name_is_validated_after_assembly(self) -> None:
        source = json.dumps(
            {"text": "Modern ", "extra": [{"text": "Industrialization"}]},
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(source, root)
            client = SplitGlossaryBreakingClient()
            adapter = RecordingAdapter()
            progress: list[str] = []

            with self.assertRaisesRegex(
                TranslationError,
                "Mod名または公式用語",
            ) as raised:
                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "sk-test",
                    "gpt-test",
                    self.mod_name_glossary("Modern Industrialization"),
                    TranslationOptions(),
                    progress=lambda _done, _total, message: progress.append(message),
                )

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(adapter.calls, [])
        self.assertIn("対象: Quest title", str(raised.exception))
        self.assertIn("キー: quest.test.title", str(raised.exception))
        self.assertIn(str(project.source_path), str(raised.exception))
        self.assertIn("翻訳ファイルへ書き込んでいません", str(raised.exception))
        self.assertTrue(any("固有名詞確認" in message for message in progress))
        for retry_call in client.calls[1:]:
            self.assertIn("MANDATORY RETRY SAFETY", retry_call[0]["context"])

    def test_safe_official_existing_translation_with_formatting_is_reused(self) -> None:
        glossary = GlossaryCatalog(
            entries={
                "Copper Widget": GlossaryEntry(
                    source="Copper Widget",
                    target="銅の装置",
                    key="item.example.widget",
                    mod_id="example",
                    translated=True,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(
                "Use &bCopper Widget&r",
                root,
                existing={"unit-1": "使う &b銅の装置&r"},
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls[0][1]["unit-1"], "使う &b銅の装置&r")
        self.assertEqual((outcome.reused, outcome.translated), (1, 0))

    def test_resource_scoped_reordered_title_uses_one_official_term_binding(self) -> None:
        official = GlossaryEntry(
            source="Reactor Controller (Reinforced)",
            target="原子炉制御装置 (強化)",
            key="block.bigreactors.reinforced_reactorcontroller",
            mod_id="bigreactors",
            translated=True,
            provenance="bigreactors.jar!/assets/bigreactors/lang/en_us.json",
            target_state="translated",
        )
        glossary = GlossaryCatalog(
            entries={official.source: official},
            evidence={official.source: (official,)},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = TranslationProject(
                adapter_id="test",
                adapter_label="Test",
                source_path=root / "source.snbt",
                default_output=root / "ja_jp.json",
                source_locale="en_us",
                target_locale="ja_jp",
                units=[
                    TranslationUnit(
                        id="unit-1",
                        key="mq_localizer.task.reactor.title",
                        source="&#505050Reinforced Reactor Controller&r",
                        context="Task title",
                        source_path=str(root / "reactor.snbt"),
                        category="task_title",
                        resource_ids=(
                            "bigreactors:reinforced_reactorcontroller",
                        ),
                    )
                ],
            )
            client = PlaceholderOnlyClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("Reinforced", client.calls[0][0]["text"])
        self.assertNotIn("Controller", client.calls[0][0]["text"])
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "&#505050原子炉制御装置 (強化)&r",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_resource_scope_does_not_hide_whole_title_from_another_mod(self) -> None:
        collector = GlossaryEntry(
            "Collector",
            "Collector",
            "block.xycraft_machines.collector",
            "xycraft_machines",
            False,
            "xycraft.jar",
        )
        reprocessor = GlossaryEntry(
            "Reprocessor Collector",
            "再処理装置回収器",
            "block.bigreactors.reprocessorcollector",
            "bigreactors",
            True,
            "bigreactors.jar",
            "translated",
        )
        glossary = GlossaryCatalog(
            entries={collector.source: collector, reprocessor.source: reprocessor},
            evidence={
                collector.source: (collector,),
                reprocessor.source: (reprocessor,),
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = TranslationProject(
                adapter_id="test",
                adapter_label="Test",
                source_path=root / "source.snbt",
                default_output=root / "ja_jp.json",
                source_locale="en_us",
                target_locale="ja_jp",
                units=[
                    TranslationUnit(
                        id="unit-1",
                        key="mq_localizer.task.collector.title",
                        source="&#435548Collector&r",
                        context="Task title",
                        resource_ids=("bigreactors:reprocessorcollector",),
                    )
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertIn("Collector", client.calls[0][0]["text"])
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "&#435548訳:Collector&r",
        )

    def test_existing_term_moved_out_of_its_formatting_group_is_retranslated(self) -> None:
        glossary = GlossaryCatalog(
            entries={
                "Copper Widget": GlossaryEntry(
                    source="Copper Widget",
                    target="銅の装置",
                    key="item.example.widget",
                    mod_id="example",
                    translated=True,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(
                "Use &bCopper Widget&r",
                root,
                existing={"unit-1": "銅の装置を&b使う&r"},
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual((outcome.reused, outcome.translated), (0, 1))
        self.assertIn("&b銅の装置&r", adapter.calls[0][1]["unit-1"])

    @staticmethod
    def overlapping_term_glossary() -> GlossaryCatalog:
        return GlossaryCatalog(
            entries={
                "Rainbow Sword": GlossaryEntry(
                    source="Rainbow Sword",
                    target="虹の剣",
                    key="item.example.rainbow_sword",
                    mod_id="example",
                    translated=True,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                ),
                "Sword": GlossaryEntry(
                    source="Sword",
                    target="剣",
                    key="item.example.sword",
                    mod_id="example",
                    translated=True,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                ),
            }
        )

    @staticmethod
    def elementalcraft_term_glossary() -> GlossaryCatalog:
        return GlossaryCatalog(
            entries={
                source: GlossaryEntry(
                    source=source,
                    target=source,
                    key=key,
                    mod_id="elementalcraft",
                    translated=False,
                    provenance=(
                        "elementalcraft.jar!/assets/elementalcraft/lang/en_us.json"
                    ),
                )
                for source, key in (
                    ("Element Binder", "block.elementalcraft.binder"),
                    ("Air", "element.elementalcraft.air"),
                )
            }
        )

    @staticmethod
    def undergarden_title_glossary() -> GlossaryCatalog:
        return GlossaryCatalog(
            entries={
                "Create": GlossaryEntry(
                    source="Create",
                    target="Create",
                    key="mod.display_name.create",
                    mod_id="create",
                    translated=False,
                    provenance="create.jar!/META-INF/mods.toml",
                ),
                "Forgotten Minion": GlossaryEntry(
                    source="Forgotten Minion",
                    target="忘れ去られたミニオン",
                    key="entity.undergarden.minion",
                    mod_id="undergarden",
                    translated=True,
                    provenance="undergarden.jar!/assets/undergarden/lang/en_us.json",
                ),
            }
        )

    def test_styled_long_term_and_independent_short_term_translate_by_occurrence(self) -> None:
        source = "Use Rainbow &aSword&r and Sword."
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = OverlappingTermClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.overlapping_term_glossary(),
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "Rainbow &aSword&rと剣を使う",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_occurrence_aware_existing_layout_reuses_only_the_safe_assignment(self) -> None:
        source = "Use Rainbow &aSword&r and Sword."
        cases = (
            ("Rainbow &aSword&rと剣を使う。", True),
            ("Rainbow Swordと&a剣&rを使う。", False),
        )
        for candidate, should_reuse in cases:
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                client = OverlappingTermClient()
                adapter = RecordingAdapter()

                outcome = TranslationService(client).translate(
                    _project(source, root, existing={"unit-1": candidate}),
                    adapter,
                    root / "ja_jp.snbt",
                    "sk-test",
                    "gpt-test",
                    self.overlapping_term_glossary(),
                    TranslationOptions(),
                )

                self.assertEqual(not client.calls, should_reuse)
                self.assertEqual(outcome.reused, int(should_reuse))

    def test_reported_elementalcraft_styled_reorder_translates_without_retry(self) -> None:
        source = (
            "To do so you need an &5Infuser&r on top of a &3Container&r. "
            "Don't forget to connect it to the &3Extractor's Container&r "
            "with a &3pipe&r."
        )
        glossary = GlossaryCatalog(
            entries={
                "Extractor": GlossaryEntry(
                    source="Extractor",
                    target="Extractor",
                    key="block.elementalcraft.extractor",
                    mod_id="elementalcraft",
                    translated=False,
                    provenance="elementalcraft.jar!/assets/elementalcraft/lang/en_us.json",
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(source, root)
            client = FormattingGroupReorderClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
        )

        self.assertEqual(len(client.calls), 1, "自然な装飾group移動で個別再試行しない")
        root_item = next(
            item for item in client.calls[0] if item.get("styled_bindings")
        )
        provider_text = root_item["text"]
        self.assertEqual(
            len(_PROTECTED_TOKEN.findall(provider_text)),
            6,
            "plain styled groupはatom、term+plain groupは既存3 tokenを保つ",
        )
        self.assertEqual(
            {binding["source_text"] for binding in root_item["styled_bindings"]},
            {"Infuser", "Container", "pipe"},
        )
        self.assertNotIn("Infuser", provider_text)
        self.assertIn("'s Container", provider_text)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            (
                "そのためには、&3容器&rの上に&5注入器&rを設置する必要があります。"
                "&3パイプ&rを使って、それを&3ExtractorのContainer&rに接続するのを"
                "忘れないでください。"
            ),
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_reported_styled_proper_nouns_are_atomic_for_provider_and_restore(self) -> None:
        source = (
            "Use the &2Element Binder&r to create this ingot ! "
            "It consumes &eAir&r !"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "description-line-1",
                        "mq_localizer.quest.058c53606a481b5a.description.1",
                        source,
                        "quest_description",
                    )
                ],
            )
            client = AtomicStyledTermsClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.elementalcraft_term_glossary(),
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1, "atomic term groupは再試行不要")
        item = client.calls[0][0]
        tokens = _PROTECTED_TOKEN.findall(item["text"])
        self.assertEqual(len(tokens), 2)
        self.assertEqual(len(set(tokens)), 2)
        self.assertNotIn("Element Binder", item["text"])
        self.assertNotIn("Air", item["text"])
        self.assertEqual(
            item["term_bindings"],
            [
                {
                    "token": tokens[0],
                    "source_term": "Element Binder",
                    "approved_output": "Element Binder",
                },
                {
                    "token": tokens[1],
                    "source_term": "Air",
                    "approved_output": "Air",
                },
            ],
        )
        self.assertEqual(
            adapter.calls[0][1]["description-line-1"],
            "&2Element Binder&rを使ってこのインゴットを作成しましょう！"
            "&eAir&rを消費します！",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_term_split_by_formatting_is_one_provider_semantic_unit(self) -> None:
        source = (
            "When paired with a &dSpirit&r Crucible, the &aRepair Pylon&r "
            "allows items to be repaired."
        )
        glossary = GlossaryCatalog(
            entries={
                name: GlossaryEntry(
                    source=name,
                    target=name,
                    key=f"block.example.{index}",
                    mod_id="example",
                    translated=False,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                )
                for index, name in enumerate(("Spirit Crucible", "Repair Pylon"))
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "description",
                        "mq_localizer.quest.5605f9aacca63ad3.description.2",
                        source,
                        "quest_description",
                    )
                ],
            )
            client = GroupedSemanticTermClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1, "固有名詞断片の分離で再試行しない")
        item = client.calls[0][0]
        self.assertEqual(
            [binding["source_term"] for binding in item["term_bindings"]],
            ["Spirit Crucible", "Repair Pylon"],
        )
        self.assertEqual(len(_PROTECTED_TOKEN.findall(item["text"])), 2)
        self.assertNotIn("Spirit", item["text"])
        self.assertEqual(
            adapter.calls[0][1]["description"],
            "&aRepair Pylon&rと&dSpirit&r Crucibleを組み合わせると、"
            "アイテムを修復できます。",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_term_inside_partial_format_scope_is_not_collapsed(self) -> None:
        source = "&aPrefix Spirit&r Crucible end"
        glossary = GlossaryCatalog(
            entries={
                "Spirit Crucible": GlossaryEntry(
                    source="Spirit Crucible",
                    target="Spirit Crucible",
                    key="block.example.spirit_crucible",
                    mod_id="example",
                    translated=False,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "description",
                        "mq_localizer.quest.partial_scope.description.0",
                        source,
                        "quest_description",
                    )
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        item = client.calls[0][0]
        self.assertEqual(
            [binding["source_term"] for binding in item["term_bindings"]],
            ["Spirit", " Crucible"],
        )
        self.assertEqual(len(_PROTECTED_TOKEN.findall(item["text"])), 4)
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_grouped_term_inside_unclosed_format_scope_is_not_collapsed(self) -> None:
        source = r"Intro &aPrefix Planets \& Dimensions end"
        glossary = GlossaryCatalog().with_source_preserved_terms(
            [r"Planets \& Dimensions"]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "description",
                        "mq_localizer.quest.unclosed_scope.description.0",
                        source,
                        "quest_description",
                    )
                ],
            )
            client = PrefixClient()

            outcome = TranslationService(client).translate(
                project,
                RecordingAdapter(),
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        item = client.calls[0][0]
        self.assertEqual(
            [binding["source_term"] for binding in item["term_bindings"]],
            ["Planets ", " Dimensions"],
        )
        self.assertEqual(len(_PROTECTED_TOKEN.findall(item["text"])), 4)
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_project_name_split_by_escaped_ampersand_translates_without_retry(
        self,
    ) -> None:
        source = (
            r'More information can be found in the "Planets \& Dimensions" '
            "chapter!"
        )
        glossary = GlossaryCatalog().with_source_preserved_terms(
            [r"Planets \& Dimensions"]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "description",
                        "mq_localizer.quest.04b3aa8f91eb10f3.description.4",
                        source,
                        "quest_description",
                    )
                ],
            )
            client = GroupedSemanticTermClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )
            existing_value = (
                r"Planets \& Dimensionsの章で詳しい情報を確認できます！"
            )
            existing_project = _categorized_project(
                root,
                [
                    (
                        "description",
                        "mq_localizer.quest.04b3aa8f91eb10f3.description.4",
                        source,
                        "quest_description",
                    )
                ],
                existing={"description": existing_value},
            )
            reuse_client = GroupedSemanticTermClient()
            reuse_outcome = TranslationService(reuse_client).translate(
                existing_project,
                RecordingAdapter(),
                existing_project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )
            missing_body_project = _categorized_project(
                root,
                [
                    (
                        "description",
                        "mq_localizer.quest.04b3aa8f91eb10f3.description.4",
                        source,
                        "quest_description",
                    )
                ],
                existing={"description": r"Planets \& Dimensions"},
            )
            missing_body_client = GroupedSemanticTermClient()
            missing_body_outcome = TranslationService(missing_body_client).translate(
                missing_body_project,
                RecordingAdapter(),
                missing_body_project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1, "escaped ampersandで再試行しない")
        self.assertEqual(reuse_client.calls, [], "安全な既存訳を再翻訳しない")
        self.assertEqual((reuse_outcome.reused, reuse_outcome.translated), (1, 0))
        self.assertEqual(len(missing_body_client.calls), 1, "本文欠落の既存訳は再翻訳する")
        self.assertEqual(
            (missing_body_outcome.reused, missing_body_outcome.translated),
            (0, 1),
        )
        item = client.calls[0][0]
        self.assertEqual(
            item["term_bindings"],
            [
                {
                    "token": item["term_bindings"][0]["token"],
                    "source_term": r"Planets \& Dimensions",
                    "approved_output": r"Planets \& Dimensions",
                }
            ],
        )
        self.assertEqual(len(_PROTECTED_TOKEN.findall(item["text"])), 1)
        self.assertEqual(
            adapter.calls[0][1]["description"],
            r"Planets \& Dimensionsの章で詳しい情報を確認できます！",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_real_openai_client_restores_reported_atomic_styled_terms(self) -> None:
        source = (
            "Use the &2Element Binder&r to create this ingot ! "
            "It consumes &eAir&r !"
        )
        structured_output = {
            "translations": {
                "item_0000": {
                    "fragments": {
                        "fragment_0000": "",
                        "fragment_0001": "を使ってこのインゴットを作成しましょう！",
                        "fragment_0002": "を消費します！",
                    },
                    "token_positions": {
                        "token_0000": 0,
                        "token_0001": 1,
                    },
                }
            }
        }
        transport = StructuredSequenceTransport(
            {
                "status": "completed",
                "output_text": json.dumps(structured_output, ensure_ascii=False),
            }
        )
        client = OpenAIClient(transport=transport, max_retries=0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "description-line-1",
                        "mq_localizer.quest.058c53606a481b5a.description.1",
                        source,
                        "quest_description",
                    )
                ],
            )
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.elementalcraft_term_glossary(),
                TranslationOptions(),
            )

        self.assertEqual(len(transport.calls), 1, "安全な応答は個別再試行しない")
        payload = transport.calls[0]["payload"]
        assert payload is not None
        provider_items = json.loads(payload["input"])["items"]
        self.assertEqual(len(provider_items), 1)
        item = provider_items[0]
        self.assertEqual(
            item["source_fragments"],
            {
                "fragment_0000": "Use the ",
                "fragment_0001": " to create this ingot ! It consumes ",
                "fragment_0002": " !",
            },
        )
        self.assertEqual(
            item["source_token_order"],
            ["token_0000", "token_0001"],
        )
        self.assertEqual(
            item["term_bindings"],
            [
                {
                    "token_key": "token_0000",
                    "source_term": "Element Binder",
                    "approved_output": "Element Binder",
                },
                {
                    "token_key": "token_0001",
                    "source_term": "Air",
                    "approved_output": "Air",
                },
            ],
        )
        self.assertEqual(
            adapter.calls[0][1]["description-line-1"],
            "&2Element Binder&rを使ってこのインゴットを作成しましょう！"
            "&eAir&rを消費します！",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_real_openai_client_translates_three_reported_styled_bundles_in_one_batch(
        self,
    ) -> None:
        sources = {
            "reported-13dc": (
                "mq_localizer.quest.13dc2d43396579fb.description.1",
                "The &5Improved Element Binder&r works like a &3Binder&r and "
                "&3Infuser&r meaning it can do either binding or infusion at a faster "
                "rate. As for any other &2Instruments&r, the &5Improved Binder&r "
                "needs to be on top of a &3Container&r but not a &3Small Container&r.",
            ),
            "reported-552d": (
                "mq_localizer.quest.552d81ef1814a4dd.description.1",
                "The &5Binder&r is one of the most important &2Instrument&r. It "
                "channels elements to combine multiples items together. Be careful "
                "because the order is which you put the items is really important, "
                "you need to put them clockwise from the top. As for any other "
                "&2Instruments&r, the &5Binder&r needs to be on top of a "
                "&3Container&r, but not a &3Small Container&r.",
            ),
            "reported-45fc": (
                "mq_localizer.quest.45fc67ac6aad4eba.description.1",
                "The &2Reprocessor&r must be 3x3x7 to work perfectly. For further "
                "informations see the &e&lExtreme Book&r",
            ),
        }
        child_translations = {
            "Binder": "バインダー",
            "Infuser": "注入器",
            "Instruments": "装置",
            "Improved Binder": "改良型バインダー",
            "Container": "コンテナ",
            "Small Container": "小型コンテナ",
            "Instrument": "装置",
            "Reprocessor": "再処理装置",
            "Extreme Book": "究極の本",
        }
        parent_fragments = {
            "reported-13dc": (
                "",
                "は",
                "や",
                "と同様に機能し、より高速に結合と注入の両方を行えます。他の",
                "と同じく、",
                "ではなく",
                "の上に",
                "を設置する必要があります。",
            ),
            "reported-552d": (
                "",
                "は最も重要な",
                "の一つです。エレメントを導き、複数のアイテムを結合します。"
                "アイテムを置く順番は非常に重要なので注意してください。上から"
                "時計回りに配置する必要があります。他の",
                "と同じく、",
                "ではなく",
                "の上に",
                "を設置する必要があります。",
            ),
            "reported-45fc": (
                "",
                "が正しく動作するには、3x3x7の大きさでなければなりません。詳しくは",
                "を参照してください。",
            ),
        }

        class DynamicStyledBundleTransport:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []
                self.parent_token_orders: dict[str, tuple[str, ...]] = {}

            def request(
                self,
                method: str,
                url: str,
                headers: dict[str, str],
                payload: dict[str, Any] | None,
                timeout: int,
            ) -> dict[str, Any]:
                self.calls.append(
                    {
                        "method": method,
                        "url": url,
                        "headers": headers,
                        "payload": payload,
                        "timeout": timeout,
                    }
                )
                if payload is None:
                    raise AssertionError("A Responses payload is required")
                provider_items = json.loads(payload["input"])["items"]
                translations: dict[str, Any] = {}
                for item in provider_items:
                    item_id = item["id"]
                    response_key = item["response_key"]
                    fragment_keys = tuple(item["source_fragments"])
                    token_keys = tuple(item["source_token_order"])
                    styled = item.get("styled_bindings", [])
                    if not styled:
                        source_text = item["source_fragments"]["fragment_0000"]
                        translations[response_key] = {
                            "fragments": {
                                "fragment_0000": child_translations[source_text]
                            },
                            "token_positions": {},
                        }
                        continue

                    styled_keys = tuple(binding["token_key"] for binding in styled)
                    if item_id == "reported-13dc":
                        term_keys = tuple(
                            binding["token_key"]
                            for binding in item.get("term_bindings", [])
                        )
                        output_order = (
                            term_keys[0],
                            *styled_keys[:3],
                            styled_keys[5],
                            styled_keys[4],
                            styled_keys[3],
                        )
                    elif item_id == "reported-552d":
                        output_order = (
                            *styled_keys[:3],
                            styled_keys[5],
                            styled_keys[4],
                            styled_keys[3],
                        )
                    elif item_id == "reported-45fc":
                        output_order = styled_keys
                    else:
                        raise AssertionError(f"Unexpected parent item: {item_id}")

                    if set(output_order) != set(token_keys):
                        raise AssertionError(
                            f"Parent token plan does not cover {item_id}: "
                            f"{output_order!r} != {token_keys!r}"
                        )
                    fragments = parent_fragments[item_id]
                    if len(fragments) != len(fragment_keys):
                        raise AssertionError(
                            f"Fragment plan does not match {item_id}: "
                            f"{len(fragments)} != {len(fragment_keys)}"
                        )
                    self.parent_token_orders[item_id] = output_order
                    translations[response_key] = {
                        "fragments": dict(zip(fragment_keys, fragments, strict=True)),
                        "token_positions": {
                            token_key: position
                            for position, token_key in enumerate(output_order)
                        },
                    }
                return {
                    "status": "completed",
                    "output_text": json.dumps(
                        {"translations": translations}, ensure_ascii=False
                    ),
                }

        glossary = GlossaryCatalog(
            entries={
                "Improved Element Binder": GlossaryEntry(
                    source="Improved Element Binder",
                    target="Improved Element Binder",
                    key="block.elementalcraft.improved_element_binder",
                    mod_id="elementalcraft",
                    translated=False,
                    provenance=(
                        "elementalcraft.jar!/assets/elementalcraft/lang/en_us.json"
                    ),
                )
            }
        )
        transport = DynamicStyledBundleTransport()
        progress: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (unit_id, key, source, "quest_description")
                    for unit_id, (key, source) in sources.items()
                ],
            )
            adapter = RecordingAdapter()

            outcome = TranslationService(
                OpenAIClient(transport=transport, max_retries=0)
            ).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(batch_size=24),
                progress=lambda _done, _total, message: progress.append(message),
            )

        self.assertEqual(
            [unit.key for unit in project.units],
            [key for key, _source in sources.values()],
        )
        self.assertEqual(len(transport.calls), 1, "3 bundleは同一batchで完了する")
        payload = transport.calls[0]["payload"]
        assert payload is not None
        provider_items = json.loads(payload["input"])["items"]
        self.assertEqual(len(provider_items), 17)
        provider_by_id = {item["id"]: item for item in provider_items}
        expected_bundle_sizes = {
            "reported-13dc": 7,
            "reported-552d": 7,
            "reported-45fc": 3,
        }
        expected_styled_counts = {
            "reported-13dc": 6,
            "reported-552d": 6,
            "reported-45fc": 2,
        }
        for parent_id, expected_count in expected_styled_counts.items():
            parent = provider_by_id[parent_id]
            styled = parent["styled_bindings"]
            self.assertEqual(len(styled), expected_count)
            body_ids = {binding["body_item_id"] for binding in styled}
            self.assertEqual(len(body_ids) + 1, expected_bundle_sizes[parent_id])
            self.assertTrue(body_ids <= provider_by_id.keys())
            for binding in styled:
                body = provider_by_id[binding["body_item_id"]]
                self.assertEqual(
                    body["source_fragments"],
                    {"fragment_0000": binding["source_text"]},
                )

        term_bindings = provider_by_id["reported-13dc"]["term_bindings"]
        self.assertEqual(
            term_bindings,
            [
                {
                    "token_key": "token_0000",
                    "source_term": "Improved Element Binder",
                    "approved_output": "Improved Element Binder",
                }
            ],
        )
        self.assertNotIn(
            term_bindings[0]["token_key"],
            {
                binding["token_key"]
                for binding in provider_by_id["reported-13dc"]["styled_bindings"]
            },
        )
        self.assertEqual(
            {
                item_id: [int(token_key.removeprefix("token_")) for token_key in order]
                for item_id, order in transport.parent_token_orders.items()
            },
            {
                "reported-13dc": [0, 1, 2, 3, 6, 5, 4],
                "reported-552d": [0, 1, 2, 5, 4, 3],
                "reported-45fc": [0, 1],
            },
        )
        self.assertEqual(len(adapter.calls), 1, "adapter.writeは完了時の1回だけ")
        self.assertEqual(
            adapter.calls[0][1],
            {
                "reported-13dc": (
                    "&5Improved Element Binder&rは&3バインダー&rや&3注入器&rと同様に"
                    "機能し、より高速に結合と注入の両方を行えます。他の&2装置&rと"
                    "同じく、&3小型コンテナ&rではなく&3コンテナ&rの上に"
                    "&5改良型バインダー&rを設置する必要があります。"
                ),
                "reported-552d": (
                    "&5バインダー&rは最も重要な&2装置&rの一つです。エレメントを導き、"
                    "複数のアイテムを結合します。アイテムを置く順番は非常に重要なので"
                    "注意してください。上から時計回りに配置する必要があります。他の"
                    "&2装置&rと同じく、&3小型コンテナ&rではなく&3コンテナ&rの上に"
                    "&5バインダー&rを設置する必要があります。"
                ),
                "reported-45fc": (
                    "&2再処理装置&rが正しく動作するには、3x3x7の大きさでなければ"
                    "なりません。詳しくは&e&l究極の本&rを参照してください。"
                ),
            },
        )
        progress_text = "\n".join(progress)
        self.assertNotIn("再試行", progress_text)
        self.assertNotIn(
            "装飾コードで囲まれた本文または保護対象が別の装飾範囲へ移動しました",
            progress_text,
        )
        self.assertEqual((outcome.translated, outcome.reused), (3, 0))

    def test_styled_child_validation_failure_retries_the_complete_bundle(self) -> None:
        class RecoveringStyledBundleTransport:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def request(
                self,
                method: str,
                url: str,
                headers: dict[str, str],
                payload: dict[str, Any] | None,
                timeout: int,
            ) -> dict[str, Any]:
                self.calls.append(
                    {
                        "method": method,
                        "url": url,
                        "headers": headers,
                        "payload": payload,
                        "timeout": timeout,
                    }
                )
                if payload is None:
                    raise AssertionError("A Responses payload is required")
                provider_items = json.loads(payload["input"])["items"]
                translations: dict[str, Any] = {}
                for item in provider_items:
                    response_key = item["response_key"]
                    if item["id"].endswith("::styled::0000"):
                        translations[response_key] = {
                            "fragments": {
                                "fragment_0000": (
                                    "" if len(self.calls) == 1 else "再処理装置"
                                )
                            },
                            "token_positions": {},
                        }
                        continue
                    translations[response_key] = {
                        "fragments": {
                            "fragment_0000": "",
                            "fragment_0001": "は動作する必要があります。",
                        },
                        "token_positions": {"token_0000": 0},
                    }
                return {
                    "status": "completed",
                    "output_text": json.dumps(
                        {"translations": translations}, ensure_ascii=False
                    ),
                }

        transport = RecoveringStyledBundleTransport()
        progress: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "retry-styled-root",
                        "mq_localizer.quest.retry.description.1",
                        "The &2Reprocessor&r must work.",
                        "quest_description",
                    )
                ],
            )
            adapter = RecordingAdapter()

            outcome = TranslationService(
                OpenAIClient(transport=transport, max_retries=0)
            ).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
                progress=lambda _done, _total, message: progress.append(message),
            )

        self.assertEqual(len(transport.calls), 2)
        request_items = [
            json.loads(call["payload"]["input"])["items"]
            for call in transport.calls
        ]
        expected_bundle_ids = {
            "retry-styled-root::styled::0000",
            "retry-styled-root",
        }
        self.assertEqual(
            [{item["id"] for item in items} for items in request_items],
            [expected_bundle_ids, expected_bundle_ids],
            "初回失敗後も子だけではなく子＋親を同じrequestで再試行する",
        )
        for items in request_items:
            parent = next(item for item in items if item["id"] == "retry-styled-root")
            self.assertEqual(
                parent["styled_bindings"][0]["body_item_id"],
                "retry-styled-root::styled::0000",
            )
        retry_child = next(
            item
            for item in request_items[1]
            if item["id"] == "retry-styled-root::styled::0000"
        )
        self.assertIn("MANDATORY RETRY SAFETY", retry_child["context"])
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(
            adapter.calls[0][1]["retry-styled-root"],
            "&2再処理装置&rは動作する必要があります。",
        )
        self.assertTrue(any("まとめて個別再試行" in message for message in progress))
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_stacked_formatting_around_one_term_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Use &l&2Element Binder&r now.", root)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.elementalcraft_term_glossary(),
                TranslationOptions(),
            )

        item = client.calls[0][0]
        tokens = _PROTECTED_TOKEN.findall(item["text"])
        self.assertEqual(len(tokens), 1)
        self.assertEqual(item["term_bindings"][0]["token"], tokens[0])
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "訳:Use &l&2Element Binder&r now.",
        )

    def test_atomic_term_token_avoids_literal_mqp_token_collision(self) -> None:
        source = "Show __MQP_FFFF__ and &2Element Binder&r."
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(source, root)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.elementalcraft_term_glossary(),
                TranslationOptions(),
            )

        item = client.calls[0][0]
        tokens = _PROTECTED_TOKEN.findall(item["text"])
        self.assertEqual(len(tokens), 2)
        self.assertEqual(len(set(tokens)), 2)
        self.assertNotEqual(item["term_bindings"][0]["token"], "__MQP_FFFF__")
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "訳:Show __MQP_FFFF__ and &2Element Binder&r.",
        )

    def test_reported_undergarden_title_translates_create_as_a_verb(self) -> None:
        glossary = self.undergarden_title_glossary()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("&aCreate a Forgotten Minion", root)
            client = UnderGardenTitleClient()
            adapter = RecordingAdapter()
            progress: list[str] = []

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
                progress=lambda _done, _total, message: progress.append(message),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertIn("Create a ", client.calls[0][0]["text"])
        self.assertNotIn("Forgotten Minion", client.calls[0][0]["text"])
        self.assertEqual(
            len(_PROTECTED_TOKEN.findall(client.calls[0][0]["text"])),
            1,
            "先頭の &a は provider へ可動tokenとして渡さない",
        )
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "&a忘れ去られたミニオンを作成する",
        )
        self.assertFalse(any("再試行" in message for message in progress))
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_real_openai_client_normalizes_the_term_after_a_local_prefix(self) -> None:
        structured_output = {
            "translations": {
                "item_0000": {
                    "fragments": {
                        "fragment_0000": "",
                        "fragment_0001": "を作成する",
                    },
                    "token_positions": {"token_0000": 0},
                }
            }
        }
        transport = StructuredSequenceTransport(
            {
                "status": "completed",
                "output_text": json.dumps(structured_output, ensure_ascii=False),
            }
        )
        progress: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("&aCreate a Forgotten Minion", root)
            adapter = RecordingAdapter()

            outcome = TranslationService(
                OpenAIClient(transport=transport, max_retries=0)
            ).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.undergarden_title_glossary(),
                TranslationOptions(),
                progress=lambda _done, _total, message: progress.append(message),
            )

        provider_item = json.loads(transport.calls[0]["payload"]["input"])["items"][0]
        self.assertEqual(provider_item["source_token_order"], ["token_0000"])
        self.assertEqual(
            provider_item["source_fragments"],
            {"fragment_0000": "Create a ", "fragment_0001": ""},
        )
        self.assertEqual(
            provider_item["term_bindings"],
            [
                {
                    "token_key": "token_0000",
                    "source_term": "Forgotten Minion",
                    "approved_output": "忘れ去られたミニオン",
                }
            ],
        )
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "&a忘れ去られたミニオンを作成する",
        )
        self.assertFalse(any("再試行" in message for message in progress))
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_provider_cannot_inject_the_locally_fixed_prefix_token(self) -> None:
        class PrefixInjectionClient(PrefixClient):
            def translate_batch(
                self,
                api_key: str,
                model: str,
                items: list[dict[str, str]],
                source_locale: str,
                target_locale: str,
                cancel: object = None,
            ) -> dict[str, str]:
                del api_key, model, source_locale, target_locale, cancel
                self.calls.append(items)
                return {
                    item["id"]: "__MQP_0000__"
                    + _PROTECTED_TOKEN.search(item["text"]).group(0)
                    + "を作成する"
                    for item in items
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            project = _project("&aCreate a Forgotten Minion", root)
            client = PrefixInjectionClient()
            adapter = RecordingAdapter(write_file=True)

            with self.assertRaisesRegex(
                TranslationError,
                "余分: __MQP_0000__",
            ):
                TranslationService(client).translate(
                    project,
                    adapter,
                    output,
                    "sk-test",
                    "gpt-test",
                    self.undergarden_title_glossary(),
                    TranslationOptions(),
                )

            self.assertEqual(len(client.calls), 2)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "ORIGINAL")

    def test_stacked_unclosed_leading_formatting_is_restored_locally(self) -> None:
        class StackedPrefixClient(PrefixClient):
            def translate_batch(
                self,
                api_key: str,
                model: str,
                items: list[dict[str, str]],
                source_locale: str,
                target_locale: str,
                cancel: object = None,
            ) -> dict[str, str]:
                del api_key, model, source_locale, target_locale, cancel
                self.calls.append(items)
                return {item["id"]: "これを作る" for item in items}

        prefixes = ("&l&2", "&#12AB34", "&x&1&2&A&B&3&4")
        for prefix in prefixes:
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _project(prefix + "Build this", root)
                client = StackedPrefixClient()
                adapter = RecordingAdapter()

                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "sk-test",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

                self.assertEqual(client.calls[0][0]["text"], "Build this")
                self.assertEqual(
                    adapter.calls[0][1]["unit-1"],
                    prefix + "これを作る",
                )

    def test_reported_terminal_unclosed_style_is_appended_locally(self) -> None:
        class TerminalStyleClient(PrefixClient):
            def translate_batch(
                self,
                api_key: str,
                model: str,
                items: list[dict[str, str]],
                source_locale: str,
                target_locale: str,
                cancel: object = None,
            ) -> dict[str, str]:
                del api_key, model, source_locale, target_locale, cancel
                self.calls.append(items)
                translations = {
                    "unit-1": "これは機械工学の頂点へ至る最後の段階の一つです。",
                    "unit-1::styled::0000": "高ティア機械技術",
                }
                return {item["id"]: translations[item["id"]] for item in items}

        source = (
            "They represent one of the final steps before reaching the pinnacle "
            "of machine engineering. &7High-Tier Machine Technology"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(source, root)
            client = TerminalStyleClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1, "末尾装飾は個別再試行しない")
        items = {item["id"]: item for item in client.calls[0]}
        self.assertEqual(set(items), {"unit-1", "unit-1::styled::0000"})
        self.assertEqual(
            items["unit-1"]["text"],
            "They represent one of the final steps before reaching the pinnacle "
            "of machine engineering. ",
        )
        self.assertNotIn("styled_bindings", items["unit-1"])
        self.assertNotRegex(items["unit-1"]["text"], _PROTECTED_TOKEN)
        self.assertEqual(
            items["unit-1::styled::0000"]["text"],
            "High-Tier Machine Technology",
        )
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "これは機械工学の頂点へ至る最後の段階の一つです。"
            "&7高ティア機械技術",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_complex_unclosed_formatting_stays_provider_visible_and_strict(self) -> None:
        cases = (
            (
                "Text &aStyled &bSwitched",
                2,
                "訳:Text &aStyled &bSwitched",
            ),
            ("&aFirst &bSecond&r", 3, "&a訳:First &bSecond&r"),
            ("&aLine 1\nLine 2", 2, "&a訳:Line 1\nLine 2"),
        )
        for source, token_count, expected in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _project(source, root)
                client = PrefixClient()
                adapter = RecordingAdapter()

                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "sk-test",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

                self.assertEqual(
                    len(_PROTECTED_TOKEN.findall(client.calls[0][0]["text"])),
                    token_count,
                )
                self.assertEqual(adapter.calls[0][1]["unit-1"], expected)

    def test_reported_undergarden_body_omission_retry_explicitly_requires_the_verb(self) -> None:
        glossary = self.undergarden_title_glossary()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("&aCreate a Forgotten Minion", root)
            client = RecoveringUnderGardenTitleClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 2)
        retry_item = client.calls[1][0]
        retry_context = retry_item["context"]
        self.assertIn("Translate all meaningful fragment content", retry_context)
        self.assertIn("retry must contain translated ordinary text", retry_context)
        self.assertIn("omission or fusion of articles and determiners", retry_context)
        self.assertIn("OpenAI の応答から翻訳本文が失われました", retry_context)
        self.assertIn("Create a ", retry_item["text"])
        self.assertNotIn("Forgotten Minion", retry_item["text"])
        self.assertEqual(
            retry_item["term_bindings"],
            [
                {
                    "token": retry_item["term_bindings"][0]["token"],
                    "source_term": "Forgotten Minion",
                    "approved_output": "忘れ去られたミニオン",
                }
            ],
        )
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "&a忘れ去られたミニオンを作成する",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_undergarden_description_sends_meaningful_term_bindings(self) -> None:
        def entry(
            source: str,
            target: str,
            key: str,
            mod_id: str,
            translated: bool,
        ) -> GlossaryEntry:
            return GlossaryEntry(
                source=source,
                target=target,
                key=key,
                mod_id=mod_id,
                translated=translated,
                provenance=f"{mod_id}.jar!/assets/{mod_id}/lang/en_us.json",
            )

        glossary = GlossaryCatalog(
            entries={
                "Create": GlossaryEntry(
                    "Create",
                    "Create",
                    "mod.display_name.create",
                    "create",
                    False,
                    "create.jar!/META-INF/mods.toml",
                ),
                "Machines create items": entry(
                    "Machines create items",
                    "Machines create items",
                    "item.example.create_usage",
                    "example",
                    False,
                ),
                # This unrelated one-word Quark entity used to claim the
                # adjective in "Forgotten Block" and obscure its role.
                "Forgotten": entry(
                    "Forgotten",
                    "Forgotten",
                    "entity.quark.forgotten",
                    "quark",
                    False,
                ),
                "Forgotten Minion": entry(
                    "Forgotten Minion",
                    "忘れ去られたミニオン",
                    "entity.undergarden.minion",
                    "undergarden",
                    True,
                ),
                "Carved Gloomgourd": entry(
                    "Carved Gloomgourd",
                    "くり抜かれたグルームゴード",
                    "block.undergarden.carved_gloomgourd",
                    "undergarden",
                    True,
                ),
            }
        )
        source = (
            "Create a Forgotten Minion using a Forgotten Block and a "
            "Carved Gloomgourd."
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = UnderGardenDescriptionClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        item = client.calls[0][0]
        self.assertIn("Create a ", item["text"])
        self.assertIn("Forgotten Block", item["text"])
        self.assertNotIn("Forgotten Minion", item["text"])
        self.assertNotIn("Carved Gloomgourd", item["text"])
        self.assertEqual(
            item["term_bindings"],
            [
                {
                    "token": "__MQP_0000__",
                    "source_term": "Forgotten Minion",
                    "approved_output": "忘れ去られたミニオン",
                },
                {
                    "token": "__MQP_0001__",
                    "source_term": "Carved Gloomgourd",
                    "approved_output": "くり抜かれたグルームゴード",
                },
            ],
        )
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "Forgotten Blockとくり抜かれたグルームゴードを使って"
            "忘れ去られたミニオンを作成する。",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_new_foreign_script_retries_one_item_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(
                "Special pick for transmutation tricks and utility.",
                root,
            )
            client = UnicodeFailureClient()
            adapter = RecordingAdapter()
            progress: list[str] = []

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
                progress=lambda _done, _total, message: progress.append(message),
            )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(outcome.translated, 1)
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "変成の手法や実用用途に適した特別な一品。",
        )
        self.assertTrue(any("Armenian" in message for message in progress))

    def test_persistent_unicode_failure_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            client = UnicodeFailureClient(persistent=True)
            adapter = RecordingAdapter(write_file=True)

            with self.assertRaises(TranslationError) as raised:
                TranslationService(client).translate(
                    _project("Special pick for transmutation tricks.", root),
                    adapter,
                    output,
                    "sk-test",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

            message = str(raised.exception)
            self.assertIn("Armenian", message)
            self.assertIn("この処理では翻訳ファイルへ書き込んでいません", message)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "ORIGINAL")

    def test_unsafe_unicode_existing_translation_is_retranslated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(
                "Basic Access Port",
                root,
                existing={"unit-1": "基本アクセス\u200b\u200bポート"},
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
            )

        self.assertEqual((outcome.reused, outcome.translated), (0, 1))
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("\u200b", adapter.calls[0][1]["unit-1"])

    def test_mod_display_name_namespace_does_not_overlap_resource_id_protection(self) -> None:
        glossary = GlossaryCatalog(
            entries={
                "ElementalCraft": GlossaryEntry(
                    source="ElementalCraft",
                    target="ElementalCraft",
                    key="mod.display_name.elementalcraft",
                    mod_id="elementalcraft",
                    translated=False,
                    provenance="elementalcraft.jar!/META-INF/mods.toml",
                )
            }
        )
        source = "Any #elementalcraft:gems/fine_water"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(),
            )

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            adapter.calls[0][1]["unit-1"],
            "訳:Any #elementalcraft:gems/fine_water",
        )
        self.assertEqual((outcome.translated, outcome.reused), (1, 0))

    def test_unsafe_unselected_existing_mod_name_translation_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Selected quest", root)
            project.units.append(
                TranslationUnit(
                    id="unit-2",
                    key="task.test.title",
                    source="Use Create",
                    context="Task title",
                    category="task_title",
                )
            )
            project.existing["unit-2"] = "クリエイトを使う"
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.mod_name_glossary(),
                TranslationOptions(selected_categories=frozenset({"quest_title"})),
            )

            self.assertEqual(adapter.calls[0][1].keys(), {"unit-1"})
            self.assertEqual(adapter.selected_calls, [frozenset({"unit-1"})])
            self.assertEqual(outcome.preserved_unselected, 0)

    def test_mod_name_split_across_raw_json_text_parts_is_hidden_from_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = json.dumps(
                {"text": "Modern ", "extra": [{"text": "Industrialization"}]},
                separators=(",", ":"),
            )
            project = _project(source, root)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

            payload_texts = [item["text"] for call in client.calls for item in call]
            self.assertTrue(payload_texts)
            self.assertFalse(any("Modern" in text or "Industrialization" in text for text in payload_texts))
            rendered = json.loads(adapter.calls[0][1]["unit-1"])
            self.assertIn("Modern ", rendered["text"])
            self.assertIn("Industrialization", rendered["extra"][0]["text"])

    def test_raw_json_hover_is_independent_from_the_main_display_stream(self) -> None:
        source = json.dumps(
            {
                "text": "Modern ",
                "hoverEvent": {
                    "action": "show_text",
                    "contents": {"text": "Industrialization"},
                },
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertTrue(any("Modern" in text for text in payload_texts))
        self.assertTrue(any("Industrialization" in text for text in payload_texts))
        rendered = json.loads(adapter.calls[0][1]["unit-1"])
        self.assertEqual(rendered["text"], "訳:Modern ")
        self.assertEqual(
            rendered["hoverEvent"]["contents"]["text"],
            "訳:Industrialization",
        )

    def test_raw_json_hover_does_not_interrupt_a_main_extra_term(self) -> None:
        source = json.dumps(
            {
                "text": "",
                "extra": [
                    {
                        "text": "Modern ",
                        "hoverEvent": {
                            "action": "show_text",
                            "contents": {"text": "Tooltip"},
                        },
                    },
                    {"text": "Industrialization"},
                ],
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertFalse(any("Modern" in text for text in payload_texts))
        self.assertFalse(any("Industrialization" in text for text in payload_texts))
        self.assertTrue(any("Tooltip" in text for text in payload_texts))
        rendered = json.loads(adapter.calls[0][1]["unit-1"])
        self.assertEqual(rendered["extra"][0]["text"], "Modern ")
        self.assertEqual(rendered["extra"][1]["text"], "Industrialization")
        self.assertEqual(
            rendered["extra"][0]["hoverEvent"]["contents"]["text"],
            "訳:Tooltip",
        )

    def test_raw_json_with_arguments_are_independent_display_streams(self) -> None:
        source = json.dumps(
            {
                "translate": "example.message",
                "with": [
                    {"text": "Modern "},
                    {"text": "Industrialization"},
                ],
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertTrue(any("Modern" in text for text in payload_texts))
        self.assertTrue(any("Industrialization" in text for text in payload_texts))

    def test_raw_json_dynamic_components_are_visible_term_barriers(self) -> None:
        dynamic_components = (
            {"translate": "example.message"},
            {"score": {"name": "Player", "objective": "quest"}},
            {"selector": "@p"},
            {"keybind": "key.jump"},
            {"nbt": "Items[0].tag", "storage": "example:data"},
        )
        for dynamic in dynamic_components:
            with self.subTest(dynamic=dynamic), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = json.dumps(
                    [
                        {"text": "Modern "},
                        dynamic,
                        {"text": "Industrialization"},
                    ],
                    separators=(",", ":"),
                )
                client = PrefixClient()
                adapter = RecordingAdapter()

                TranslationService(client).translate(
                    _project(source, root),
                    adapter,
                    root / "ja_jp.snbt",
                    "sk-test",
                    "gpt-test",
                    self.mod_name_glossary("Modern Industrialization"),
                    TranslationOptions(),
                )

                payload_texts = [
                    item["text"] for call in client.calls for item in call
                ]
                self.assertTrue(any("Modern" in text for text in payload_texts))
                self.assertTrue(
                    any("Industrialization" in text for text in payload_texts)
                )
                rendered = json.loads(adapter.calls[0][1]["unit-1"])
                self.assertEqual(rendered[0]["text"], "訳:Modern ")
                self.assertEqual(rendered[2]["text"], "訳:Industrialization")

    def test_raw_json_unknown_array_values_are_term_barriers(self) -> None:
        source = json.dumps(
            [{"text": "Modern "}, 7, {"text": "Industrialization"}],
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertTrue(any("Modern" in text for text in payload_texts))
        self.assertTrue(any("Industrialization" in text for text in payload_texts))
        rendered = json.loads(adapter.calls[0][1]["unit-1"])
        self.assertEqual(rendered[0]["text"], "訳:Modern ")
        self.assertEqual(rendered[1], 7)
        self.assertEqual(rendered[2]["text"], "訳:Industrialization")

    def test_raw_json_hover_dynamic_components_split_literal_streams(self) -> None:
        source = json.dumps(
            {
                "text": "Main",
                "hoverEvent": {
                    "action": "show_text",
                    "contents": [
                        {"text": "Modern "},
                        {"translate": "example.middle"},
                        {"text": "Industrialization"},
                    ],
                },
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertTrue(any("Modern" in text for text in payload_texts))
        self.assertTrue(any("Industrialization" in text for text in payload_texts))
        rendered = json.loads(adapter.calls[0][1]["unit-1"])
        hover = rendered["hoverEvent"]["contents"]
        self.assertEqual(hover[0]["text"], "訳:Modern ")
        self.assertEqual(hover[1]["translate"], "example.middle")
        self.assertEqual(hover[2]["text"], "訳:Industrialization")

    def test_raw_json_empty_and_style_only_components_do_not_break_a_term(self) -> None:
        source = json.dumps(
            [
                {"text": "Modern "},
                {"bold": True},
                {"text": "", "italic": True},
                {"text": "Industrialization"},
            ],
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertFalse(any("Modern" in text for text in payload_texts))
        self.assertFalse(any("Industrialization" in text for text in payload_texts))
        rendered = json.loads(adapter.calls[0][1]["unit-1"])
        self.assertEqual(rendered[0]["text"], "Modern ")
        self.assertEqual(rendered[2]["text"], "")
        self.assertEqual(rendered[3]["text"], "Industrialization")

    def test_raw_json_literals_after_a_dynamic_barrier_join_following_siblings(self) -> None:
        source = json.dumps(
            [
                {"text": "Before "},
                {
                    "translate": "example.message",
                    "extra": [{"text": "Modern "}],
                },
                {"text": "Industrialization"},
            ],
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertTrue(any("Before" in text for text in payload_texts))
        self.assertFalse(any("Modern" in text for text in payload_texts))
        self.assertFalse(any("Industrialization" in text for text in payload_texts))
        rendered = json.loads(adapter.calls[0][1]["unit-1"])
        self.assertEqual(rendered[0]["text"], "訳:Before ")
        self.assertEqual(rendered[1]["extra"][0]["text"], "Modern ")
        self.assertEqual(rendered[2]["text"], "Industrialization")

    def test_raw_json_dynamic_separator_is_an_independent_display_stream(self) -> None:
        source = json.dumps(
            {
                "selector": "@a",
                "separator": {"text": "Modern "},
                "extra": [{"text": "Industrialization"}],
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                _project(source, root),
                adapter,
                root / "ja_jp.snbt",
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertTrue(any("Modern" in text for text in payload_texts))
        self.assertTrue(any("Industrialization" in text for text in payload_texts))
        rendered = json.loads(adapter.calls[0][1]["unit-1"])
        self.assertEqual(rendered["separator"]["text"], "訳:Modern ")
        self.assertEqual(rendered["extra"][0]["text"], "訳:Industrialization")

    def test_adapter_terminology_group_protects_a_term_split_across_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Modern ", root)
            project.units.append(
                TranslationUnit(
                    id="unit-2",
                    key="quest.test.description.part.2",
                    source="Industrialization",
                    context="Quest description",
                    category="quest_description",
                )
            )
            project.metadata["terminology_groups"] = [["unit-1", "unit-2"]]
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        payload_texts = [item["text"] for call in client.calls for item in call]
        self.assertFalse(any("Modern" in text for text in payload_texts))
        self.assertFalse(any("Industrialization" in text for text in payload_texts))
        self.assertEqual(adapter.calls[0][1]["unit-1"], "Modern ")
        self.assertEqual(adapter.calls[0][1]["unit-2"], "Industrialization")
        self.assertEqual((outcome.translated, outcome.reused), (2, 0))

    def test_unsafe_existing_translation_cannot_move_a_term_between_grouped_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(
                "Modern ",
                root,
                existing={
                    "unit-1": "Modern Indu",
                    "unit-2": "strialization",
                },
            )
            project.units.append(
                TranslationUnit(
                    id="unit-2",
                    key="quest.test.description.part.2",
                    source="Industrialization",
                    context="Quest description",
                    category="quest_description",
                )
            )
            project.metadata["terminology_groups"] = [["unit-1", "unit-2"]]
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                self.mod_name_glossary("Modern Industrialization"),
                TranslationOptions(),
            )

        self.assertEqual((outcome.translated, outcome.reused), (2, 0))
        self.assertEqual(adapter.calls[0][1]["unit-1"], "Modern ")
        self.assertEqual(adapter.calls[0][1]["unit-2"], "Industrialization")

    def test_invalid_adapter_terminology_groups_fail_before_api_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Use Create", root)
            project.metadata["terminology_groups"] = [["unit-1", "missing-unit"]]
            client = PrefixClient()
            adapter = RecordingAdapter()

            with self.assertRaisesRegex(TranslationError, "不明な翻訳単位"):
                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "sk-test",
                    "gpt-test",
                    self.mod_name_glossary(),
                    TranslationOptions(),
                )

        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls, [])

    def test_raw_json_component_arrays_translate_leaves_and_bracketed_prose_stays_plain(self) -> None:
        source = json.dumps(
            [
                {"text": "Root", "color": "gold"},
                [{"text": "Nested", "italic": True}],
                " tail",
            ],
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            array_client = PrefixClient()
            array_adapter = RecordingAdapter()
            TranslationService(array_client).translate(
                _project(source, root),
                array_adapter,
                root / "array-ja_jp.snbt",
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
            )

            prose_client = PrefixClient()
            prose_adapter = RecordingAdapter()
            TranslationService(prose_client).translate(
                _project("[Optional] objective", root),
                prose_adapter,
                root / "prose-ja_jp.snbt",
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
            )

        rendered = json.loads(array_adapter.calls[0][1]["unit-1"])
        self.assertEqual(rendered[0], {"text": "訳:Root", "color": "gold"})
        self.assertEqual(rendered[1][0], {"text": "訳:Nested", "italic": True})
        self.assertEqual(rendered[2], "訳: tail")
        self.assertEqual(prose_adapter.calls[0][1]["unit-1"], "訳:[Optional] objective")

    def test_unselected_titles_are_reference_terms_only_inside_selected_descriptions(self) -> None:
        title_specs = [
            (
                "book",
                "quest_book.main.title",
                "Impostor Syndrome",
                "quest_book_title",
            ),
            (
                "group",
                "chapter_group.main.title",
                "Main Journey",
                "chapter_group_title",
            ),
            (
                "chapter",
                "chapter.forest.title",
                "Dark Forest",
                "chapter_title",
            ),
            (
                "quest",
                "quest.path.title",
                "Forgotten Path",
                "quest_title",
            ),
            (
                "task",
                "task.crystal.title",
                "Crystal Submission",
                "task_title",
            ),
            (
                "reward",
                "reward.cache.title",
                "Hidden Cache",
                "reward_title",
            ),
            (
                "reward-table",
                "reward_table.lucky.title",
                "Lucky Pool",
                "reward_table_title",
            ),
            (
                "quest-link",
                "quest_link.shortcut.title",
                "Shortcut Gate",
                "quest_link_title",
            ),
        ]
        names = [source for _unit_id, _key, source, _category in title_specs]
        description = "Consult " + ", ".join(names[:-1]) + ", and " + names[-1] + "."

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    *title_specs,
                    (
                        "description",
                        "quest.path.quest_desc[0]",
                        description,
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_description"}),
                ),
            )

        provider_items = [item for call in client.calls for item in call]
        self.assertEqual([item["id"] for item in provider_items], ["description"])
        provider_item = provider_items[0]
        bindings = provider_item.get("term_bindings", [])
        self.assertEqual(
            [binding["source_term"] for binding in bindings],
            names,
        )
        self.assertEqual(
            [binding["approved_output"] for binding in bindings],
            names,
        )
        self.assertEqual(len(_PROTECTED_TOKEN.findall(provider_item["text"])), len(names))
        for name in names:
            self.assertNotIn(name, provider_item["text"])

        self.assertEqual(len(adapter.calls), 1)
        emitted = adapter.calls[0][1]
        self.assertEqual(set(emitted), {"description"})
        for name in names:
            self.assertIn(name, emitted["description"])
        self.assertEqual(adapter.selected_calls, [frozenset({"description"})])
        self.assertEqual((outcome.total, outcome.skipped_by_selection), (1, len(title_specs)))

    def test_unselected_title_source_wins_over_same_mod_official_translation(self) -> None:
        glossary = GlossaryCatalog(
            entries={
                "Forgotten Path": GlossaryEntry(
                    source="Forgotten Path",
                    target="忘れられた道",
                    key="item.example.forgotten_path",
                    mod_id="example",
                    translated=True,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    ("title", "quest.path.title", "Forgotten Path", "quest_title"),
                    (
                        "description",
                        "quest.path.quest_desc[0]",
                        "Use Forgotten Path now.",
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                glossary,
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_description"}),
                ),
            )

        item = client.calls[0][0]
        self.assertEqual(
            item["term_bindings"],
            [
                {
                    "token": item["term_bindings"][0]["token"],
                    "source_term": "Forgotten Path",
                    "approved_output": "Forgotten Path",
                }
            ],
        )
        rendered = adapter.calls[0][1]["description"]
        self.assertIn("Forgotten Path", rendered)
        self.assertNotIn("忘れられた道", rendered)
        self.assertEqual(adapter.selected_calls, [frozenset({"description"})])

    def test_unselected_title_is_also_protected_in_selected_prose_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "title",
                        "chapter.path.title",
                        "Impostor Syndrome",
                        "chapter_title",
                    ),
                    (
                        "subtitle",
                        "quest.path.subtitle",
                        "Welcome to Impostor Syndrome",
                        "quest_subtitle",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_subtitle"}),
                ),
            )

        item = client.calls[0][0]
        self.assertEqual(item["id"], "subtitle")
        self.assertEqual(
            [binding["source_term"] for binding in item["term_bindings"]],
            ["Impostor Syndrome"],
        )
        self.assertNotIn("Impostor Syndrome", item["text"])
        self.assertIn("Impostor Syndrome", adapter.calls[0][1]["subtitle"])
        self.assertEqual(set(adapter.calls[0][1]), {"subtitle"})

    def test_existing_description_must_preserve_an_unselected_title_reference(self) -> None:
        cases = (
            ("忘れられた道を読む。", True, 0),
            ("Forgotten Pathを読む。", False, 1),
        )
        for existing_description, expect_api, expected_reused in cases:
            with self.subTest(existing=existing_description), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _categorized_project(
                    root,
                    [
                        ("title", "quest.path.title", "Forgotten Path", "quest_title"),
                        (
                            "description",
                            "quest.path.quest_desc[0]",
                            "Read Forgotten Path.",
                            "quest_description",
                        ),
                    ],
                    existing={"description": existing_description},
                )
                client = PrefixClient()
                adapter = RecordingAdapter()

                outcome = TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "sk-test",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(
                        preserve_existing=True,
                        selected_categories=frozenset({"quest_description"}),
                    ),
                )

                self.assertEqual(bool(client.calls), expect_api)
                self.assertEqual(outcome.reused, expected_reused)
                rendered = adapter.calls[0][1]["description"]
                self.assertIn("Forgotten Path", rendered)
                self.assertEqual(adapter.selected_calls, [frozenset({"description"})])

    def test_selected_title_is_translated_normally_and_is_not_a_reference_term(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    ("title", "quest.path.title", "Forgotten Path", "quest_title"),
                    (
                        "description",
                        "quest.path.quest_desc[0]",
                        "Read Forgotten Path.",
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset(
                        {"quest_title", "quest_description"}
                    ),
                ),
            )

        provider_items = {
            item["id"]: item for call in client.calls for item in call
        }
        self.assertEqual(set(provider_items), {"title", "description"})
        self.assertIn("Forgotten Path", provider_items["description"]["text"])
        self.assertEqual(provider_items["description"].get("term_bindings", []), [])
        self.assertEqual(set(adapter.calls[0][1]), {"title", "description"})
        self.assertEqual(outcome.skipped_by_selection, 0)

    def test_same_spelling_selected_title_makes_an_unselected_reference_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "chapter-title",
                        "chapter.shared.title",
                        "Shared Name",
                        "chapter_title",
                    ),
                    (
                        "quest-title",
                        "quest.shared.title",
                        "Shared Name",
                        "quest_title",
                    ),
                    (
                        "description",
                        "quest.shared.quest_desc[0]",
                        "Read Shared Name.",
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset(
                        {"quest_title", "quest_description"}
                    ),
                ),
            )

        provider_items = {
            item["id"]: item for call in client.calls for item in call
        }
        self.assertEqual(set(provider_items), {"quest-title", "description"})
        self.assertIn("Shared Name", provider_items["description"]["text"])
        self.assertEqual(
            provider_items["description"].get("term_bindings", []),
            [],
        )
        self.assertEqual(
            set(adapter.calls[0][1]),
            {"quest-title", "description"},
        )

    def test_non_title_prose_never_becomes_a_project_reference_term(self) -> None:
        prose_names = [
            "Optional Route",
            "Ancient Warning",
            "Hover Legend",
            "Custom Lore",
            "Narrative Seed",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "quest-subtitle",
                        "quest.path.subtitle",
                        prose_names[0],
                        "quest_subtitle",
                    ),
                    (
                        "chapter-subtitle",
                        "chapter.path.subtitle",
                        prose_names[1],
                        "chapter_subtitle",
                    ),
                    (
                        "hover",
                        "image.path.hover",
                        prose_names[2],
                        "image_hover",
                    ),
                    ("other", "custom.lore", prose_names[3], "other"),
                    (
                        "description-seed",
                        "quest.seed.quest_desc[0]",
                        prose_names[4],
                        "quest_description",
                    ),
                    (
                        "description",
                        "quest.path.quest_desc[0]",
                        "Compare " + ", ".join(prose_names) + ".",
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_description"}),
                ),
            )

        provider_items = {
            item["id"]: item for call in client.calls for item in call
        }
        self.assertEqual(set(provider_items), {"description-seed", "description"})
        compared = provider_items["description"]
        self.assertEqual(compared.get("term_bindings", []), [])
        for prose in prose_names:
            self.assertIn(prose, compared["text"])
        self.assertEqual(
            adapter.selected_calls,
            [frozenset({"description-seed", "description"})],
        )

    def test_ambiguous_create_title_protects_product_use_but_not_an_imperative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    ("title", "quest.create.title", "Create", "quest_title"),
                    (
                        "imperative",
                        "quest.create.quest_desc[0]",
                        "Create a machine.",
                        "quest_description",
                    ),
                    (
                        "reference",
                        "quest.create.quest_desc[1]",
                        "Use Create for automation.",
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_description"}),
                ),
            )

        provider_items = {
            item["id"]: item for call in client.calls for item in call
        }
        imperative = provider_items["imperative"]
        reference = provider_items["reference"]
        self.assertIn("Create a machine", imperative["text"])
        self.assertEqual(imperative.get("term_bindings", []), [])
        self.assertNotIn("Create", reference["text"])
        self.assertEqual(
            [binding["source_term"] for binding in reference["term_bindings"]],
            ["Create"],
        )
        self.assertIn("Create", adapter.calls[0][1]["reference"])
        self.assertNotIn("title", adapter.calls[0][1])

    def test_decorated_unselected_title_protects_only_its_visible_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "title",
                        "quest.path.title",
                        "&6Forgotten Path&r",
                        "quest_title",
                    ),
                    (
                        "description",
                        "quest.path.quest_desc[0]",
                        "Read Forgotten Path today.",
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_description"}),
                ),
            )

        item = client.calls[0][0]
        self.assertEqual(
            [(binding["source_term"], binding["approved_output"]) for binding in item["term_bindings"]],
            [("Forgotten Path", "Forgotten Path")],
        )
        self.assertNotIn("Forgotten Path", item["text"])
        serialized_bindings = json.dumps(item["term_bindings"], ensure_ascii=False)
        self.assertNotIn("&6", serialized_bindings)
        self.assertNotIn("&r", serialized_bindings)
        rendered = adapter.calls[0][1]["description"]
        self.assertIn("Forgotten Path", rendered)
        self.assertNotIn("&6", rendered)
        self.assertNotIn("&r", rendered)

    def test_raw_json_unselected_title_uses_only_one_literal_primary_stream(self) -> None:
        title_component = json.dumps(
            {
                "text": "Arcane ",
                "extra": [{"text": "Horizons"}],
                "with": [{"text": "Argument Name"}],
                "hoverEvent": {
                    "action": "show_text",
                    "contents": {"text": "Tooltip Name"},
                },
            },
            separators=(",", ":"),
        )
        dynamic_component = json.dumps(
            {
                "translate": "example.dynamic",
                "extra": [{"text": "Literal Tail"}],
            },
            separators=(",", ":"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    ("title", "quest.arcane.title", title_component, "quest_title"),
                    (
                        "dynamic-title",
                        "quest.dynamic.title",
                        dynamic_component,
                        "quest_title",
                    ),
                    (
                        "description",
                        "quest.arcane.quest_desc[0]",
                        (
                            "Visit Arcane Horizons; ignore Argument Name, "
                            "Tooltip Name, and Literal Tail."
                        ),
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_description"}),
                ),
            )

        item = client.calls[0][0]
        self.assertEqual(
            [binding["source_term"] for binding in item["term_bindings"]],
            ["Arcane Horizons"],
        )
        self.assertNotIn("Arcane Horizons", item["text"])
        for auxiliary in ("Argument Name", "Tooltip Name", "Literal Tail"):
            self.assertIn(auxiliary, item["text"])

    def test_adapter_reference_group_combines_split_unselected_title_parts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "title-part-1",
                        "mq_localizer.quest.arcane.title.part.1",
                        "&6Arcane ",
                        "quest_title",
                    ),
                    (
                        "title-part-2",
                        "mq_localizer.quest.arcane.title.part.2",
                        "Horizons&r",
                        "quest_title",
                    ),
                    (
                        "description",
                        "mq_localizer.quest.arcane.description.1",
                        "Visit Arcane Horizons.",
                        "quest_description",
                    ),
                ],
            )
            project.metadata.update(
                {
                    "terminology_groups": [
                        ["title-part-1", "title-part-2"],
                    ],
                    "reference_term_groups": [
                        ["title-part-1", "title-part-2"],
                    ],
                }
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_description"}),
                ),
            )

        item = client.calls[0][0]
        self.assertEqual(
            [binding["source_term"] for binding in item["term_bindings"]],
            ["Arcane Horizons"],
        )
        self.assertNotIn("Arcane Horizons", item["text"])
        self.assertEqual(set(adapter.calls[0][1]), {"description"})

    def test_duplicate_project_titles_use_one_longest_match_per_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _categorized_project(
                root,
                [
                    (
                        "chapter-title",
                        "chapter.path.title",
                        "Ancient Forgotten Path",
                        "chapter_title",
                    ),
                    (
                        "quest-title-duplicate",
                        "quest.path.title",
                        "Ancient Forgotten Path",
                        "quest_title",
                    ),
                    (
                        "short-title",
                        "quest.short.title",
                        "Forgotten Path",
                        "quest_title",
                    ),
                    (
                        "description",
                        "quest.path.quest_desc[0]",
                        "Follow Ancient Forgotten Path, then Forgotten Path.",
                        "quest_description",
                    ),
                ],
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(
                    preserve_existing=False,
                    selected_categories=frozenset({"quest_description"}),
                ),
            )

        item = client.calls[0][0]
        bindings = item["term_bindings"]
        self.assertEqual(
            [binding["source_term"] for binding in bindings],
            ["Ancient Forgotten Path", "Forgotten Path"],
        )
        self.assertEqual(len(bindings), 2)
        self.assertEqual(len(_PROTECTED_TOKEN.findall(item["text"])), 2)
        self.assertNotIn("Ancient Forgotten Path", item["text"])
        self.assertNotIn("Forgotten Path", item["text"])
        rendered = adapter.calls[0][1]["description"]
        self.assertIn(
            "Follow Ancient Forgotten Path, then Forgotten Path.",
            rendered,
        )
        self.assertEqual(set(adapter.calls[0][1]), {"description"})

    def test_category_selection_excludes_existing_unselected_units_from_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Quest source", root)
            project.units.append(
                TranslationUnit(
                    id="unit-2",
                    key="task.test.title",
                    source="Task source",
                    context="Task title",
                    category="task_title",
                )
            )
            project.existing["unit-2"] = "既存タスク訳"
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "sk-test",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(selected_categories=frozenset({"quest_title"})),
            )

            self.assertEqual([[item["id"] for item in call] for call in client.calls], [["unit-1"]])
            self.assertEqual(
                adapter.calls[0][1],
                {"unit-1": "訳:Quest source"},
            )
            self.assertEqual(adapter.selected_calls, [frozenset({"unit-1"})])
            self.assertEqual(outcome.total, 1)
            self.assertEqual(outcome.translated, 1)
            self.assertEqual(outcome.skipped_by_selection, 1)
            self.assertEqual(outcome.preserved_unselected, 0)

    def test_empty_or_nonmatching_category_selection_fails_before_api_and_write(self) -> None:
        for categories in (frozenset(), frozenset({"task_title"})):
            with self.subTest(categories=categories), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _project("Quest source", root)
                client = PrefixClient()
                adapter = RecordingAdapter()
                with self.assertRaisesRegex(TranslationError, "選択した項目"):
                    TranslationService(client).translate(
                        project,
                        adapter,
                        project.default_output,
                        "sk-test",
                        "gpt-test",
                        GlossaryCatalog(),
                        TranslationOptions(selected_categories=categories),
                    )
                self.assertEqual(client.calls, [])
                self.assertEqual(adapter.calls, [])

    def test_raw_json_translates_only_text_component_leaves(self) -> None:
        component: dict[str, Any] = {
            "text": "Root",
            "color": "gold",
            "bold": True,
            "translate": "metadata.key.must.not.change",
            "extra": [
                {
                    "text": "Child §a",
                    "clickEvent": {"action": "run_command", "value": "/say hi"},
                    "hoverEvent": {
                        "action": "show_text",
                        "value": {"text": "Legacy hover"},
                    },
                },
                "Literal extra",
                {
                    "translate": "item.example.widget",
                    "with": [{"text": "Argument"}, "Raw argument"],
                },
                {"score": {"name": "Player", "objective": "quest"}},
                {
                    "text": "Item holder",
                    "hoverEvent": {
                        "action": "show_item",
                        "value": '{id:"minecraft:stone"}',
                    },
                },
            ],
            "hoverEvent": {
                "action": "show_text",
                "contents": {"text": "Hover text", "italic": False},
            },
        }
        source = json.dumps(component, ensure_ascii=False)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project(source, root)
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                root / "ja_jp.snbt",
                "api-key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(batch_size=50),
            )

        self.assertEqual(outcome.translated, 1)
        self.assertEqual(len(adapter.calls), 1)
        rendered = json.loads(adapter.calls[0][1]["unit-1"])
        self.assertEqual(rendered["text"], "訳:Root")
        self.assertEqual(rendered["color"], "gold")
        self.assertTrue(rendered["bold"])
        self.assertEqual(rendered["translate"], "metadata.key.must.not.change")
        self.assertEqual(rendered["extra"][0]["text"], "訳:Child §a")
        self.assertEqual(
            rendered["extra"][0]["clickEvent"],
            {"action": "run_command", "value": "/say hi"},
        )
        self.assertEqual(
            rendered["extra"][0]["hoverEvent"]["value"]["text"],
            "訳:Legacy hover",
        )
        self.assertEqual(rendered["extra"][1], "訳:Literal extra")
        self.assertEqual(rendered["extra"][2]["translate"], "item.example.widget")
        self.assertEqual(rendered["extra"][2]["with"][0]["text"], "訳:Argument")
        self.assertEqual(rendered["extra"][2]["with"][1], "訳:Raw argument")
        self.assertEqual(rendered["extra"][3], component["extra"][3])
        self.assertEqual(rendered["extra"][4]["text"], "訳:Item holder")
        self.assertEqual(rendered["extra"][4]["hoverEvent"], component["extra"][4]["hoverEvent"])
        self.assertEqual(rendered["hoverEvent"]["contents"]["text"], "訳:Hover text")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.calls[0]), 8)
        self.assertTrue(all("metadata.key.must.not.change" not in item["text"] for item in client.calls[0]))

    def test_protection_failure_never_reaches_writer_or_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            project = _project("§aTranslate this\nNext %s", root)
            client = TamperingClient()
            adapter = RecordingAdapter(write_file=True)

            with self.assertRaises(TranslationError):
                TranslationService(client).translate(
                    project,
                    adapter,
                    output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

            self.assertEqual(client.calls, 2, "one batch attempt and one isolated retry are expected")
            self.assertEqual(adapter.calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "ORIGINAL")

    def test_protection_failure_retries_one_item_with_reason_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("§aTranslate this %s", root)
            client = RecoveringTamperingClient()
            adapter = RecordingAdapter()
            progress: list[str] = []

            outcome = TranslationService(client).translate(
                project,
                adapter,
                project.default_output,
                "api-key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(),
                progress=lambda _done, _total, message: progress.append(message),
            )

            self.assertEqual(outcome.translated, 1)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(len(adapter.calls), 1)
            retry_item = client.calls[1][0]
            self.assertIn("MANDATORY RETRY SAFETY", retry_item["context"])
            self.assertIn("Previous validation failure:", retry_item["context"])
            self.assertIn("キー: quest.test.title", retry_item["context"])
            self.assertIn(str(project.source_path), retry_item["context"])
            retry_progress = next(message for message in progress if "個別再試行" in message)
            self.assertIn("Quest title", retry_progress)
            self.assertIn("キー: quest.test.title", retry_progress)
            self.assertIn(str(project.source_path), retry_progress)
            self.assertIn("1回目の理由:", retry_progress)

    def test_persistent_protection_failure_reports_item_both_reasons_and_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            project = _project("§aTranslate this %s", root)
            client = StagedProtectionFailureClient()
            adapter = RecordingAdapter(write_file=True)

            with self.assertRaises(TranslationError) as raised:
                TranslationService(client).translate(
                    project,
                    adapter,
                    output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

            message = str(raised.exception)
            self.assertIn("対象: Quest title", message)
            self.assertIn("キー: quest.test.title", message)
            self.assertIn(f"原文ファイル: {project.source_path}", message)
            self.assertIn("1回目の理由:", message)
            self.assertIn("欠落", message)
            self.assertIn("再試行後の理由:", message)
            self.assertIn("余分", message)
            self.assertIn("この処理では翻訳ファイルへ書き込んでいません", message)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "ORIGINAL")

    def test_cancel_after_protection_failure_stops_before_individual_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("§aTranslate this %s", root)
            client = RecoveringTamperingClient()
            adapter = RecordingAdapter()
            cancel = Event()

            def progress(_done: int, _total: int, message: str) -> None:
                if "個別再試行" in message:
                    cancel.set()

            with self.assertRaises(CancelledError):
                TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                    progress=progress,
                    cancel=cancel,
                )

            self.assertEqual(len(client.calls), 1)
            self.assertEqual(adapter.calls, [])

    def test_cjk_adjacent_protected_syntax_is_not_rescanned_after_assembly(self) -> None:
        sources = (
            "Use minecraft:stone now",
            json.dumps({"text": "Use minecraft:stone now"}, separators=(",", ":")),
        )
        for source in sources:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _project(source, root)
                client = CjkAdjacentTokenClient()
                adapter = RecordingAdapter()

                outcome = TranslationService(client).translate(
                    project,
                    adapter,
                    project.default_output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

                self.assertEqual(outcome.translated, 1)
                rendered = adapter.calls[0][1]["unit-1"]
                if source.startswith("{"):
                    rendered = json.loads(rendered)["text"]
                self.assertEqual(rendered, "使用minecraft:stoneを続行")
                self.assertEqual(len(client.calls), 1)

    def test_missing_translation_body_is_rejected_but_a_protected_mod_name_alone_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            client = PlaceholderOnlyClient()
            adapter = RecordingAdapter()

            with self.assertRaisesRegex(TranslationError, "翻訳本文"):
                TranslationService(client).translate(
                    _project("§aTranslate this", root),
                    adapter,
                    output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

            self.assertEqual(len(client.calls), 2)
            self.assertEqual(adapter.calls, [])

            mixed_client = PlaceholderOnlyClient()
            mixed_adapter = RecordingAdapter()
            with self.assertRaisesRegex(TranslationError, "翻訳本文"):
                TranslationService(mixed_client).translate(
                    _project("Translate Create", root),
                    mixed_adapter,
                    output,
                    "api-key",
                    "gpt-test",
                    self.mod_name_glossary(),
                    TranslationOptions(),
                )
            self.assertEqual(len(mixed_client.calls), 2)
            self.assertEqual(mixed_adapter.calls, [])

            numbered_client = PlaceholderOnlyClient()
            with self.assertRaisesRegex(TranslationError, "翻訳本文"):
                TranslationService(numbered_client).translate(
                    _project("Create 2", root),
                    RecordingAdapter(),
                    output,
                    "api-key",
                    "gpt-test",
                    self.mod_name_glossary(),
                    TranslationOptions(),
                )

            mod_client = PlaceholderOnlyClient()
            mod_adapter = RecordingAdapter()
            outcome = TranslationService(mod_client).translate(
                _project("Create", root),
                mod_adapter,
                output,
                "api-key",
                "gpt-test",
                self.mod_name_glossary(),
                TranslationOptions(),
            )

        self.assertEqual(outcome.translated, 1)
        self.assertEqual(mod_adapter.calls[0][1]["unit-1"], "Create")

    def test_english_determiner_may_disappear_before_a_content_placeholder(self) -> None:
        cases = (
            (
                "A Copper Widget",
                GlossaryCatalog(
                    entries={
                        "Copper Widget": GlossaryEntry(
                            source="Copper Widget",
                            target="銅の装置",
                            key="item.example.widget",
                            mod_id="example",
                            translated=True,
                            provenance="example.jar!/assets/example/lang/en_us.json",
                        )
                    }
                ),
                "銅の装置",
            ),
            ("The %s", GlossaryCatalog(), "%s"),
            (
                "Star",
                GlossaryCatalog(
                    entries={
                        "Star": GlossaryEntry(
                            source="Star",
                            target="★",
                            key="item.example.star",
                            mod_id="example",
                            translated=True,
                            provenance="example.jar!/assets/example/lang/en_us.json",
                        )
                    }
                ),
                "★",
            ),
        )
        for source, glossary, expected in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                client = PlaceholderOnlyClient()
                adapter = RecordingAdapter()

                outcome = TranslationService(client).translate(
                    _project(source, root),
                    adapter,
                    root / "ja_jp.snbt",
                    "api-key",
                    "gpt-test",
                    glossary,
                    TranslationOptions(),
                )

                self.assertEqual(len(client.calls), 1)
                self.assertEqual(adapter.calls[0][1]["unit-1"], expected)
                self.assertEqual(outcome.translated, 1)

    def test_determiner_omission_needs_content_and_an_english_source_locale(self) -> None:
        cases = (
            ("The §a", "en_us"),
            ("Use %s", "en_us"),
            ("The %s", "de_de"),
        )
        for source, source_locale in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _project(source, root)
                project.source_locale = source_locale
                client = PlaceholderOnlyClient()

                with self.assertRaisesRegex(TranslationError, "翻訳本文"):
                    TranslationService(client).translate(
                        project,
                        RecordingAdapter(),
                        root / "ja_jp.snbt",
                        "api-key",
                        "gpt-test",
                        GlossaryCatalog(),
                        TranslationOptions(),
                    )
                self.assertEqual(len(client.calls), 2)

        for source in ("Create %s", "Create a %s"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                client = PlaceholderOnlyClient()
                with self.assertRaisesRegex(TranslationError, "翻訳本文"):
                    TranslationService(client).translate(
                        _project(source, root),
                        RecordingAdapter(),
                        root / "ja_jp.snbt",
                        "api-key",
                        "gpt-test",
                        self.mod_name_glossary(),
                        TranslationOptions(),
                    )
                self.assertEqual(len(client.calls), 2)

    def test_bodyless_official_or_determiner_existing_translation_is_reused(self) -> None:
        star_glossary = GlossaryCatalog(
            entries={
                "Star": GlossaryEntry(
                    source="Star",
                    target="★",
                    key="item.example.star",
                    mod_id="example",
                    translated=True,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                )
            }
        )
        copper_glossary = GlossaryCatalog(
            entries={
                "Copper Widget": GlossaryEntry(
                    source="Copper Widget",
                    target="銅の装置",
                    key="item.example.widget",
                    mod_id="example",
                    translated=True,
                    provenance="example.jar!/assets/example/lang/en_us.json",
                )
            }
        )
        cases = (
            ("Star", "★", star_glossary),
            ("A Copper Widget", "銅の装置", copper_glossary),
            ("The %s", "%s", GlossaryCatalog()),
            (
                json.dumps({"text": "The %s"}, separators=(",", ":")),
                json.dumps({"text": "%s"}, separators=(",", ":")),
                GlossaryCatalog(),
            ),
        )
        for source, existing, glossary in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                client = PrefixClient()
                adapter = RecordingAdapter()

                outcome = TranslationService(client).translate(
                    _project(source, root, existing={"unit-1": existing}),
                    adapter,
                    root / "ja_jp.snbt",
                    "api-key",
                    "gpt-test",
                    glossary,
                    TranslationOptions(),
                )

                self.assertEqual(client.calls, [])
                self.assertEqual(adapter.calls[0][1]["unit-1"], existing)
                self.assertEqual((outcome.reused, outcome.translated), (1, 0))

    def test_structured_output_failure_never_reaches_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            client = ExplodingClient(TranslationError("invalid structured output"))
            adapter = RecordingAdapter(write_file=True)

            with self.assertRaisesRegex(TranslationError, "invalid structured output"):
                TranslationService(client).translate(
                    _project("Translate me", root),
                    adapter,
                    output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

            self.assertEqual(client.calls, 1)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "ORIGINAL")

    def test_persistent_protocol_failure_stops_after_individual_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ja_jp.snbt"
            output.write_text("ORIGINAL", encoding="utf-8")
            client = ExplodingClient(
                OpenAIResponseProtocolError("invalid token permutation", "unit-1")
            )
            adapter = RecordingAdapter(write_file=True)

            with self.assertRaisesRegex(TranslationError, "個別再試行後の理由"):
                TranslationService(client).translate(
                    _project("Translate me", root),
                    adapter,
                    output,
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

            self.assertEqual(client.calls, 2)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(output.read_text(encoding="utf-8"), "ORIGINAL")

    def test_malformed_raw_json_fails_before_api_and_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            with self.assertRaisesRegex(TranslationError, "raw JSON text") as caught:
                TranslationService(client).translate(
                    _project('{"text": "unterminated"', root),
                    adapter,
                    root / "ja_jp.snbt",
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )

            message = str(caught.exception)
            self.assertIn("対象: Quest title", message)
            self.assertIn("キー: quest.test.title", message)
            self.assertIn(str(root / "source.snbt"), message)
            self.assertIn("OpenAI APIは呼び出さず", message)

        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls, [])

    def test_existing_translation_and_code_only_unit_skip_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("Original text", root, existing={"unit-1": "既存訳"})
            project.units.append(
                TranslationUnit(
                    id="unit-2",
                    key="quest.code",
                    source="§a\n%1$s minecraft:stone",
                )
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                root / "ja_jp.snbt",
                "api-key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(preserve_existing=True),
            )

        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls[0][1], {"unit-1": "既存訳", "unit-2": "§a\n%1$s minecraft:stone"})
        self.assertEqual((outcome.reused, outcome.copied_without_translation, outcome.translated), (1, 1, 0))

    def test_bodyless_or_literal_dropping_existing_translation_is_retranslated(self) -> None:
        cases = (
            ("Important text §a\n", "§a\n"),
            ("Important text", "   "),
            ("Keep __MQP_0000__ here", "ここに保持"),
            ("§aHello", "こんにちは§a"),
            ("A\nB", "AB\n"),
            (r"Literal \&a text", r"訳文 a\&"),
        )
        for source, existing in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _project(source, root, existing={"unit-1": existing})
                client = PrefixClient()
                adapter = RecordingAdapter()

                outcome = TranslationService(client).translate(
                    project,
                    adapter,
                    root / "ja_jp.snbt",
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(preserve_existing=True),
                )

                self.assertEqual((outcome.reused, outcome.translated), (0, 1))
                self.assertEqual(len(client.calls), 1)
                self.assertNotEqual(adapter.calls[0][1]["unit-1"], existing)

    def test_nontranslatable_existing_value_must_match_source_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = _project("123 ---", root, existing={"unit-1": "456 ---"})
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                root / "ja_jp.snbt",
                "api-key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(preserve_existing=True),
            )

        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls[0][1]["unit-1"], "123 ---")
        self.assertEqual((outcome.reused, outcome.copied_without_translation), (0, 1))

    def test_existing_printf_and_template_may_reorder_within_one_layout_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = "{0}を%sで使う"
            project = _project(
                "Use %s and {0}",
                root,
                existing={"unit-1": existing},
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                root / "ja_jp.snbt",
                "api-key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(preserve_existing=True),
            )

        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls[0][1]["unit-1"], existing)
        self.assertEqual((outcome.reused, outcome.translated), (1, 0))

    def test_existing_url_translation_with_cjk_suffix_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = "https://example.invalid/pathを確認"
            project = _project(
                "Visit https://example.invalid/path now",
                root,
                existing={"unit-1": existing},
            )
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                root / "ja_jp.snbt",
                "api-key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(preserve_existing=True),
            )

        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls[0][1]["unit-1"], existing)
        self.assertEqual((outcome.reused, outcome.translated), (1, 0))

    def test_raw_json_existing_translation_validates_each_text_leaf(self) -> None:
        source = json.dumps(
            {"text": "Translate", "extra": ["123", {"text": "More"}]},
            separators=(",", ":"),
        )
        unsafe_candidates = (
            {"text": "翻訳", "extra": ["456", {"text": "続き"}]},
            {"text": "   ", "extra": ["123", {"text": "続き"}]},
        )
        for candidate in unsafe_candidates:
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _project(
                    source,
                    root,
                    existing={
                        "unit-1": json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
                    },
                )
                client = PrefixClient()
                adapter = RecordingAdapter()

                outcome = TranslationService(client).translate(
                    project,
                    adapter,
                    root / "ja_jp.snbt",
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(preserve_existing=True),
                )

                self.assertEqual((outcome.reused, outcome.translated), (0, 1))
                self.assertEqual(len(client.calls), 1)
                rendered = json.loads(adapter.calls[0][1]["unit-1"])
                self.assertEqual(rendered["extra"][0], "123")

    def test_raw_json_existing_translation_cannot_move_a_mod_name_style_boundary(self) -> None:
        source = json.dumps(
            {
                "text": "Modern ",
                "color": "red",
                "extra": [
                    {"text": "Industrialization", "color": "blue"},
                ],
            },
            separators=(",", ":"),
        )
        candidate = json.dumps(
            {
                "text": "Modern Indu",
                "color": "red",
                "extra": [
                    {"text": "strialization", "color": "blue"},
                ],
            },
            separators=(",", ":"),
        )
        glossary = GlossaryCatalog(
            entries={
                "Modern Industrialization": GlossaryEntry(
                    "Modern Industrialization",
                    "Modern Industrialization",
                    "mod.display_name.modern_industrialization",
                    "modern_industrialization",
                    False,
                    "modern-industrialization.jar",
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                _project(source, root, existing={"unit-1": candidate}),
                adapter,
                root / "ja_jp.snbt",
                "api-key",
                "gpt-test",
                glossary,
                TranslationOptions(preserve_existing=True),
            )

        self.assertEqual((outcome.reused, outcome.translated), (0, 1))
        self.assertEqual(len(client.calls), 1)

    def test_unsafe_existing_formatting_is_retranslated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = "§aOriginal\nNext %s"
            project = _project(source, root, existing={"unit-1": "既存訳だがコードがない"})
            client = PrefixClient()
            adapter = RecordingAdapter()

            outcome = TranslationService(client).translate(
                project,
                adapter,
                root / "ja_jp.snbt",
                "api-key",
                "gpt-test",
                GlossaryCatalog(),
                TranslationOptions(preserve_existing=True),
            )

        self.assertEqual((outcome.reused, outcome.translated), (0, 1))
        self.assertEqual(len(client.calls), 1)
        rendered = adapter.calls[0][1]["unit-1"]
        self.assertEqual(rendered.count("§a"), 1)
        self.assertEqual(rendered.count("\n"), 1)
        self.assertEqual(rendered.count("%s"), 1)

    def test_existing_raw_json_must_preserve_non_text_structure(self) -> None:
        source_component = {
            "text": "Open",
            "color": "gold",
            "bold": True,
            "insertion": 1,
            "clickEvent": {"action": "open_url", "value": "https://example.invalid"},
        }
        unsafe_candidates = (
            {
                **source_component,
                "text": "開く",
                "color": "red",
                "clickEvent": {
                    "action": "open_url",
                    "value": "https://changed.invalid",
                },
            },
            {**source_component, "text": "開く", "bold": 1},
            {**source_component, "text": "開く", "insertion": 1.0},
        )
        for changed_component in unsafe_candidates:
            with self.subTest(candidate=changed_component), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                project = _project(
                    json.dumps(source_component),
                    root,
                    existing={"unit-1": json.dumps(changed_component, ensure_ascii=False)},
                )
                client = PrefixClient()
                adapter = RecordingAdapter()

                outcome = TranslationService(client).translate(
                    project,
                    adapter,
                    root / "ja_jp.snbt",
                    "api-key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(preserve_existing=True),
                )

                self.assertEqual((outcome.reused, outcome.translated), (0, 1))
                rendered = json.loads(adapter.calls[0][1]["unit-1"])
                self.assertEqual(rendered["color"], "gold")
                self.assertIs(rendered["bold"], True)
                self.assertIs(type(rendered["insertion"]), int)
                self.assertEqual(rendered["clickEvent"], source_component["clickEvent"])

    def test_model_is_validated_before_api_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RecordingAdapter()
            with self.assertRaises(TranslationError):
                TranslationService(client).translate(
                    _project("Translate", root),
                    adapter,
                    root / "ja_jp.snbt",
                    "key",
                    "   ",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )
        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls, [])

    def test_output_preflight_fails_before_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = PrefixClient()
            adapter = RejectingAdapter()
            with self.assertRaisesRegex(AdapterError, "unsafe output"):
                TranslationService(client).translate(
                    _project("Translate", root),
                    adapter,
                    root / "unsafe.snbt",
                    "key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                )
        self.assertEqual(client.calls, [])
        self.assertEqual(adapter.calls, [])

    def test_cancel_after_api_response_stops_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cancel = Event()
            client = CancellingClient(cancel)
            adapter = RecordingAdapter()
            with self.assertRaises(CancelledError):
                TranslationService(client).translate(
                    _project("Translate", root),
                    adapter,
                    root / "ja_jp.snbt",
                    "key",
                    "gpt-test",
                    GlossaryCatalog(),
                    TranslationOptions(),
                    cancel=cancel,
                )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
