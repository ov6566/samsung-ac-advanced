"""Ownership and manual-override classification for Eco."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class EcoChangeOwner(StrEnum):
    CONTROLLER = "controller"
    SAMSUNG_FEEDBACK = "samsung_feedback"
    USER = "user"


@dataclass(slots=True)
class EcoChange:
    owner: EcoChangeOwner
    fields: set[str]


class EcoOverrideManager:
    """Classify incoming controlled-setting changes by ownership."""

    CONTROLLED_FIELDS = {
        "power",
        "hvac_mode",
        "target_temperature",
        "fan_mode",
        "oscillation_mode",
        "optional_mode",
    }

    @staticmethod
    def changed_fields(
        observed: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> set[str]:
        return {
            key
            for key in EcoOverrideManager.CONTROLLED_FIELDS
            if key in expected
            and expected[key] is not None
            and observed.get(key) is not None
            and observed.get(key) != expected[key]
        }

    def classify(
        self,
        *,
        observed: Mapping[str, Any],
        expected: Mapping[str, Any],
        transition_owns: bool,
        samsung_feedback_fields: set[str] | None = None,
        changed_fields: set[str] | None = None,
    ) -> EcoChange:
        fields = (
            set(changed_fields)
            if changed_fields is not None
            else self.changed_fields(observed, expected)
        )
        if not fields:
            return EcoChange(EcoChangeOwner.SAMSUNG_FEEDBACK, set())
        if transition_owns:
            return EcoChange(EcoChangeOwner.CONTROLLER, fields)
        if samsung_feedback_fields and fields.issubset(samsung_feedback_fields):
            return EcoChange(EcoChangeOwner.SAMSUNG_FEEDBACK, fields)
        return EcoChange(EcoChangeOwner.USER, fields)
