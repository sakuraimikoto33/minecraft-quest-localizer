from __future__ import annotations

from dataclasses import dataclass


MIB_BYTES = 1024 * 1024

SOURCE_MEMBERS_MIN = 1
SOURCE_MEMBERS_MAX = 1_000_000
LANGUAGE_FILE_MIB_MIN = 1
LANGUAGE_FILE_MIB_MAX = 256
SOURCE_LANGUAGE_MIB_MIN = 1
SOURCE_LANGUAGE_MIB_MAX = 1024
TOTAL_LANGUAGE_MIB_MIN = 1
TOTAL_LANGUAGE_MIB_MAX = 4096


@dataclass(frozen=True, slots=True)
class GlossaryScanLimits:
    """User-configurable limits for Mod, KubeJS, and resource-pack scans.

    The count applies to each asset inventory. MiB values use binary
    mebibytes so the values displayed in settings map exactly to the byte
    budgets used by the scanner. Minecraft's official language assets use
    separate fixed integrity and safety limits.
    """

    max_source_members: int = 100_000
    max_language_file_mib: int = 16
    max_source_language_mib: int = 64
    max_total_language_mib: int = 512
    enabled: bool = True

    def __post_init__(self) -> None:
        ranged_values = (
            (
                "max_source_members",
                self.max_source_members,
                SOURCE_MEMBERS_MIN,
                SOURCE_MEMBERS_MAX,
            ),
            (
                "max_language_file_mib",
                self.max_language_file_mib,
                LANGUAGE_FILE_MIB_MIN,
                LANGUAGE_FILE_MIB_MAX,
            ),
            (
                "max_source_language_mib",
                self.max_source_language_mib,
                SOURCE_LANGUAGE_MIB_MIN,
                SOURCE_LANGUAGE_MIB_MAX,
            ),
            (
                "max_total_language_mib",
                self.max_total_language_mib,
                TOTAL_LANGUAGE_MIB_MIN,
                TOTAL_LANGUAGE_MIB_MAX,
            ),
        )
        for name, value, minimum, maximum in ranged_values:
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")

        if type(self.enabled) is not bool:
            raise TypeError("enabled must be a boolean")

        if self.max_language_file_mib > self.max_source_language_mib:
            raise ValueError(
                "max_language_file_mib must not exceed max_source_language_mib"
            )
        if self.max_source_language_mib > self.max_total_language_mib:
            raise ValueError(
                "max_source_language_mib must not exceed max_total_language_mib"
            )

    @property
    def max_language_file_bytes(self) -> int:
        return self.max_language_file_mib * MIB_BYTES

    @property
    def max_source_language_bytes(self) -> int:
        return self.max_source_language_mib * MIB_BYTES

    @property
    def max_total_language_bytes(self) -> int:
        return self.max_total_language_mib * MIB_BYTES

    @property
    def effective_max_source_members(self) -> int | None:
        if not self.enabled:
            return None
        return self.max_source_members

    @property
    def effective_max_language_file_bytes(self) -> int | None:
        if not self.enabled:
            return None
        return self.max_language_file_bytes

    @property
    def effective_max_source_language_bytes(self) -> int | None:
        if not self.enabled:
            return None
        return self.max_source_language_bytes

    @property
    def effective_max_total_language_bytes(self) -> int | None:
        if not self.enabled:
            return None
        return self.max_total_language_bytes
