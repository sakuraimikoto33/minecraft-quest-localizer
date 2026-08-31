from __future__ import annotations

import http.client
import io
import json
import socket
import ssl
import sys
import threading
import unittest
import urllib.error
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mq_localizer.domain import CancelledError, TranslationError  # noqa: E402
import mq_localizer.openai_client as openai_client_module  # noqa: E402
from mq_localizer.openai_client import (  # noqa: E402
    DEFAULT_TRANSLATION_PROMPT,
    FAST_MODE_SERVICE_TIER,
    IMMUTABLE_TRANSLATION_PROTOCOL,
    OpenAIAPIError,
    OpenAIClient,
    OpenAIRefusalError,
    OpenAIResponseProtocolError,
    OpenAIRetryEvent,
    UrllibJsonTransport,
)
from mq_localizer.protection import TokenProtector  # noqa: E402
from mq_localizer.unicode_safety import JAPANESE_UNICODE_INSTRUCTIONS  # noqa: E402


class SequenceTransport:
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


def _output_text(payload: object) -> dict[str, Any]:
    return {
        "status": "completed",
        "output_text": json.dumps(payload, ensure_ascii=False),
    }


def _plain_translation_payload(**translations: str) -> dict[str, Any]:
    return {
        "translations": {
            f"item_{item_index:04d}": {
                "fragments": {"fragment_0000": text},
                "token_positions": {},
            }
            for item_index, (_item_id, text) in enumerate(translations.items())
        }
    }


class OpenAIModelTests(unittest.TestCase):
    def test_urllib_transport_rejects_oversized_response(self) -> None:
        class OversizedResponse:
            def __enter__(self) -> "OversizedResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return b"x" * size

        with (
            patch.object(openai_client_module, "_MAX_API_RESPONSE_BYTES", 32),
            patch.object(
                openai_client_module.urllib.request,
                "urlopen",
                return_value=OversizedResponse(),
            ),
        ):
            with self.assertRaisesRegex(OpenAIAPIError, "安全上限") as caught:
                UrllibJsonTransport().request(
                    "GET",
                    "https://unit.invalid/v1/models",
                    {},
                    None,
                    10,
                )
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.kind, "invalid_response")

    def test_urllib_transport_classifies_retryable_network_failures(self) -> None:
        failures = (
            (socket.timeout("direct timeout"), "timeout"),
            (
                urllib.error.URLError(socket.timeout("wrapped timeout")),
                "timeout",
            ),
            (
                urllib.error.URLError(ConnectionResetError("wrapped reset")),
                "connection",
            ),
            (ConnectionResetError("direct reset"), "connection"),
            (socket.gaierror("dns failure"), "connection"),
        )

        for failure, expected_kind in failures:
            with self.subTest(failure=type(failure).__name__, kind=expected_kind):
                with patch.object(
                    openai_client_module.urllib.request,
                    "urlopen",
                    side_effect=failure,
                ):
                    with self.assertRaises(OpenAIAPIError) as caught:
                        UrllibJsonTransport().request(
                            "GET",
                            "https://unit.invalid/v1/models",
                            {},
                            None,
                            10,
                        )

                self.assertTrue(caught.exception.retryable)
                self.assertEqual(caught.exception.kind, expected_kind)
                self.assertIsNone(caught.exception.status)

    def test_urllib_transport_does_not_retry_tls_certificate_failures(self) -> None:
        failures = (
            ssl.SSLCertVerificationError("certificate verify failed"),
            urllib.error.URLError(
                ssl.SSLCertVerificationError("wrapped certificate verify failed")
            ),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(
                    openai_client_module.urllib.request,
                    "urlopen",
                    side_effect=failure,
                ):
                    with self.assertRaises(OpenAIAPIError) as caught:
                        UrllibJsonTransport().request(
                            "GET",
                            "https://unit.invalid/v1/models",
                            {},
                            None,
                            10,
                        )

                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.kind, "certificate")

    def test_urllib_transport_classifies_incomplete_response_as_connection_error(self) -> None:
        class IncompleteResponse:
            def __enter__(self) -> "IncompleteResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _size: int = -1) -> bytes:
                raise http.client.IncompleteRead(b"partial", 100)

        with patch.object(
            openai_client_module.urllib.request,
            "urlopen",
            return_value=IncompleteResponse(),
        ):
            with self.assertRaises(OpenAIAPIError) as caught:
                UrllibJsonTransport().request(
                    "GET",
                    "https://unit.invalid/v1/models",
                    {},
                    None,
                    10,
                )

        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.kind, "connection")

    def test_http_error_body_failure_uses_the_known_status_retry_policy(self) -> None:
        class FailingBody:
            def __init__(self, error: BaseException) -> None:
                self.error = error

            def read(self, _size: int = -1) -> bytes:
                raise self.error

            def close(self) -> None:
                return None

        cases = (
            (400, socket.timeout("error body timed out"), "timeout", False),
            (503, socket.timeout("error body timed out"), "timeout", True),
            (
                400,
                http.client.IncompleteRead(b"partial", 100),
                "connection",
                False,
            ),
            (
                503,
                http.client.IncompleteRead(b"partial", 100),
                "connection",
                True,
            ),
        )

        for status, body_error, expected_kind, expected_retryable in cases:
            with self.subTest(status=status, kind=expected_kind):
                http_error = urllib.error.HTTPError(
                    "https://unit.invalid/v1/models",
                    status,
                    "HTTP error",
                    {"x-request-id": f"req-{status}-{expected_kind}"},
                    FailingBody(body_error),
                )
                with patch.object(
                    openai_client_module.urllib.request,
                    "urlopen",
                    side_effect=http_error,
                ):
                    with self.assertRaises(OpenAIAPIError) as caught:
                        UrllibJsonTransport().request(
                            "GET",
                            "https://unit.invalid/v1/models",
                            {},
                            None,
                            10,
                        )

                self.assertEqual(caught.exception.retryable, expected_retryable)
                self.assertEqual(caught.exception.kind, expected_kind)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(
                    caught.exception.request_id,
                    f"req-{status}-{expected_kind}",
                )

    def test_urllib_transport_malformed_json_is_not_retryable(self) -> None:
        malformed_responses = (b"not-json", b"[]", b'"text"')
        for response_bytes in malformed_responses:
            with self.subTest(response=response_bytes):
                with patch.object(
                    openai_client_module.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(response_bytes),
                ):
                    with self.assertRaises(OpenAIAPIError) as caught:
                        UrllibJsonTransport().request(
                            "GET",
                            "https://unit.invalid/v1/models",
                            {},
                            None,
                            10,
                        )

                self.assertFalse(caught.exception.retryable)
                self.assertEqual(caught.exception.kind, "invalid_response")

    def test_models_are_filtered_sorted_and_request_is_authenticated(self) -> None:
        transport = SequenceTransport(
            {
                "data": [
                    {"id": "gpt-old", "created": 10, "owned_by": "openai"},
                    {"id": "text-embedding-3-large", "created": 999},
                    {"id": "gpt-audio-1", "created": 998},
                    {"id": "o3", "created": 20, "owned_by": "system"},
                    {"id": "gpt-z", "created": 20},
                    {"id": "ft:gpt-4o-mini:org:quest-localizer:abc", "created": 15},
                    {"id": "unrelated-model", "created": 1000},
                    {"created": 1001},
                    "invalid",
                ]
            }
        )
        client = OpenAIClient(transport=transport, base_url="https://unit.invalid/v1/", timeout=7)

        models = client.list_models("  secret-key  ")

        self.assertEqual(
            [model.id for model in models],
            ["o3", "gpt-z", "ft:gpt-4o-mini:org:quest-localizer:abc", "gpt-old"],
        )
        self.assertEqual(models[0].owned_by, "system")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://unit.invalid/v1/models")
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-key")
        self.assertIsNone(call["payload"])
        self.assertEqual(call["timeout"], 7)

    def test_future_o_series_models_are_kept_but_non_text_variants_are_filtered(self) -> None:
        transport = SequenceTransport(
            {
                "data": [
                    {"id": "o5", "created": 5},
                    {"id": "o5-mini", "created": 4},
                    {"id": "o6-2027-01-01", "created": 3},
                    {"id": "o6-audio-preview", "created": 9},
                    {"id": "o7-image", "created": 8},
                    {"id": "o8-search-preview", "created": 7},
                    {"id": "omni-moderation-latest", "created": 6},
                    {"id": "o-not-a-model", "created": 2},
                ]
            }
        )

        models = OpenAIClient(transport=transport).list_models("key")

        self.assertEqual([model.id for model in models], ["o5", "o5-mini", "o6-2027-01-01"])

    def test_malformed_models_payload_raises_api_error(self) -> None:
        cases: list[dict[str, Any]] = [
            {},
            {"data": None},
            {"data": {}},
            {"data": [{"id": "gpt-test", "created": "not-an-integer"}]},
            {"data": [{"id": "o5", "created": True}]},
        ]

        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(OpenAIAPIError):
                    OpenAIClient(transport=SequenceTransport(response)).list_models("key")

    def test_empty_key_and_pre_cancel_do_not_call_transport(self) -> None:
        transport = SequenceTransport({"data": []})
        client = OpenAIClient(transport=transport)

        with self.assertRaises(OpenAIAPIError):
            client.list_models("   ")
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(CancelledError):
            client.list_models("key", cancelled)
        self.assertEqual(transport.calls, [])

    def test_active_blocking_transport_is_abandoned_on_cancel(self) -> None:
        cancel = Event()
        release = Event()
        finished = Event()
        observed_daemon: list[bool] = []

        class BlockingTransport:
            def request(
                self,
                _method: str,
                _url: str,
                _headers: dict[str, str],
                _payload: dict[str, Any] | None,
                _timeout: int,
            ) -> dict[str, Any]:
                observed_daemon.append(threading.current_thread().daemon)
                cancel.set()
                release.wait(5)
                finished.set()
                return {"data": []}

        try:
            with self.assertRaises(CancelledError):
                OpenAIClient(transport=BlockingTransport()).list_models("key", cancel)
            self.assertEqual(observed_daemon, [True])
            self.assertFalse(finished.is_set(), "caller must not wait for the active transport")
        finally:
            release.set()
            finished.wait(1)


class OpenAIResponseTests(unittest.TestCase):
    def test_responses_payload_uses_strict_schema_and_nested_output(self) -> None:
        translated = {
            "translations": {
                "item_0000": {
                    "fragments": {"fragment_0000": "翻訳", "fragment_0001": "1"},
                    "token_positions": {"token_0000": 0},
                },
                "item_0001": {
                    "fragments": {"fragment_0000": "翻訳2"},
                    "token_positions": {},
                },
            }
        }
        raw = json.dumps(translated, ensure_ascii=False)
        transport = SequenceTransport(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": raw[: len(raw) // 2]},
                            {"type": "output_text", "text": raw[len(raw) // 2 :]},
                        ],
                    }
                ]
            }
        )
        client = OpenAIClient(transport=transport)
        items = [
            {"id": "u1", "text": "One __MQP_0000__", "context": "title"},
            {"id": "u2", "text": "Two", "context": "description"},
        ]

        result = client.translate_batch("key", "  gpt-test  ", items, "en_us", "ja_jp")

        self.assertEqual(
            result,
            {"u1": "翻訳__MQP_0000__1", "u2": "翻訳2"},
        )
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.openai.com/v1/responses")
        payload = call["payload"]
        assert payload is not None
        self.assertEqual(payload["model"], "gpt-test")
        self.assertFalse(payload["store"])
        self.assertNotIn("service_tier", payload)
        provider_input = json.loads(payload["input"])
        self.assertEqual(
            provider_input,
            {
                "items": [
                    {
                        "id": "u1",
                        "response_key": "item_0000",
                        "source_fragments": {
                            "fragment_0000": "One ",
                            "fragment_0001": "",
                        },
                        "source_token_order": ["token_0000"],
                        "context": "title",
                    },
                    {
                        "id": "u2",
                        "response_key": "item_0001",
                        "source_fragments": {"fragment_0000": "Two"},
                        "source_token_order": [],
                        "context": "description",
                    },
                ]
            },
        )
        self.assertNotIn("__MQP_0000__", payload["input"])
        schema_format = payload["text"]["format"]
        self.assertEqual(schema_format["type"], "json_schema")
        self.assertTrue(schema_format["strict"])

        schema = schema_format["schema"]
        translations_schema = schema["properties"]["translations"]
        self.assertEqual(
            list(translations_schema["properties"]),
            ["item_0000", "item_0001"],
        )
        self.assertEqual(
            translations_schema["required"],
            ["item_0000", "item_0001"],
        )
        u1_schema = translations_schema["properties"]["item_0000"]
        self.assertEqual(
            u1_schema["properties"]["fragments"]["required"],
            ["fragment_0000", "fragment_0001"],
        )
        self.assertEqual(
            u1_schema["properties"]["token_positions"]["required"],
            ["token_0000"],
        )
        self.assertEqual(
            u1_schema["properties"]["token_positions"]["properties"]["token_0000"],
            {"type": "integer"},
        )
        u2_schema = translations_schema["properties"]["item_0001"]
        self.assertEqual(
            u2_schema["properties"]["fragments"]["required"],
            ["fragment_0000"],
        )
        self.assertEqual(
            u2_schema["properties"]["token_positions"]["required"],
            [],
        )
        self.assertIn("__MQP_0000__", payload["instructions"])
        self.assertEqual(
            payload["instructions"],
            DEFAULT_TRANSLATION_PROMPT.replace("{source_locale}", "en_us").replace(
                "{target_locale}", "ja_jp"
            )
            + "\n\n"
            + IMMUTABLE_TRANSLATION_PROTOCOL
            + "\n\n"
            + JAPANESE_UNICODE_INSTRUCTIONS,
        )

    def test_fast_mode_adds_priority_service_tier_only_to_responses_request(self) -> None:
        transport = SequenceTransport(
            _output_text(_plain_translation_payload(u1="翻訳"))
        )
        client = OpenAIClient(transport=transport)

        result = client.translate_batch(
            "key",
            "gpt-test",
            [{"id": "u1", "text": "source"}],
            "en_us",
            "ja_jp",
            service_tier=FAST_MODE_SERVICE_TIER,
        )

        self.assertEqual(result, {"u1": "翻訳"})
        self.assertEqual(transport.calls[0]["payload"]["service_tier"], "priority")

    def test_unknown_translation_service_tier_is_rejected_before_transport(self) -> None:
        transport = SequenceTransport(_output_text({}))

        with self.assertRaisesRegex(TranslationError, "priority"):
            OpenAIClient(transport=transport).translate_batch(
                "key",
                "gpt-test",
                [{"id": "u1", "text": "source"}],
                "en_us",
                "ja_jp",
                service_tier="fast",
            )

        self.assertEqual(transport.calls, [])

    def test_term_bindings_are_serialized_as_data_and_protocol_allows_semantic_reordering(
        self,
    ) -> None:
        transport = SequenceTransport(
            _output_text(
                {
                    "translations": {
                        "item_0000": {
                            "fragments": {
                                "fragment_0000": "Forgotten Blockと",
                                "fragment_0001": "を使って",
                                "fragment_0002": "を作成する。",
                            },
                            "token_positions": {"token_0000": 1, "token_0001": 0},
                        }
                    }
                }
            )
        )
        client = OpenAIClient(transport=transport)
        item = {
            "id": "u1",
            "text": (
                "Create a __MQP_0000__ using a Forgotten Block "
                "and a __MQP_0001__."
            ),
            "context": "quest description",
            "term_bindings": [
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
        }

        result = client.translate_batch(
            "key",
            "gpt-test",
            [item],
            "en_us",
            "ja_jp",
        )

        self.assertEqual(
            result["u1"],
            "Forgotten Blockと__MQP_0001__を使って__MQP_0000__を作成する。",
        )
        payload = transport.calls[0]["payload"]
        assert payload is not None
        provider_item = json.loads(payload["input"])["items"][0]
        self.assertEqual(
            provider_item,
            {
                "id": "u1",
                "response_key": "item_0000",
                "source_fragments": {
                    "fragment_0000": "Create a ",
                    "fragment_0001": " using a Forgotten Block and a ",
                    "fragment_0002": ".",
                },
                "source_token_order": ["token_0000", "token_0001"],
                "context": "quest description",
                "term_bindings": [
                    {
                        "token_key": "token_0000",
                        "source_term": "Forgotten Minion",
                        "approved_output": "忘れ去られたミニオン",
                    },
                    {
                        "token_key": "token_0001",
                        "source_term": "Carved Gloomgourd",
                        "approved_output": "くり抜かれたグルームゴード",
                    },
                ],
            },
        )
        self.assertNotIn("__MQP_", payload["input"])
        instructions = payload["instructions"]
        self.assertIn("term_bindings", instructions)
        self.assertIn("movable semantic units", instructions)
        self.assertIn("preserve the role of each term", instructions)
        self.assertIn("never attach an ordinary word", instructions)
        self.assertIn("tokens cover only their", instructions)
        self.assertIn("translate all meaningful fragment content", instructions)
        self.assertIn("non-empty translated fragment content", instructions)
        self.assertIn("omission or fusion of articles and determiners", instructions)

    def test_styled_bindings_are_normalized_and_reference_a_plain_body_item(
        self,
    ) -> None:
        transport = SequenceTransport(
            _output_text(
                {
                    "translations": {
                        "item_0000": {
                            "fragments": {
                                "fragment_0000": "",
                                "fragment_0001": "を使う。",
                            },
                            "token_positions": {"token_0000": 0},
                        },
                        "item_0001": {
                            "fragments": {
                                "fragment_0000": "エレメントバインダー",
                            },
                            "token_positions": {},
                        },
                    }
                }
            )
        )
        items = [
            {
                "id": "parent",
                "text": "Use __MQP_0000__.",
                "context": "quest description",
                "styled_bindings": [
                    {
                        "token": "__MQP_0000__",
                        "source_text": "Element Binder",
                        "body_item_id": "styled-body",
                    }
                ],
            },
            {
                "id": "styled-body",
                "text": "Element Binder",
                "context": "styled formatting body",
            },
        ]

        result = OpenAIClient(transport=transport).translate_batch(
            "key",
            "gpt-test",
            items,
            "en_us",
            "ja_jp",
        )

        self.assertEqual(
            result,
            {
                "parent": "__MQP_0000__を使う。",
                "styled-body": "エレメントバインダー",
            },
        )
        payload = transport.calls[0]["payload"]
        assert payload is not None
        provider_items = json.loads(payload["input"])["items"]
        self.assertEqual(
            provider_items[0]["styled_bindings"],
            [
                {
                    "token_key": "token_0000",
                    "source_text": "Element Binder",
                    "body_item_id": "styled-body",
                }
            ],
        )
        self.assertNotIn("__MQP_0000__", payload["input"])

    def test_invalid_styled_bindings_are_rejected_before_transport(self) -> None:
        valid_parent = {
            "id": "parent",
            "text": "Use __MQP_0000__.",
            "styled_bindings": [
                {
                    "token": "__MQP_0000__",
                    "source_text": "Element Binder",
                    "body_item_id": "styled-body",
                }
            ],
        }
        valid_body = {"id": "styled-body", "text": "Element Binder"}
        cases: dict[str, list[dict[str, Any]]] = {
            "missing child": [valid_parent],
            "body source mismatch": [
                valid_parent,
                {"id": "styled-body", "text": "Air"},
            ],
            "duplicate styled token": [
                {
                    "id": "parent",
                    "text": "Use __MQP_0000__.",
                    "styled_bindings": [
                        {
                            "token": "__MQP_0000__",
                            "source_text": "Element Binder",
                            "body_item_id": "styled-body",
                        },
                        {
                            "token": "__MQP_0000__",
                            "source_text": "Air",
                            "body_item_id": "other-body",
                        },
                    ],
                },
                valid_body,
                {"id": "other-body", "text": "Air"},
            ],
            "term and styled token overlap": [
                {
                    **valid_parent,
                    "term_bindings": [
                        {
                            "token": "__MQP_0000__",
                            "source_term": "Element Binder",
                            "approved_output": "Element Binder",
                        }
                    ],
                },
                valid_body,
            ],
            "child contains protected token": [
                {
                    "id": "parent",
                    "text": "Use __MQP_0000__.",
                    "styled_bindings": [
                        {
                            "token": "__MQP_0000__",
                            "source_text": "Element __MQP_0001__ Binder",
                            "body_item_id": "styled-body",
                        }
                    ],
                },
                {
                    "id": "styled-body",
                    "text": "Element __MQP_0001__ Binder",
                },
            ],
        }

        for case, items in cases.items():
            with self.subTest(case=case):
                transport = SequenceTransport(_output_text({}))

                with self.assertRaisesRegex(TranslationError, "styled_bindings"):
                    OpenAIClient(transport=transport).translate_batch(
                        "key",
                        "gpt-test",
                        items,
                        "en_us",
                        "ja_jp",
                    )

                self.assertEqual(transport.calls, [])

    def test_same_protected_token_value_can_be_used_independently_across_items(self) -> None:
        response = {
            "translations": {
                "item_0000": {
                    "fragments": {"fragment_0000": "甲", "fragment_0001": ""},
                    "token_positions": {"token_0000": 0},
                },
                "item_0001": {
                    "fragments": {"fragment_0000": "乙", "fragment_0001": ""},
                    "token_positions": {"token_0000": 0},
                },
            }
        }
        items = [
            {"id": "u1", "text": "One __MQP_0000__"},
            {"id": "u2", "text": "Two __MQP_0000__"},
        ]

        result = OpenAIClient(
            transport=SequenceTransport(_output_text(response))
        ).translate_batch("key", "gpt-test", items, "en_us", "ja_jp")

        self.assertEqual(
            result,
            {"u1": "甲__MQP_0000__", "u2": "乙__MQP_0000__"},
        )

    def test_literal_mqp_text_round_trips_through_structured_reconstruction(self) -> None:
        source = "Keep literal __MQP_0000__"
        protected = TokenProtector().protect(source)
        tokens = tuple(protected.replacements)
        self.assertEqual(len(tokens), 1)
        # The wrapper token cannot collide with the literal source marker.
        self.assertNotEqual(tokens[0], "__MQP_0000__")
        response = {
            "translations": {
                "item_0000": {
                    "fragments": {
                        "fragment_0000": "リテラルを保持: ",
                        "fragment_0001": "",
                    },
                    "token_positions": {"token_0000": 0},
                }
            }
        }
        transport = SequenceTransport(_output_text(response))

        reconstructed = OpenAIClient(transport=transport).translate_batch(
            "key",
            "gpt-test",
            [{"id": "u1", "text": protected.protected}],
            "en_us",
            "ja_jp",
        )["u1"]

        self.assertEqual(protected.restore(reconstructed), "リテラルを保持: __MQP_0000__")
        payload = transport.calls[0]["payload"]
        assert payload is not None
        self.assertNotIn("__MQP_0000__", payload["input"])
        self.assertNotIn(tokens[0], payload["input"])

    def test_invalid_format_and_layout_ranks_are_rejected_by_protected_restore(self) -> None:
        cases = (
            ("&aGreen&r plain", "装飾"),
            ("Left\nRight\tTail", "改行|タブ"),
        )
        for source, expected_error in cases:
            with self.subTest(source=source):
                protected = TokenProtector().protect(source)
                tokens = tuple(protected.replacements)
                self.assertEqual(len(tokens), 2)
                fragments = openai_client_module._PROTECTED_TOKEN_PATTERN.split(
                    protected.protected
                )
                response = {
                    "translations": {
                        "item_0000": {
                            "fragments": {
                                f"fragment_{index:04d}": fragment
                                for index, fragment in enumerate(fragments)
                            },
                            # This is a complete integer permutation, so the
                            # client reconstructs it. ProtectedText.restore is
                            # the authority that rejects semantic rank changes.
                            "token_positions": {
                                "token_0000": 1,
                                "token_0001": 0,
                            },
                        }
                    }
                }
                reconstructed = OpenAIClient(
                    transport=SequenceTransport(_output_text(response))
                ).translate_batch(
                    "key",
                    "gpt-test",
                    [{"id": "u1", "text": protected.protected}],
                    "en_us",
                    "ja_jp",
                )["u1"]

                with self.assertRaisesRegex(TranslationError, expected_error):
                    protected.restore(reconstructed)

    def test_japanese_unicode_instruction_is_not_applied_to_other_locales(self) -> None:
        transport = SequenceTransport(
            _output_text(_plain_translation_payload(u1="वैज्ञानिक"))
        )
        client = OpenAIClient(transport=transport)

        self.assertEqual(
            client.translate_batch(
                "key",
                "gpt-test",
                [{"id": "u1", "text": "Scientist", "context": "title"}],
                "en_us",
                "hi_in",
            ),
            {"u1": "वैज्ञानिक"},
        )
        payload = transport.calls[0]["payload"]
        assert payload is not None
        self.assertNotIn(JAPANESE_UNICODE_INSTRUCTIONS, payload["instructions"])

    def test_custom_prompt_replaces_default_and_only_locale_placeholders_are_rendered(self) -> None:
        custom = (
            "Translate {source_locale} -> {target_locale}. "
            'Keep this JSON example unchanged: {"tone":"friendly"}.'
        )
        transport = SequenceTransport(
            _output_text(_plain_translation_payload(u1="翻訳"))
        )
        client = OpenAIClient(transport=transport, translation_prompt=custom)

        result = client.translate_batch(
            "key",
            "gpt-test",
            [{"id": "u1", "text": "Source", "context": "title"}],
            "en_us",
            "ja_jp",
        )

        self.assertEqual(result, {"u1": "翻訳"})
        payload = transport.calls[0]["payload"]
        assert payload is not None
        rendered_custom = (
            'Translate en_us -> ja_jp. Keep this JSON example unchanged: {"tone":"friendly"}.'
        )
        self.assertEqual(
            payload["instructions"],
            rendered_custom
            + "\n\n"
            + IMMUTABLE_TRANSLATION_PROTOCOL
            + "\n\n"
            + JAPANESE_UNICODE_INSTRUCTIONS,
        )
        self.assertLess(
            payload["instructions"].index(rendered_custom),
            payload["instructions"].index(IMMUTABLE_TRANSLATION_PROTOCOL),
        )
        self.assertIn("one distinct integer", payload["instructions"])
        self.assertIn("layout tokens", payload["instructions"])
        self.assertIn("Do not add", payload["instructions"])
        self.assertNotIn("professional Minecraft modpack localizer", payload["instructions"])

    def test_input_ids_tokens_and_term_bindings_must_be_unambiguous(self) -> None:
        cases: list[list[dict[str, Any]]] = [
            [
                {"id": "u1", "text": "A __MQP_0000__"},
                {"id": "u1", "text": "B"},
            ],
            [
                {
                    "id": "u1",
                    "text": "A __MQP_0000__ and __MQP_0000__",
                }
            ],
            [{"id": "u1", "text": "A __MQP_NOT_A_TOKEN__"}],
            [
                {
                    "id": "u1",
                    "text": "A __MQP_0000__",
                    "term_bindings": [
                        {
                            "token": "__MQP_0001__",
                            "source_term": "A",
                            "approved_output": "A",
                        }
                    ],
                }
            ],
            [
                {
                    "id": "u1",
                    "text": "A __MQP_0000__",
                    "term_bindings": [
                        {
                            "token": "__MQP_0000__",
                            "source_term": "A",
                            "approved_output": "A",
                        },
                        {
                            "token": "__MQP_0000__",
                            "source_term": "A",
                            "approved_output": "A",
                        },
                    ],
                }
            ],
        ]

        for items in cases:
            with self.subTest(items=items):
                transport = SequenceTransport(_output_text({}))
                with self.assertRaises(TranslationError):
                    OpenAIClient(transport=transport).translate_batch(
                        "key", "gpt-test", items, "en_us", "ja_jp"
                    )
                self.assertEqual(transport.calls, [])

    def test_translation_and_fragment_key_sets_are_checked_locally(self) -> None:
        valid_item = {
            "fragments": {"fragment_0000": "前", "fragment_0001": "後"},
            "token_positions": {"token_0000": 0},
        }
        cases: list[dict[str, Any]] = [
            {"translations": {}},
            {"translations": {"item_0000": valid_item, "extra": valid_item}},
            {
                "translations": {
                    "item_0000": {
                        "fragments": {"fragment_0000": "前"},
                        "token_positions": {"token_0000": 0},
                    }
                }
            },
            {
                "translations": {
                    "item_0000": {
                        "fragments": {
                            "fragment_0000": "前",
                            "fragment_0001": "後",
                            "fragment_0002": "余分",
                        },
                        "token_positions": {"token_0000": 0},
                    }
                }
            },
            {
                "translations": {
                    "item_0000": {
                        "fragments": {
                            "fragment_0000": "前",
                            "fragment_0001": "後",
                        },
                        "token_positions": {},
                    }
                }
            },
            {
                "translations": {
                    "item_0000": {
                        "fragments": {
                            "fragment_0000": "前",
                            "fragment_0001": "後",
                        },
                        "token_positions": {"token_0000": 0, "token_0001": 0},
                    }
                }
            },
            {"translations": {"item_0000": {**valid_item, "extra": "x"}}},
            {"translations": {"item_0000": valid_item}, "extra": "x"},
        ]
        item = [{"id": "u1", "text": "A __MQP_0000__"}]

        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(TranslationError):
                    OpenAIClient(transport=SequenceTransport(_output_text(response))).translate_batch(
                        "key", "gpt-test", item, "en_us", "ja_jp"
                    )

    def test_token_positions_must_be_a_complete_integer_permutation(self) -> None:
        fragments = {
            "fragment_0000": "前",
            "fragment_0001": "中",
            "fragment_0002": "後",
        }
        cases: list[dict[str, Any]] = [
            {"token_0000": 0, "token_0001": 0},
            {"token_0000": -1, "token_0001": 1},
            {"token_0000": 0, "token_0001": 2},
            {"token_0000": True, "token_0001": 1},
            {"token_0000": 0.0, "token_0001": 1},
            {"token_0000": "0", "token_0001": 1},
        ]
        item = [
            {
                "id": "u1",
                "text": "A __MQP_0000__ B __MQP_0001__ C",
            }
        ]

        for positions in cases:
            response = {
                "translations": {
                    "item_0000": {
                        "fragments": fragments,
                        "token_positions": positions,
                    }
                }
            }
            with self.subTest(positions=positions):
                with self.assertRaises(OpenAIResponseProtocolError) as raised:
                    OpenAIClient(transport=SequenceTransport(_output_text(response))).translate_batch(
                        "key", "gpt-test", item, "en_us", "ja_jp"
                    )
                self.assertEqual(raised.exception.item_id, "u1")

    def test_fragment_cannot_contain_mqp_like_or_protected_syntax(self) -> None:
        unsafe_fragments = [
            "本文__MQP_0000__",
            "本文__mqp_NOT_A_TOKEN__",
            "本文**MQP_0000**",
            "本文`MQP\\_0000`",
            "§a本文",
            "本文\\n改行",
            "本文\n改行",
            "{0}",
            "https://example.invalid/path",
            "minecraft:stone",
            "/give",
        ]
        item = [{"id": "u1", "text": "source"}]

        for fragment in unsafe_fragments:
            response = _plain_translation_payload(u1=fragment)
            with self.subTest(fragment=fragment):
                with self.assertRaises(TranslationError):
                    OpenAIClient(transport=SequenceTransport(_output_text(response))).translate_batch(
                        "key", "gpt-test", item, "en_us", "ja_jp"
                    )

    def test_duplicate_json_object_key_is_rejected(self) -> None:
        raw = (
            '{"translations":{"item_0000":{"fragments":{"fragment_0000":"one",'
            '"fragment_0000":"two"},"token_positions":{}}}}'
        )
        transport = SequenceTransport({"status": "completed", "output_text": raw})

        with self.assertRaisesRegex(TranslationError, "重複したJSONキー"):
            OpenAIClient(transport=transport).translate_batch(
                "key",
                "gpt-test",
                [{"id": "u1", "text": "source"}],
                "en_us",
                "ja_jp",
            )

    def test_schema_property_limit_is_checked_before_transport(self) -> None:
        transport = SequenceTransport(_output_text({}))
        item = [{"id": "u1", "text": "A __MQP_0000__"}]

        with (
            patch.object(openai_client_module, "_MAX_STRUCTURED_OUTPUT_PROPERTIES", 6),
            self.assertRaisesRegex(TranslationError, "プロパティ数"),
        ):
            OpenAIClient(transport=transport).translate_batch(
                "key", "gpt-test", item, "en_us", "ja_jp"
            )

        self.assertEqual(transport.calls, [])

    def test_schema_text_limit_is_checked_before_transport(self) -> None:
        transport = SequenceTransport(_output_text({}))
        item = [{"id": "u1", "text": "source"}]

        with (
            patch.object(
                openai_client_module,
                "_MAX_STRUCTURED_OUTPUT_SCHEMA_CHARS",
                10,
            ),
            self.assertRaisesRegex(TranslationError, "文字数上限"),
        ):
            OpenAIClient(transport=transport).translate_batch(
                "key", "gpt-test", item, "en_us", "ja_jp"
            )

        self.assertEqual(transport.calls, [])

    def test_structurally_invalid_outputs_are_rejected(self) -> None:
        cases: list[dict[str, Any]] = [
            {"output_text": "not json"},
            _output_text({}),
            _output_text({"translations": {"id": "u1", "text": "x"}}),
            _output_text({"translations": ["invalid"]}),
            _output_text({"translations": [{"id": "u1", "text": 2}]}),
            _output_text(
                {
                    "translations": [
                        {"id": "u1", "text": "one"},
                        {"id": "u1", "text": "duplicate"},
                    ]
                }
            ),
            _output_text({"translations": [{"id": "extra", "text": "x"}]}),
            {"error": {"message": "response failed"}},
            {"output": []},
        ]
        item = [{"id": "u1", "text": "source", "context": "context"}]

        for response in cases:
            with self.subTest(response=response):
                client = OpenAIClient(transport=SequenceTransport(response), max_retries=0)
                with self.assertRaises(TranslationError):
                    client.translate_batch("key", "gpt-test", item, "en_us", "ja_jp")

    def test_non_completed_response_status_is_rejected_with_redacted_details(self) -> None:
        secret = "custom-secret-value-123456"
        output = json.dumps({"translations": [{"id": "u1", "text": "must not be used"}]})
        cases = [
            (
                {
                    "status": "failed",
                    "error": {
                        "code": "server_error",
                        "message": f"request failed for {secret} and sk-proj-other-secret-123456",
                    },
                    "output_text": output,
                },
                "server_error",
            ),
            (
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output_text": output,
                },
                "max_output_tokens",
            ),
            ({"status": "cancelled", "output_text": output}, "cancelled"),
            ({"status": "queued", "output_text": output}, "queued"),
        ]
        item = [{"id": "u1", "text": "source", "context": "context"}]

        for response, expected_detail in cases:
            with self.subTest(status=response["status"]):
                client = OpenAIClient(transport=SequenceTransport(response), max_retries=0)
                with self.assertRaises(TranslationError) as raised:
                    client.translate_batch(secret, "gpt-test", item, "en_us", "ja_jp")
                message = str(raised.exception)
                self.assertIn(str(response["status"]), message)
                self.assertIn(expected_detail, message)
                self.assertNotIn(secret, message)
                self.assertNotIn("sk-proj-other-secret-123456", message)

    def test_completed_refusal_stops_without_becoming_a_protocol_error(self) -> None:
        secret = "custom-secret-value-123456"
        valid_output = json.dumps(
            _plain_translation_payload(u1="使用してはいけない翻訳"),
            ensure_ascii=False,
        )
        transport = SequenceTransport(
            {
                "status": "completed",
                # Refusal must win even if a compatible transport also exposes
                # a seemingly valid aggregate output_text value.
                "output_text": valid_output,
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "refusal",
                                "refusal": (
                                    f"cannot process {secret} or "
                                    "sk-proj-other-secret-123456"
                                ),
                            }
                        ],
                    }
                ],
            }
        )

        with self.assertRaises(OpenAIRefusalError) as raised:
            OpenAIClient(transport=transport).translate_batch(
                secret,
                "gpt-test",
                [{"id": "u1", "text": "source"}],
                "en_us",
                "ja_jp",
            )

        self.assertNotIsInstance(raised.exception, OpenAIResponseProtocolError)
        self.assertIn("拒否", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("sk-proj-other-secret-123456", str(raised.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_completed_refusal_with_malformed_detail_still_stops_as_refusal(self) -> None:
        transport = SequenceTransport(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": {"bad": "shape"}}],
                    }
                ],
            }
        )

        with self.assertRaises(OpenAIRefusalError):
            OpenAIClient(transport=transport).translate_batch(
                "key",
                "gpt-test",
                [{"id": "u1", "text": "source"}],
                "en_us",
                "ja_jp",
            )

        self.assertEqual(len(transport.calls), 1)

    def test_malformed_response_output_containers_raise_translation_error(self) -> None:
        cases: list[dict[str, Any]] = [
            {"status": "completed", "output_text": 1},
            {"status": "completed", "output": None},
            {"status": "completed", "output": [None]},
            {"status": "completed", "output": [{"content": None}]},
            {"status": "completed", "output": [{"content": [None]}]},
            {
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": 1}]}],
            },
        ]
        item = [{"id": "u1", "text": "source", "context": "context"}]

        for response in cases:
            with self.subTest(response=response):
                client = OpenAIClient(transport=SequenceTransport(response), max_retries=0)
                with self.assertRaises(TranslationError):
                    client.translate_batch("key", "gpt-test", item, "en_us", "ja_jp")

    def test_429_retries_without_real_sleep(self) -> None:
        transport = SequenceTransport(
            OpenAIAPIError("rate limited", status=429, request_id="req-1"),
            _output_text(_plain_translation_payload(u1="成功")),
        )
        client = OpenAIClient(transport=transport, max_retries=2)

        with patch("mq_localizer.openai_client.time.sleep") as sleep:
            result = client.translate_batch(
                "key",
                "gpt-test",
                [{"id": "u1", "text": "source", "context": "context"}],
                "en_us",
                "ja_jp",
            )

        self.assertEqual(result, {"u1": "成功"})
        self.assertEqual(len(transport.calls), 2)
        sleep.assert_called_once_with(1.0)

    def test_statusless_timeout_retries_and_emits_immutable_typed_event(self) -> None:
        transport = SequenceTransport(
            OpenAIAPIError(
                "request timed out",
                retryable=True,
                kind="timeout",
            ),
            _output_text(_plain_translation_payload(u1="成功")),
        )
        events: list[OpenAIRetryEvent] = []
        client = OpenAIClient(
            transport=transport,
            max_retries=3,
            on_retry=events.append,
        )

        with patch("mq_localizer.openai_client.time.sleep") as sleep:
            result = client.translate_batch(
                "key",
                "gpt-test",
                [{"id": "u1", "text": "source", "context": "context"}],
                "en_us",
                "ja_jp",
            )

        self.assertEqual(result, {"u1": "成功"})
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            events,
            [
                OpenAIRetryEvent(
                    attempt=1,
                    max_retries=3,
                    delay=1.0,
                    endpoint="/responses",
                    status=None,
                    request_id="",
                    kind="timeout",
                )
            ],
        )
        with self.assertRaises(FrozenInstanceError):
            events[0].attempt = 99  # type: ignore[misc]
        sleep.assert_called_once_with(1.0)

    def test_retry_events_follow_capped_exponential_backoff(self) -> None:
        transport = SequenceTransport(
            OpenAIAPIError("busy", 503, "req-1"),
            OpenAIAPIError("busy", 503, "req-2"),
            OpenAIAPIError("busy", 503, "req-3"),
            OpenAIAPIError("busy", 503, "req-4"),
            {"data": []},
        )
        events: list[OpenAIRetryEvent] = []
        client = OpenAIClient(
            transport=transport,
            max_retries=4,
            on_retry=events.append,
        )

        with patch("mq_localizer.openai_client.time.sleep") as sleep:
            self.assertEqual(client.list_models("key"), [])

        self.assertEqual([event.attempt for event in events], [1, 2, 3, 4])
        self.assertEqual([event.delay for event in events], [1.0, 2.0, 4.0, 8.0])
        self.assertTrue(all(event.max_retries == 4 for event in events))
        self.assertTrue(all(event.endpoint == "/models" for event in events))
        self.assertTrue(all(event.status == 503 for event in events))
        self.assertEqual(
            [event.request_id for event in events],
            ["req-1", "req-2", "req-3", "req-4"],
        )
        self.assertTrue(all(event.kind == "http" for event in events))
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1.0, 2.0, 4.0, 8.0],
        )

    def test_retry_callback_failure_is_isolated(self) -> None:
        transport = SequenceTransport(
            OpenAIAPIError("temporary", 500, "req-temporary"),
            {"data": []},
        )
        callback_calls: list[OpenAIRetryEvent] = []

        def failing_callback(event: OpenAIRetryEvent) -> None:
            callback_calls.append(event)
            raise RuntimeError("observer failed")

        client = OpenAIClient(
            transport=transport,
            max_retries=1,
            on_retry=failing_callback,
        )
        with patch("mq_localizer.openai_client.time.sleep") as sleep:
            self.assertEqual(client.list_models("key"), [])

        self.assertEqual(len(callback_calls), 1)
        self.assertEqual(len(transport.calls), 2)
        sleep.assert_called_once_with(1.0)

    def test_statusless_timeout_exhaustion_uses_initial_plus_configured_retries(self) -> None:
        transport = SequenceTransport(
            *(
                OpenAIAPIError(
                    f"timeout {attempt}",
                    retryable=True,
                    kind="timeout",
                )
                for attempt in range(3)
            )
        )
        events: list[OpenAIRetryEvent] = []
        client = OpenAIClient(
            transport=transport,
            max_retries=2,
            on_retry=events.append,
        )

        with patch("mq_localizer.openai_client.time.sleep") as sleep:
            with self.assertRaises(OpenAIAPIError) as caught:
                client.list_models("key")

        self.assertEqual(len(transport.calls), 3)
        self.assertEqual([event.attempt for event in events], [1, 2])
        self.assertEqual([event.delay for event in events], [1.0, 2.0])
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertIsNone(caught.exception.status)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1.0, 2.0],
        )

        no_retry_transport = SequenceTransport(
            OpenAIAPIError(
                "timeout",
                retryable=True,
                kind="timeout",
            )
        )
        no_retry_events: list[OpenAIRetryEvent] = []
        with self.assertRaises(OpenAIAPIError):
            OpenAIClient(
                transport=no_retry_transport,
                max_retries=0,
                on_retry=no_retry_events.append,
            ).list_models("key")
        self.assertEqual(len(no_retry_transport.calls), 1)
        self.assertEqual(no_retry_events, [])

    def test_cancel_set_by_retry_callback_stops_before_delay_and_next_attempt(self) -> None:
        cancel = Event()
        transport = SequenceTransport(
            OpenAIAPIError(
                "temporary timeout",
                retryable=True,
                kind="timeout",
            ),
            {"data": []},
        )
        events: list[OpenAIRetryEvent] = []

        def cancel_retry(event: OpenAIRetryEvent) -> None:
            events.append(event)
            cancel.set()

        client = OpenAIClient(
            transport=transport,
            max_retries=3,
            on_retry=cancel_retry,
        )

        with self.assertRaises(CancelledError):
            client.list_models("key", cancel)

        self.assertEqual(len(events), 1)
        self.assertEqual(len(transport.calls), 1)

    def test_only_explicit_transport_and_retryable_http_errors_are_retried(self) -> None:
        for status in (408, 409, 429, 500, 503):
            with self.subTest(status=status):
                error = OpenAIAPIError("retry", status, "req-http")
                self.assertTrue(error.retryable)
                self.assertEqual(error.kind, "http")

        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                error = OpenAIAPIError("terminal", status, "req-http")
                self.assertFalse(error.retryable)
                self.assertEqual(error.kind, "http")

        transport = SequenceTransport(
            OpenAIAPIError("invalid JSON", kind="invalid_response"),
            {"data": []},
        )
        events: list[OpenAIRetryEvent] = []
        with patch("mq_localizer.openai_client.time.sleep") as sleep:
            with self.assertRaisesRegex(OpenAIAPIError, "invalid JSON") as caught:
                OpenAIClient(
                    transport=transport,
                    max_retries=3,
                    on_retry=events.append,
                ).list_models("key")

        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.kind, "invalid_response")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(events, [])
        sleep.assert_not_called()

    def test_non_retryable_and_exhausted_errors_keep_status_and_request_id(self) -> None:
        non_retry = SequenceTransport(OpenAIAPIError("bad request", 400, "req-bad"))
        with patch("mq_localizer.openai_client.time.sleep") as sleep:
            with self.assertRaises(OpenAIAPIError) as raised:
                OpenAIClient(transport=non_retry, max_retries=3).list_models("key")
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(raised.exception.request_id, "req-bad")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.kind, "http")
        self.assertIn("req-bad", str(raised.exception))
        sleep.assert_not_called()

        exhausted = SequenceTransport(
            OpenAIAPIError("busy", 429, "req-last"),
            OpenAIAPIError("still busy", 429, "req-last"),
        )
        with patch("mq_localizer.openai_client.time.sleep") as sleep:
            with self.assertRaises(OpenAIAPIError) as raised:
                OpenAIClient(transport=exhausted, max_retries=1).list_models("key")
        self.assertEqual(raised.exception.status, 429)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.kind, "http")
        self.assertIn("req-last", str(raised.exception))
        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
