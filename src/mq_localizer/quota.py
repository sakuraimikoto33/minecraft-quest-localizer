"""Conservative, local accounting for OpenAI's data-sharing token offer.

This is not an organization Usage API or a billing guarantee. One ledger is
shared by all keys used on this installation; secrets are never persisted.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from .domain import TranslationError
from .io_utils import atomic_write_text


# Explicit allowlist, reviewed 2026-09-18 against help.openai.com/en/articles/10306912.
# Never infer eligibility for new dates, fine tunes, pro/search or provider aliases.
_GROUP_MODELS = {
    "1m": (
        "gpt-5.6-sol", "gpt-5.6", "gpt-5.5", "gpt-5.5-2026-04-23",
        "gpt-5.4", "gpt-5.4-2026-03-05", "gpt-5.2", "gpt-5.2-2025-12-11",
        "gpt-5.1", "gpt-5.1-2025-11-13", "gpt-5.1-codex", "gpt-5-codex",
        "gpt-5", "gpt-5-2025-08-07", "gpt-5-chat-latest",
        "gpt-4.1", "gpt-4.1-2025-04-14", "gpt-4o", "gpt-4o-2024-05-13",
        "gpt-4o-2024-08-06", "gpt-4o-2024-11-20", "o3", "o3-2025-04-16",
        "o1", "o1-2024-12-17", "o1-preview", "o1-preview-2024-09-12",
    ),
    "10m": (
        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.4-mini", "gpt-5.4-mini-2026-03-17",
        "gpt-5.4-nano", "gpt-5.4-nano-2026-03-17", "gpt-5.1-codex-mini",
        "gpt-5-mini", "gpt-5-mini-2025-08-07", "gpt-5-nano", "gpt-5-nano-2025-08-07",
        "gpt-4.1-mini", "gpt-4.1-mini-2025-04-14", "gpt-4.1-nano", "gpt-4.1-nano-2025-04-14",
        "gpt-4o-mini", "gpt-4o-mini-2024-07-18", "o4-mini", "o4-mini-2025-04-16",
        "o1-mini", "o1-mini-2024-09-12", "codex-mini-latest",
    ),
}
MODEL_GROUPS = {model: group for group, models in _GROUP_MODELS.items() for model in models}
GROUP_LABELS = {"1m": "1Mトークングループ", "10m": "10Mトークングループ"}


def group_for_model(model: str) -> str | None:
    return MODEL_GROUPS.get(model.strip())


def limits_for_usage_tier(tier: int) -> dict[str, int]:
    if type(tier) is not int or not 1 <= tier <= 5:
        raise ValueError("Usage Tierは1～5を選択してください")
    return {"1m": 250_000, "10m": 2_500_000} if tier <= 2 else {"1m": 1_000_000, "10m": 10_000_000}


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    group: str
    usage_tier: int
    daily_limit: int
    used_tokens: int
    reserved_tokens: int
    utc_date: str

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.daily_limit - self.used_tokens - self.reserved_tokens)


class ComplimentaryQuotaExhausted(TranslationError):
    def __init__(self, status: QuotaStatus, required_max_tokens: int) -> None:
        self.status = status
        self.required_max_tokens = required_max_tokens
        super().__init__(
            f"{GROUP_LABELS[status.group]}の残量不足: 残り{status.remaining_tokens:,} / "
            f"次のリクエストの最大使用量{required_max_tokens:,} tokens"
        )


def _token_count(value: object) -> bool:
    return type(value) is int and value >= 0


class QuotaLedger:
    """Atomic JSON ledger protected by an OS lock (including other processes).

    Unknown/in-flight reservations survive crashes and date changes. They are
    never silently released: even a timed-out request may still be generating.
    Actual daily usage resets at UTC midnight, not at local midnight.
    """

    def __init__(self, path: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _date(self) -> str:
        return self.now().astimezone(timezone.utc).date().isoformat()

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_suffix(".lock").open("a+b") as lock:
                # A real byte is needed for Windows byte-range locks.
                lock.seek(0, 2)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield self._load()
                finally:
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError, UnicodeError) as exc:
            raise TranslationError(
                "無料枠の使用量ファイルを安全に確認・保存できません。"
                "送信を停止しました。他の起動中アプリやopenai-usage.jsonを確認してください。"
            ) from exc

    def _load(self) -> dict:
        today = self._date()
        if not self.path.exists():
            return {"utc_date": today, "groups": {g: {"used_tokens": 0} for g in GROUP_LABELS}, "reservations": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("utc_date"), str):
            raise ValueError("Invalid quota ledger")
        datetime.strptime(data["utc_date"], "%Y-%m-%d")
        if data["utc_date"] > today:
            raise ValueError("Clock moved backwards")
        groups, reservations = data.get("groups"), data.get("reservations")
        if not isinstance(groups, dict) or set(groups) != set(GROUP_LABELS) or not isinstance(reservations, dict):
            raise ValueError("Invalid quota groups")
        for group in groups.values():
            if not isinstance(group, dict) or not _token_count(group.get("used_tokens")):
                raise ValueError("Invalid usage")
        for entry in reservations.values():
            if not isinstance(entry, dict) or entry.get("group") not in GROUP_LABELS or not _token_count(entry.get("tokens")):
                raise ValueError("Invalid reservation")
        if data["utc_date"] != today:
            data["utc_date"] = today
            data["groups"] = {g: {"used_tokens": 0} for g in GROUP_LABELS}
        return data

    def _save(self, data: dict) -> None:
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _status(data: dict, group: str, tier: int) -> QuotaStatus:
        return QuotaStatus(group, tier, limits_for_usage_tier(tier)[group],
                           data["groups"][group]["used_tokens"],
                           sum(r["tokens"] for r in data["reservations"].values() if r["group"] == group),
                           data["utc_date"])

    def statuses(self, tier: int) -> dict[str, QuotaStatus]:
        limits_for_usage_tier(tier)
        with self._locked() as data:
            return {g: self._status(data, g, tier) for g in GROUP_LABELS}

    def reserve(self, group: str, tier: int, tokens: int) -> str:
        if not _token_count(tokens) or tokens == 0:
            raise ValueError("Invalid reservation size")
        with self._locked() as data:
            status = self._status(data, group, tier)
            if tokens > status.remaining_tokens:
                raise ComplimentaryQuotaExhausted(status, tokens)
            reservation = uuid4().hex
            data["reservations"][reservation] = {"group": group, "tokens": tokens}
            self._save(data)
            return reservation

    def settle(self, reservation: str, tokens: int) -> None:
        if not _token_count(tokens):
            raise ValueError("Invalid actual usage")
        with self._locked() as data:
            entry = data["reservations"].pop(reservation, None)
            if entry is None:
                raise ValueError("Missing reservation")
            data["groups"][entry["group"]]["used_tokens"] += tokens
            self._save(data)

    def record(self, group: str, tokens: int) -> None:
        if not _token_count(tokens):
            raise ValueError("Invalid actual usage")
        with self._locked() as data:
            data["groups"][group]["used_tokens"] += tokens
            self._save(data)
