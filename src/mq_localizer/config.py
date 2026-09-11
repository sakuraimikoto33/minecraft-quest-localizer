from __future__ import annotations

import base64
import ctypes
import json
import os
import re
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .categories import DEFAULT_TRANSLATION_CATEGORY_IDS, FTB_TRANSLATION_CATEGORIES
from .io_utils import atomic_write_text
from .openai_client import (
    DEFAULT_API_BASE_URL,
    DEFAULT_TRANSLATION_PROMPT,
    LEGACY_DEFAULT_TRANSLATION_PROMPTS,
    MAX_TRANSLATION_PROMPT_LENGTH,
    is_official_api_base_url,
    is_safe_model_id,
    normalize_api_base_url,
)
from .scan_limits import GlossaryScanLimits


APP_NAME = "MinecraftQuestLocalizer"
MAX_CACHED_MODEL_COUNT = 4096
_DPAPI_ENTROPY = b"minecraft-quest-localizer/openai-api-key/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def default_config_dir() -> Path:
    if os.name == "nt" and os.getenv("APPDATA"):
        return Path(os.environ["APPDATA"]) / APP_NAME
    if os.getenv("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]) / "minecraft-quest-localizer"
    return Path.home() / ".config" / "minecraft-quest-localizer"


@dataclass(slots=True)
class AppSettings:
    api_base_url: str = DEFAULT_API_BASE_URL
    model: str = ""
    cached_models: list[str] = field(default_factory=list)
    cached_models_base_url: str = DEFAULT_API_BASE_URL
    fast_mode: bool = False
    debug_logging: bool = False
    translation_prompt: str = DEFAULT_TRANSLATION_PROMPT
    source_locale: str = "en_us"
    target_locale: str = "ja_jp"
    adapter_id: str = "auto"
    minecraft_version: str = ""
    batch_size: int = 24
    batch_char_limit: int = 9000
    request_timeout: int = 120
    max_retries: int = 3
    preserve_existing: bool = True
    skip_glossary_confirmation: bool = False
    scan_resourcepacks: bool = False
    glossary_scan_limits_enabled: bool = True
    glossary_max_source_members: int = 100_000
    glossary_max_language_file_mib: int = 16
    glossary_max_source_language_mib: int = 64
    glossary_max_total_language_mib: int = 512
    translation_categories: list[str] = field(
        default_factory=lambda: [category.id for category in FTB_TRANSLATION_CATEGORIES]
    )
    save_api_key: bool = False
    api_key_ciphertext: str = ""
    api_key_base_url: str = ""
    last_source_path: str = ""

    @property
    def glossary_scan_limits(self) -> GlossaryScanLimits:
        return GlossaryScanLimits(
            enabled=self.glossary_scan_limits_enabled,
            max_source_members=self.glossary_max_source_members,
            max_language_file_mib=self.glossary_max_language_file_mib,
            max_source_language_mib=self.glossary_max_source_language_mib,
            max_total_language_mib=self.glossary_max_total_language_mib,
        )

    def sanitized_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("api_key_ciphertext", None)
        return data


class SettingsStore:
    """JSON settings plus optional Windows DPAPI encryption for the API key."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (default_config_dir() / "settings.json")

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return AppSettings()
        if not isinstance(raw, dict):
            return AppSettings()
        return _validated_settings(raw)

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(self.path, payload, encoding="utf-8")

    def read_api_key(self, settings: AppSettings) -> str:
        try:
            api_base_url = normalize_api_base_url(settings.api_base_url)
        except (TypeError, ValueError):
            return ""
        if is_official_api_base_url(api_base_url):
            environment_key = os.getenv("OPENAI_API_KEY", "").strip()
            if environment_key:
                return environment_key
        if not settings.save_api_key or not settings.api_key_ciphertext:
            return ""
        if settings.api_key_base_url:
            try:
                key_base_url = normalize_api_base_url(settings.api_key_base_url)
            except (TypeError, ValueError):
                return ""
            if key_base_url != api_base_url:
                return ""
        elif not is_official_api_base_url(api_base_url):
            # Keys saved before endpoint scoping existed belong to OpenAI only.
            return ""
        try:
            encrypted = base64.b64decode(settings.api_key_ciphertext, validate=True)
            return _dpapi_unprotect(encrypted).decode("utf-8")
        except (OSError, ValueError, UnicodeError):
            return ""

    def set_api_key(self, settings: AppSettings, api_key: str, persist: bool) -> None:
        if any(ord(character) < 32 or ord(character) == 127 for character in api_key):
            raise ValueError("API key に制御文字を含めることはできません")
        api_key = api_key.strip()
        try:
            api_base_url = normalize_api_base_url(settings.api_base_url)
        except (TypeError, ValueError):
            api_base_url = ""
        should_save = bool(persist and api_key and api_base_url)
        settings.save_api_key = False
        settings.api_key_ciphertext = ""
        settings.api_key_base_url = ""
        if not should_save:
            return
        encrypted = _dpapi_protect(api_key.encode("utf-8"))
        settings.save_api_key = True
        settings.api_key_ciphertext = base64.b64encode(encrypted).decode("ascii")
        settings.api_key_base_url = api_base_url

    @property
    def secure_persistence_available(self) -> bool:
        return os.name == "nt"


_LOCALE_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$", re.IGNORECASE)
_MINECRAFT_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?$")


def _validated_settings(raw: dict[str, Any]) -> AppSettings:
    """Load valid fields independently and fall back safely for bad values."""

    defaults = AppSettings()
    result = AppSettings()
    api_base_url_present = "api_base_url" in raw
    api_base_url_value = raw.get("api_base_url", defaults.api_base_url)
    api_base_url_valid = False
    if isinstance(api_base_url_value, str):
        try:
            result.api_base_url = normalize_api_base_url(api_base_url_value)
        except ValueError:
            result.api_base_url = ""
        else:
            api_base_url_valid = True
    else:
        result.api_base_url = ""

    # A missing field is the legacy OpenAI configuration. An explicitly invalid
    # field must not silently redirect saved credentials or content to OpenAI.
    if not api_base_url_present:
        result.api_base_url = DEFAULT_API_BASE_URL
        api_base_url_valid = True

    short_strings = ("adapter_id",)
    path_strings = ("last_source_path",)
    for name in short_strings:
        value = raw.get(name, getattr(defaults, name))
        if isinstance(value, str) and len(value) <= 512:
            setattr(result, name, value)
    minecraft_version = raw.get("minecraft_version", defaults.minecraft_version)
    if (
        isinstance(minecraft_version, str)
        and len(minecraft_version) <= 32
        and (
            not minecraft_version
            or _MINECRAFT_VERSION_PATTERN.fullmatch(minecraft_version.strip())
        )
    ):
        result.minecraft_version = minecraft_version.strip()
    prompt = raw.get("translation_prompt", defaults.translation_prompt)
    if (
        isinstance(prompt, str)
        and prompt.strip()
        and len(prompt) <= MAX_TRANSLATION_PROMPT_LENGTH
    ):
        normalized_prompt = prompt.strip()
        if normalized_prompt in LEGACY_DEFAULT_TRANSLATION_PROMPTS:
            result.translation_prompt = DEFAULT_TRANSLATION_PROMPT
        else:
            result.translation_prompt = prompt
    for name in path_strings:
        value = raw.get(name, getattr(defaults, name))
        if isinstance(value, str) and len(value) <= 32767:
            setattr(result, name, value)

    for name in ("source_locale", "target_locale"):
        value = raw.get(name, getattr(defaults, name))
        if isinstance(value, str) and len(value) <= 32 and _LOCALE_PATTERN.fullmatch(value):
            setattr(result, name, value.lower())
    if result.source_locale == result.target_locale:
        result.source_locale = defaults.source_locale
        result.target_locale = defaults.target_locale

    integer_ranges = {
        "batch_size": (1, 100),
        "batch_char_limit": (500, 50000),
        "request_timeout": (10, 600),
        "max_retries": (0, 10),
    }
    for name, (minimum, maximum) in integer_ranges.items():
        value = raw.get(name, getattr(defaults, name))
        if type(value) is int and minimum <= value <= maximum:
            setattr(result, name, value)

    glossary_limit_names = (
        "glossary_scan_limits_enabled",
        "glossary_max_source_members",
        "glossary_max_language_file_mib",
        "glossary_max_source_language_mib",
        "glossary_max_total_language_mib",
    )
    glossary_limit_values = {
        name: raw.get(name, getattr(defaults, name)) for name in glossary_limit_names
    }
    try:
        limits = GlossaryScanLimits(
            enabled=glossary_limit_values["glossary_scan_limits_enabled"],
            max_source_members=glossary_limit_values["glossary_max_source_members"],
            max_language_file_mib=glossary_limit_values[
                "glossary_max_language_file_mib"
            ],
            max_source_language_mib=glossary_limit_values[
                "glossary_max_source_language_mib"
            ],
            max_total_language_mib=glossary_limit_values[
                "glossary_max_total_language_mib"
            ],
        )
    except (TypeError, ValueError):
        pass
    else:
        result.glossary_scan_limits_enabled = limits.enabled
        result.glossary_max_source_members = limits.max_source_members
        result.glossary_max_language_file_mib = limits.max_language_file_mib
        result.glossary_max_source_language_mib = limits.max_source_language_mib
        result.glossary_max_total_language_mib = limits.max_total_language_mib

    cached_models_base_url_value = raw.get(
        "cached_models_base_url", defaults.cached_models_base_url
    )
    cached_models_base_url_valid = False
    if isinstance(cached_models_base_url_value, str):
        try:
            cached_models_base_url = normalize_api_base_url(
                cached_models_base_url_value
            )
        except ValueError:
            cached_models_base_url = ""
        else:
            cached_models_base_url_valid = True
    else:
        cached_models_base_url = ""

    cache_matches_endpoint = (
        api_base_url_valid
        and cached_models_base_url_valid
        and cached_models_base_url == result.api_base_url
    )
    if cache_matches_endpoint:
        result.cached_models_base_url = cached_models_base_url
        model = raw.get("model", defaults.model)
        if isinstance(model, str):
            normalized_model = model.strip()
            if not normalized_model or is_safe_model_id(normalized_model):
                result.model = normalized_model

        cached_models = raw.get("cached_models", defaults.cached_models)
        if (
            isinstance(cached_models, list)
            and len(cached_models) <= MAX_CACHED_MODEL_COUNT
            and all(
                isinstance(model, str)
                and is_safe_model_id(model.strip())
                for model in cached_models
            )
        ):
            result.cached_models = list(
                dict.fromkeys(model.strip() for model in cached_models)
            )
    else:
        result.model = ""
        result.cached_models = []
        result.cached_models_base_url = (
            result.api_base_url if api_base_url_valid else ""
        )

    for name in (
        "preserve_existing",
        "save_api_key",
        "fast_mode",
        "debug_logging",
        "skip_glossary_confirmation",
        "scan_resourcepacks",
    ):
        value = raw.get(name, getattr(defaults, name))
        if type(value) is bool:
            setattr(result, name, value)
    categories = raw.get("translation_categories", defaults.translation_categories)
    if (
        isinstance(categories, list)
        and len(categories) <= len(DEFAULT_TRANSLATION_CATEGORY_IDS)
        and all(isinstance(category, str) for category in categories)
        and set(categories).issubset(DEFAULT_TRANSLATION_CATEGORY_IDS)
    ):
        selected = set(categories)
        result.translation_categories = [
            category.id for category in FTB_TRANSLATION_CATEGORIES if category.id in selected
        ]
    ciphertext = raw.get("api_key_ciphertext", "")
    if result.save_api_key and isinstance(ciphertext, str) and len(ciphertext) <= 65536:
        result.api_key_ciphertext = ciphertext
    api_key_base_url_value = raw.get("api_key_base_url", "")
    api_key_is_legacy_unscoped = api_key_base_url_value == ""
    api_key_base_url = ""
    api_key_base_url_valid = False
    if isinstance(api_key_base_url_value, str) and api_key_base_url_value:
        try:
            api_key_base_url = normalize_api_base_url(api_key_base_url_value)
        except ValueError:
            pass
        else:
            api_key_base_url_valid = True

    key_scope_matches_endpoint = (
        api_base_url_valid
        and (
            (
                api_key_base_url_valid
                and api_key_base_url == result.api_base_url
            )
            or (
                api_key_is_legacy_unscoped
                and is_official_api_base_url(result.api_base_url)
            )
        )
    )
    if result.api_key_ciphertext and key_scope_matches_endpoint:
        result.api_key_base_url = api_key_base_url
    else:
        result.save_api_key = False
        result.api_key_ciphertext = ""
        result.api_key_base_url = ""

    if not api_base_url_valid:
        result.model = ""
        result.cached_models = []
        result.cached_models_base_url = ""
        result.fast_mode = False
        result.save_api_key = False
        result.api_key_ciphertext = ""
        result.api_key_base_url = ""
    elif not is_official_api_base_url(result.api_base_url):
        result.fast_mode = False
    return result


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("API key persistence requires Windows DPAPI")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_DPAPI_ENTROPY)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        APP_NAME,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))
        del source_buffer, entropy_buffer


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("API key persistence requires Windows DPAPI")
    source, source_buffer = _blob(data)
    entropy, entropy_buffer = _blob(_DPAPI_ENTROPY)
    output = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))
        if description:
            kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        del source_buffer, entropy_buffer
