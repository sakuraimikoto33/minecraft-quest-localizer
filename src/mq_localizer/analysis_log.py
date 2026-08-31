from __future__ import annotations

import os
import re
import stat
import threading
import unicodedata
from datetime import datetime
from pathlib import Path


_API_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"(?i)Bearer\s+[^\s,;]+")
_LOG_FILE_PATTERN = re.compile(
    r"^analysis-\d{8}T\d{6}\.\d{6}-p\d+(?:-\d+)?\.log$"
)
_LOG_HEADER = "Minecraft Quest Localizer セッションログ\n"
_LEGACY_LOG_HEADER = "Minecraft Quest Localizer 解析ログ\n"
_OWNED_LOG_HEADERS = (_LOG_HEADER, _LEGACY_LOG_HEADER)


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
        self._entry_count = 0
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
            entry_number = self._entry_count + 1
            recorded_at = datetime.now().astimezone().isoformat(timespec="seconds")
            entry = (
                f"\n=== ログ記録 {entry_number}: {safe_section} ===\n"
                f"記録日時: {recorded_at}\n"
                f"レベル: {safe_level}\n"
                f"{safe_text or '記録内容なし'}\n"
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
            self._entry_count = entry_number
            return self.path

    def _create_file(self, entry: str) -> None:
        header = _LOG_HEADER + (
            f"セッション開始: {self.session_started.astimezone().isoformat(timespec='seconds')}\n"
            "画面では警告を100件まで表示します。"
            "このファイルには解析結果全文と実行ログを追記します。\n"
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
                or not _is_owned_log_file(candidate)
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


def _is_owned_log_file(path: Path) -> bool:
    """Only rotate regular files bearing this application's exact header."""

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return False
        maximum_header_length = max(len(header.encode("utf-8")) for header in _OWNED_LOG_HEADERS)
        with path.open("rb") as handle:
            # ``write_text`` and older Windows builds may have emitted CRLF.
            # Compare one complete line without its newline, never by prefix,
            # so a lookalike header cannot become an owned rotation target.
            first_line = handle.readline(maximum_header_length + 2)
        if first_line.endswith(b"\r\n"):
            first_line = first_line[:-2]
        elif first_line.endswith(b"\n"):
            first_line = first_line[:-1]
        expected = {header.rstrip("\n").encode("utf-8") for header in _OWNED_LOG_HEADERS}
        return first_line in expected
    except OSError:
        return False
