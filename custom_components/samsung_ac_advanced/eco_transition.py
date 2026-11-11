"""Tracked controller transitions for Samsung AC Advanced Eco."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, Mapping



@dataclass(slots=True)
class EcoTransition:
    """One controller-owned command sequence awaiting Samsung confirmation."""

    name: str
    expected: dict[str, Any]
    started_at: float
    deadline: float


class EcoTransitionManager:
    """Track controller commands until status confirms or times out."""

    def __init__(self, logger: logging.Logger | logging.LoggerAdapter) -> None:
        self._logger = logger
        self._transition: EcoTransition | None = None
        self._debug_enabled = False

    def set_debug_enabled(self, enabled: bool) -> None:
        """Enable or disable optional transition DEBUG diagnostics."""

        self._debug_enabled = enabled

    @property
    def active(self) -> bool:
        return self._transition is not None

    @property
    def name(self) -> str | None:
        return self._transition.name if self._transition else None

    def begin(
        self,
        name: str,
        expected: Mapping[str, Any],
        *,
        timeout: float = 120.0,
    ) -> None:
        now = time.monotonic()
        self._transition = EcoTransition(
            name=name,
            expected=dict(expected),
            started_at=now,
            deadline=now + timeout,
        )
        if self._debug_enabled:
            self._logger.debug(
                "Eco transition scheduled: %s expected=%s", name, dict(expected)
            )

    def cancel(self, *, reason: str | None = None) -> None:
        """Cancel the active transition so it can no longer own later feedback."""

        transition = self._transition
        self._transition = None
        if transition is not None and self._debug_enabled:
            self._logger.debug(
                "Eco transition cancelled: %s%s",
                transition.name,
                f" ({reason})" if reason else "",
            )

    def observe(self, observed: Mapping[str, Any]) -> str | None:
        """Observe status and return confirmed/timeout, otherwise None."""

        transition = self._transition
        if transition is None:
            return None

        # An expired transition must never be resurrected by a later status
        # change that happens to equal the old expected value. Check the
        # deadline before accepting confirmation.
        if time.monotonic() >= transition.deadline:
            name = transition.name
            missing = {
                key: {"expected": value, "observed": observed.get(key)}
                for key, value in transition.expected.items()
                if value is not None and observed.get(key) != value
            }
            self._transition = None
            self._logger.warning("Eco transition timed out: %s mismatches=%s", name, missing)
            return "timeout"

        if all(
            observed.get(key) == value
            for key, value in transition.expected.items()
            if value is not None
        ):
            name = transition.name
            self._transition = None
            self._logger.info("Eco transition confirmed: %s", name)
            return "confirmed"

        return None

    def owns(self, changed_fields: set[str]) -> bool:
        """Return whether all changed fields belong to the active transition."""

        transition = self._transition
        return bool(
            transition
            and changed_fields
            and changed_fields.issubset(transition.expected.keys())
        )

    def diagnostics(self) -> dict[str, Any] | None:
        transition = self._transition
        if transition is None:
            return None
        return {
            "name": transition.name,
            "expected": dict(transition.expected),
            "remaining_seconds": max(0.0, transition.deadline - time.monotonic()),
        }
