from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import unicodedata
from datetime import datetime
from pathlib import Path


_API_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"(?i)Bearer\s+[^\s,;]+")
_LOG_FILE_PATTERN = re.compile(
    r"^analysis-\d{8}T\d{6}\.\d{6}-p\d+(?:-\d+)?\.log$"
)
_DEBUG_LOG_FILE_PATTERN = re.compile(
    r"^debug-\d{8}T\d{6}\.\d{6}-p\d+(?:-\d+)?\.log$"
)
_OPENAI_JSON_FILE_PATTERN = re.compile(
    r"^openai-\d{8}T\d{6}\.\d{6}-p\d+(?:-\d+)?\.json$"
)
_LOG_HEADER = "Minecraft Quest Localizer セッションログ\n"
_DEBUG_LOG_HEADER = "Minecraft Quest Localizer デバッグログ\n"
_LEGACY_LOG_HEADER = "Minecraft Quest Localizer 解析ログ\n"
_OWNED_LOG_HEADERS = (_LOG_HEADER, _LEGACY_LOG_HEADER)
_OWNED_DEBUG_LOG_HEADERS = (_DEBUG_LOG_HEADER,)
_OPENAI_JSON_FORMAT = "minecraft-quest-localizer-openai-events-v1"
_OPENAI_JSON_HEADER = f'{{"metadata":{{"format":"{_OPENAI_JSON_FORMAT}",\n'
_OPENAI_JSON_TAIL = b"\n]}\n"
_COPY_CHUNK_SIZE = 1024 * 1024
_CONFIDENTIAL_JSON_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "proxy_authorization",
        "refresh_token",
        "set_cookie",
        "x_api_key",
    }
)


def redact_sensitive(text: str, *secrets: str) -> str:
    """Remove credentials from text before it can reach a persistent log."""

    redacted = str(text)
    for secret in secrets:
        if secret and len(secret) >= 8:
            redacted = redacted.replace(secret, "[API KEY REDACTED]")
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    return _API_KEY_PATTERN.sub("[API KEY REDACTED]", redacted)


def _safe_log_text(text: str, *secrets: str) -> str:
    """Normalize newlines and make embedded control characters visible."""

    redacted = redact_sensitive(text, *secrets).replace("\r\n", "\n").replace("\r", "\n")
    result: list[str] = []
    for character in redacted:
        if character in {"\n", "\t"}:
            result.append(character)
        elif unicodedata.category(character).startswith("C"):
            codepoint = ord(character)
            result.append(
                f"\\u{codepoint:04X}" if codepoint <= 0xFFFF else f"\\U{codepoint:08X}"
            )
        else:
            result.append(character)
    return "".join(result)


class SessionAnalysisLog:
    """One append-only UTF-8 journal per application session.

    The file is created lazily on the first durable UI event.  A completed
    analysis is one event in the same journal rather than a separate snapshot.
    Rotation only touches files with this class's strict filename format and a
    recognized application header, so unrelated files are never removed.
    """

    def __init__(
        self,
        directory: Path,
        *,
        max_files: int = 10,
        session_started: datetime | None = None,
        process_id: int | None = None,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be at least 1")
        self.directory = Path(directory).expanduser().resolve(strict=False)
        self.max_files = max_files
        self.session_started = session_started or datetime.now().astimezone()
        self.process_id = os.getpid() if process_id is None else process_id
        timestamp = self.session_started.strftime("%Y%m%dT%H%M%S.%f")
        self._base_name = f"analysis-{timestamp}-p{self.process_id}"
        self.path = self.directory / f"{self._base_name}.log"
        self._created = False
        self._lock = threading.Lock()

    def write(
        self,
        text: str,
        *secrets: str,
        level: str = "INFO",
        section: str = "実行ログ",
    ) -> Path:
        """Append one durable event and return the absolute journal path."""

        safe_text = _safe_log_text(text, *secrets).strip()
        safe_level = _safe_log_text(level, *secrets).strip().upper() or "INFO"
        safe_section = _safe_log_text(section, *secrets).strip() or "実行ログ"
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            recorded_at = datetime.now().astimezone().isoformat(timespec="seconds")
            prefix = f"{recorded_at} [{safe_level}] [{safe_section}] "
            entry = "".join(
                f"{prefix}{line}\n"
                for line in (safe_text or "記録内容なし").split("\n")
            )
            if not self._created:
                self._create_file(entry)
                self._created = True
                self._rotate()
            else:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(entry)
                    handle.flush()
                    os.fsync(handle.fileno())
            return self.path

    def _create_file(self, entry: str) -> None:
        header = _LOG_HEADER + (
            f"セッション開始: {self.session_started.astimezone().isoformat(timespec='seconds')}\n"
            "画面では警告を100件まで表示します。"
            "このファイルには解析結果全文と実行ログを時系列で追記します。\n"
        )
        suffix = 0
        while True:
            candidate = self.directory / (
                f"{self._base_name}.log" if suffix == 0 else f"{self._base_name}-{suffix}.log"
            )
            try:
                with candidate.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(header)
                    handle.write(entry)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                suffix += 1
                continue
            self.path = candidate
            return

    def _rotate(self) -> None:
        candidates: list[tuple[int, str, Path]] = []
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return
        for candidate in entries:
            if (
                not _LOG_FILE_PATTERN.fullmatch(candidate.name)
                or candidate == self.path
                or not _is_owned_log_file(candidate, _OWNED_LOG_HEADERS)
            ):
                continue
            try:
                modified = candidate.lstat().st_mtime_ns
            except OSError:
                continue
            candidates.append((modified, candidate.name, candidate))
        remove_count = max(0, len(candidates) + 1 - self.max_files)
        for _modified, _name, candidate in sorted(candidates)[:remove_count]:
            try:
                candidate.unlink()
            except OSError:
                # Rotation failure must not discard the newly written analysis.
                pass


class SessionDebugLog(SessionAnalysisLog):
    """Separate append-only journal for opt-in diagnostic information."""

    def __init__(
        self,
        directory: Path,
        *,
        max_files: int = 10,
        session_started: datetime | None = None,
        process_id: int | None = None,
    ) -> None:
        super().__init__(
            directory,
            max_files=max_files,
            session_started=session_started,
            process_id=process_id,
        )
        timestamp = self.session_started.strftime("%Y%m%dT%H%M%S.%f")
        self._base_name = f"debug-{timestamp}-p{self.process_id}"
        self.path = self.directory / f"{self._base_name}.log"

    def _create_file(self, entry: str) -> None:
        header = _DEBUG_LOG_HEADER + (
            f"セッション開始: {self.session_started.astimezone().isoformat(timespec='seconds')}\n"
            "デバッグログが有効な間の通常ログと詳細診断を"
            "時系列で追記します。認証情報はマスクします。\n"
        )
        suffix = 0
        while True:
            candidate = self.directory / (
                f"{self._base_name}.log"
                if suffix == 0
                else f"{self._base_name}-{suffix}.log"
            )
            try:
                with candidate.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(header)
                    handle.write(entry)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                suffix += 1
                continue
            self.path = candidate
            return

    def _rotate(self) -> None:
        candidates: list[tuple[int, str, Path]] = []
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return
        for candidate in entries:
            if (
                not _DEBUG_LOG_FILE_PATTERN.fullmatch(candidate.name)
                or candidate == self.path
                or not _is_owned_log_file(candidate, _OWNED_DEBUG_LOG_HEADERS)
            ):
                continue
            try:
                modified = candidate.lstat().st_mtime_ns
            except OSError:
                continue
            candidates.append((modified, candidate.name, candidate))
        remove_count = max(0, len(candidates) + 1 - self.max_files)
        for _modified, _name, candidate in sorted(candidates)[:remove_count]:
            try:
                candidate.unlink()
            except OSError:
                pass


class SessionOpenAIJsonLog:
    """One valid JSON document containing opt-in OpenAI events per session."""

    def __init__(
        self,
        directory: Path,
        *,
        max_files: int = 10,
        session_started: datetime | None = None,
        process_id: int | None = None,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be at least 1")
        self.directory = Path(directory).expanduser().resolve(strict=False)
        self.max_files = max_files
        self.session_started = session_started or datetime.now().astimezone()
        self.process_id = os.getpid() if process_id is None else process_id
        timestamp = self.session_started.strftime("%Y%m%dT%H%M%S.%f")
        self._base_name = f"openai-{timestamp}-p{self.process_id}"
        self.path = self.directory / f"{self._base_name}.json"
        self._created = False
        self._next_sequence = 1
        self._lock = threading.Lock()

    def write(self, message: str, *secrets: str) -> Path:
        """Append one callback event while leaving a complete JSON document."""

        with self._lock:
            event = _openai_callback_event(message, *secrets)
            event["sequence"] = self._next_sequence
            event["recorded_at"] = datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            )
            encoded_event = json.dumps(
                event,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
            self.directory.mkdir(parents=True, exist_ok=True)
            if not self._created:
                self._create_file(encoded_event)
                self._created = True
                self._rotate()
            else:
                self._append_event(encoded_event)
            self._next_sequence += 1
            return self.path

    def _create_file(self, event: bytes) -> None:
        metadata = (
            _OPENAI_JSON_HEADER
            + '"application":"Minecraft Quest Localizer",\n'
            + '"session_started":'
            + json.dumps(
                self.session_started.astimezone().isoformat(timespec="seconds"),
                ensure_ascii=False,
            )
            + ',\n"process_id":'
            + str(self.process_id)
            + '},\n"events":[\n'
        ).encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{self._base_name}.",
                suffix=".tmp",
                dir=self.directory,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(metadata)
                handle.write(event)
                handle.write(_OPENAI_JSON_TAIL)
                handle.flush()
                os.fsync(handle.fileno())

            suffix = 0
            while True:
                candidate = self.directory / (
                    f"{self._base_name}.json"
                    if suffix == 0
                    else f"{self._base_name}-{suffix}.json"
                )
                try:
                    # A hard link publishes the already-complete document
                    # atomically and fails rather than overwriting a collision.
                    os.link(temporary_path, candidate)
                except FileExistsError:
                    suffix += 1
                    continue
                self.path = candidate
                return
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _append_event(self, event: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with self.path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                document_size = source.tell()
                if document_size < len(_OPENAI_JSON_TAIL):
                    raise OSError(f"OpenAI JSONログの末尾が不正です: {self.path}")
                source.seek(-len(_OPENAI_JSON_TAIL), os.SEEK_END)
                if source.read() != _OPENAI_JSON_TAIL:
                    raise OSError(f"OpenAI JSONログの末尾が不正です: {self.path}")
                prefix_size = document_size - len(_OPENAI_JSON_TAIL)
                source.seek(0)
                with tempfile.NamedTemporaryFile(
                    mode="w+b",
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=self.directory,
                    delete=False,
                ) as target:
                    temporary_path = Path(target.name)
                    remaining = prefix_size
                    while remaining:
                        chunk = source.read(min(remaining, _COPY_CHUNK_SIZE))
                        if not chunk:
                            raise OSError(
                                f"OpenAI JSONログを最後まで読み取れませんでした: {self.path}"
                            )
                        target.write(chunk)
                        remaining -= len(chunk)
                    target.write(b",\n")
                    target.write(event)
                    target.write(_OPENAI_JSON_TAIL)
                    target.flush()
                    os.fsync(target.fileno())
            if temporary_path is None:
                raise AssertionError("OpenAI JSONの一時ファイルが作成されていません")
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _rotate(self) -> None:
        candidates: list[tuple[int, str, Path]] = []
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return
        for candidate in entries:
            if (
                not _OPENAI_JSON_FILE_PATTERN.fullmatch(candidate.name)
                or candidate == self.path
                or not _is_owned_openai_json_file(candidate)
            ):
                continue
            try:
                modified = candidate.lstat().st_mtime_ns
            except OSError:
                continue
            candidates.append((modified, candidate.name, candidate))
        remove_count = max(0, len(candidates) + 1 - self.max_files)
        for _modified, _name, candidate in sorted(candidates)[:remove_count]:
            try:
                candidate.unlink()
            except OSError:
                pass


def _openai_callback_event(message: str, *secrets: str) -> dict[str, object]:
    heading, separator, payload = str(message).partition("\n")
    phase_match = re.fullmatch(
        r"OpenAI (REQUEST|RESPONSE|ERROR|CANCELLED)",
        heading.strip(),
    )
    if not separator or phase_match is None:
        raise ValueError("OpenAI debug callbackの見出しが不正です")
    try:
        loaded = json.loads(
            payload,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_json_object_with_duplicate_keys,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("OpenAI debug callbackのJSONが不正です") from exc
    if not isinstance(loaded, dict):
        raise ValueError("OpenAI debug callbackのJSON rootがobjectではありません")
    if isinstance(loaded, _JSONObject) and loaded.duplicate_keys:
        duplicate_keys = ", ".join(
            redact_sensitive(key, *secrets) for key in sorted(loaded.duplicate_keys)
        )
        raise ValueError(
            f"OpenAI debug callbackのJSON rootに重複キーがあります: {duplicate_keys}"
        )
    heading_phase = phase_match.group(1)
    if "phase" in loaded and loaded["phase"] != heading_phase:
        raise ValueError("OpenAI debug callbackの見出しとphaseが一致しません")
    event = _redact_json_value(loaded, *secrets)
    if not isinstance(event, dict):
        raise AssertionError("redacted OpenAI event is not an object")
    event["phase"] = heading_phase
    return event


class _JSONObject(dict[str, object]):
    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        super().__init__()
        self.duplicate_keys: set[str] = set()
        for key, value in pairs:
            if key in self:
                self.duplicate_keys.add(key)
            self[key] = value


def _json_object_with_duplicate_keys(pairs: list[tuple[str, object]]) -> _JSONObject:
    return _JSONObject(pairs)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSONではない数値です: {value}")


def _redact_json_value(value: object, *secrets: str) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, child in value.items():
            text_key = str(key)
            safe_key = redact_sensitive(text_key, *secrets)
            normalized_key = text_key.strip().casefold().replace("-", "_")
            redacted[safe_key] = (
                "[REDACTED]"
                if normalized_key in _CONFIDENTIAL_JSON_FIELDS
                else _redact_json_value(child, *secrets)
            )
        return redacted
    if isinstance(value, list):
        return [_redact_json_value(item, *secrets) for item in value]
    if isinstance(value, str):
        return redact_sensitive(value, *secrets)
    return value


def _is_owned_openai_json_file(path: Path) -> bool:
    """Recognize only JSON logs created with this exact format marker."""

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return False
        maximum = len(_OPENAI_JSON_HEADER.encode("utf-8")) + 2
        with path.open("rb") as handle:
            first_line = handle.readline(maximum)
        if first_line.endswith(b"\r\n"):
            first_line = first_line[:-2]
        elif first_line.endswith(b"\n"):
            first_line = first_line[:-1]
        return first_line == _OPENAI_JSON_HEADER.rstrip("\n").encode("utf-8")
    except OSError:
        return False


def _is_owned_log_file(
    path: Path,
    owned_headers: tuple[str, ...] = _OWNED_LOG_HEADERS,
) -> bool:
    """Only rotate regular files bearing this application's exact header."""

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return False
        maximum_header_length = max(len(header.encode("utf-8")) for header in owned_headers)
        with path.open("rb") as handle:
            # ``write_text`` and older Windows builds may have emitted CRLF.
            # Compare one complete line without its newline, never by prefix,
            # so a lookalike header cannot become an owned rotation target.
            first_line = handle.readline(maximum_header_length + 2)
        if first_line.endswith(b"\r\n"):
            first_line = first_line[:-2]
        elif first_line.endswith(b"\n"):
            first_line = first_line[:-1]
        expected = {header.rstrip("\n").encode("utf-8") for header in owned_headers}
        return first_line in expected
    except OSError:
        return False
