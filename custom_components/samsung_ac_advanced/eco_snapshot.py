"""Snapshot lifecycle for Samsung AC Advanced Eco."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class EcoRestoreSnapshot:
    """Immutable settings captured immediately before Eco activation."""

    hvac_mode: str | None
    target_temperature: float | None
    fan_mode: str | None
    oscillation_mode: str | None
    optional_mode: str | None
    sound_enabled: bool | None
    auto_clean_enabled: bool | None


@dataclass(frozen=True, slots=True)
class EcoPreSleepSnapshot:
    """Auto Clean state captured immediately before one Sleep period.

    Sound is no longer a per-Sleep setting.  The Original Snapshot owns the
    pre-Eco Sound state and Eco keeps Sound muted for its entire session.
    """

    auto_clean_enabled: bool | None


class EcoSnapshotManager:
    """Keep Original, Pre-Sleep and Pending Restore responsibilities separate."""

    def __init__(self) -> None:
        self.original: EcoRestoreSnapshot | None = None
        self.pre_sleep: EcoPreSleepSnapshot | None = None
        self.pending_restore: EcoRestoreSnapshot | None = None

    def capture_original(self, snapshot: EcoRestoreSnapshot) -> EcoRestoreSnapshot:
        """Capture once; Original Snapshot is immutable for the Eco session."""

        if self.original is None:
            self.original = snapshot
        return self.original

    def capture_pre_sleep(
        self, snapshot: EcoPreSleepSnapshot
    ) -> EcoPreSleepSnapshot:
        """Capture auxiliaries for the current Sleep period only."""

        self.pre_sleep = snapshot
        return snapshot

    def clear_pre_sleep(self) -> None:
        self.pre_sleep = None

    def create_pending_restore(self) -> EcoRestoreSnapshot | None:
        """Pending Restore always references the pre-Eco Original Snapshot."""

        self.pending_restore = self.original
        return self.pending_restore

    def clear_session(self) -> None:
        self.original = None
        self.pre_sleep = None

    def clear_pending(self) -> None:
        self.pending_restore = None

    def reset_all(self) -> None:
        self.original = None
        self.pre_sleep = None
        self.pending_restore = None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "original": asdict(self.original) if self.original else None,
            "pre_sleep": asdict(self.pre_sleep) if self.pre_sleep else None,
            "pending_restore": (
                asdict(self.pending_restore) if self.pending_restore else None
            ),
        }
