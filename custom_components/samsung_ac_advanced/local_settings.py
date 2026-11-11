"""Deferred Eco Pending Restore settings used while a Samsung AC is powered off."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class LocalDesiredSettings:
    """Pre-Eco values retained only by the USER_OFF/Pending Restore exception."""

    target_temperature: float | None = None
    fan_mode: str | None = None
    oscillation_mode: str | None = None
    target_mode: str | None = None
    optional_mode: str | None = None
    sound_enabled: bool | None = None
    auto_clean_enabled: bool | None = None
    source: str = "unknown"

    def update(
        self,
        *,
        target_temperature: float | None = None,
        fan_mode: str | None = None,
        oscillation_mode: str | None = None,
        target_mode: str | None = None,
        optional_mode: str | None = None,
        sound_enabled: bool | None = None,
        auto_clean_enabled: bool | None = None,
    ) -> None:
        """Update only explicitly supplied values."""

        if target_temperature is not None:
            self.target_temperature = float(target_temperature)
        if fan_mode is not None:
            self.fan_mode = fan_mode
        if oscillation_mode is not None:
            self.oscillation_mode = oscillation_mode
        if target_mode is not None:
            self.target_mode = target_mode
        if optional_mode is not None:
            self.optional_mode = optional_mode
        if sound_enabled is not None:
            self.sound_enabled = bool(sound_enabled)
        if auto_clean_enabled is not None:
            self.auto_clean_enabled = bool(auto_clean_enabled)

    def as_dict(self) -> dict[str, Any]:
        """Return a serializable representation."""

        return asdict(self)
