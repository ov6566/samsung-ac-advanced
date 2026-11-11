"""Optional runtime diagnostics for Samsung AC Advanced."""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any

from .device_logger import SamsungACDeviceLoggerAdapter, SamsungACRuntimeLogState


_LOGGER = logging.getLogger(__name__)

_SENSITIVE_PATH_PARTS = {
    "deviceid",
    "locationid",
    "ownerid",
    "roomid",
    "serialnumber",
    "serialnumberextra",
    "sn",
    "btmac",
    "ptopmac",
    "blemac",
    "wifimac",
    "ethmac",
    "otnduid",
    "token",
}


class SamsungACDebugTracker:
    """Track non-sensitive capability changes while debug mode is enabled."""

    def __init__(
        self,
        device_name: str,
        runtime_state: SamsungACRuntimeLogState | None = None,
    ) -> None:
        """Initialize the tracker in the disabled state."""

        self._logger = SamsungACDeviceLoggerAdapter(
            _LOGGER, device_name, runtime_state
        )
        self._enabled = False
        self._previous_values: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        """Return whether debug mode is enabled."""

        return self._enabled

    def enable(self, status: dict[str, Any] | None) -> None:
        """Enable debug mode and establish a baseline."""

        self._enabled = True
        self._previous_values = self._extract_values(status)
        self._logger.info("Debug mode enabled; capability baseline stored")

    def disable(self) -> None:
        """Disable debug mode and clear the baseline."""

        self._enabled = False
        self._previous_values = None
        self._logger.info("Debug mode disabled")

    def process(self, status: dict[str, Any]) -> None:
        """Compare a new status with the previous baseline and log changes."""

        if not self._enabled:
            return

        current_values = self._extract_values(status)

        if self._previous_values is None:
            self._previous_values = current_values
            self._logger.debug("Debug mode baseline initialized")
            return

        changed_paths = sorted(
            set(self._previous_values) | set(current_values)
        )
        changes: list[tuple[str, Any, Any]] = []

        for path in changed_paths:
            old_value = self._previous_values.get(path)
            new_value = current_values.get(path)

            if old_value != new_value:
                changes.append((path, old_value, new_value))

        if changes:
            self._logger.info(
                "Debug mode detected %s capability change(s)",
                len(changes),
            )

            for path, old_value, new_value in changes:
                self._logger.info(
                    "capability changed: %s | %r -> %r",
                    path,
                    old_value,
                    new_value,
                )
        self._previous_values = current_values

    def _extract_values(
        self,
        status: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Extract non-sensitive main-component attribute values."""

        if not isinstance(status, dict):
            return {}

        components = status.get("components", {})
        if not isinstance(components, dict):
            return {}

        main = components.get("main", {})
        if not isinstance(main, dict):
            return {}

        values: dict[str, Any] = {}

        for capability, capability_data in main.items():
            if not isinstance(capability_data, dict):
                continue

            for attribute, attribute_data in capability_data.items():
                if not isinstance(attribute_data, dict):
                    continue

                path = f"{capability}.{attribute}"

                if self._is_sensitive(path):
                    continue

                if "value" in attribute_data:
                    values[path] = deepcopy(attribute_data.get("value"))

        return values

    @staticmethod
    def _is_sensitive(path: str) -> bool:
        """Return whether a capability path may contain sensitive data."""

        normalized = path.replace("_", "").replace(".", "").lower()
        return any(
            sensitive in normalized
            for sensitive in _SENSITIVE_PATH_PARTS
        )
