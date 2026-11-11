"""Per-device logging helpers for Samsung AC Advanced."""

from __future__ import annotations

import logging
from typing import Any, MutableMapping


class SamsungACRuntimeLogState:
    """Shared per-device runtime log gate controlled by the Debug switch."""

    def __init__(self) -> None:
        self.enabled = False


class SamsungACDeviceLoggerAdapter(logging.LoggerAdapter):
    """Prefix every integration log message with the configured AC name.

    INFO/DEBUG output is gated per configured air conditioner by the integration's
    Debug switch. WARNING and higher always remain available. The underlying module
    logger is explicitly opened to DEBUG so Home Assistant users no longer need a
    ``logger: custom_components.samsung_ac_advanced: info`` entry in
    ``configuration.yaml`` merely to collect integration diagnostics.
    """

    def __init__(
        self,
        logger: logging.Logger,
        device_name: str,
        runtime_state: SamsungACRuntimeLogState | None = None,
    ) -> None:
        # Allow records to reach the per-device gate without requiring YAML logger
        # configuration. The adapter below still suppresses non-warning records
        # while Debug mode is off.
        # DEBUG is safe here because every integration record is emitted through
        # this adapter and therefore still passes the per-device runtime gate.
        logger.setLevel(logging.DEBUG)
        super().__init__(logger, {"device_name": device_name})
        self._device_label = f"Samsung AC ({device_name})"
        self._runtime_state = runtime_state

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - logging API name
        """Apply the per-device runtime gate before creating a log record."""

        if (
            level < logging.WARNING
            and self._runtime_state is not None
            and not self._runtime_state.enabled
        ):
            return False
        return self.logger.isEnabledFor(level)

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Emit useful DEBUG diagnostics while suppressing successful poll timing noise.

        Home Assistant's DataUpdateCoordinator emits a DEBUG timing line after
        every refresh. With four air conditioners on a 30-second interval this
        overwhelms the operational diagnostics. Suppress only the successful
        ``Finished fetching ... (success: True)`` timing record. Failed refreshes
        keep their normal ERROR/WARNING diagnostics and an unsuccessful timing
        record is not suppressed.
        """

        if (
            isinstance(msg, str)
            and msg.startswith(
                "Finished fetching %s data in %.3f seconds (success: %s)"
            )
            and args
            and args[-1] is True
        ):
            return
        super().debug(msg, *args, **kwargs)

    def process(
        self,
        msg: Any,
        kwargs: MutableMapping[str, Any],
    ) -> tuple[Any, MutableMapping[str, Any]]:
        return f"{self._device_label} {msg}", kwargs
