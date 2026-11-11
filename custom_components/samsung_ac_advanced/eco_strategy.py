"""Eco control strategies for Samsung AC Advanced."""

from __future__ import annotations

from abc import ABC, abstractmethod
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .eco_controller import SamsungACEcoController


class EcoStrategy(ABC):
    """Base strategy for the Eco target-reached phase."""

    def __init__(self, controller: SamsungACEcoController) -> None:
        self.controller = controller

    @abstractmethod
    async def async_target_reached(self, mode: str) -> None:
        """Apply the strategy when the Eco target is reached."""

    @abstractmethod
    async def async_restart(self, mode: str) -> None:
        """Resume cooling or heating after temperature drift."""

    async def async_cancel_cleanup(self) -> None:
        """Remove hidden strategy state after a manual override."""

    async def async_maintain(self, status: dict) -> None:
        """Maintain hidden strategy state while the target remains satisfied."""


class EcoExternalStrategy(EcoStrategy):
    """Fully switch off the AC while an external sensor monitors the room."""

    async def async_target_reached(self, mode: str) -> None:
        await self.controller._async_enter_external_sleep(mode)

    async def async_restart(self, mode: str) -> None:
        await self.controller._async_start_external_resume(mode)


class EcoInternalStrategy(EcoStrategy):
    """Keep the AC quietly ventilating so its internal sensor stays active."""

    def __init__(self, controller: SamsungACEcoController) -> None:
        super().__init__(controller)
        self._quiet_retry_after = 0.0

    async def async_target_reached(self, mode: str) -> None:
        controller = self.controller
        # Samsung resets Optional Mode Quiet when the thermal mode is changed
        # to Wind.  Confirm the stable Stand-by base profile first, then let
        # async_maintain() apply Quiet as a separate controller-owned step.
        desired = {
            "power": "on",
            "hvac_mode": "wind",
            "fan_mode": "low",
            "oscillation_mode": "fixed",
            "sound_volume": 0,
        }
        await controller._async_start_pending_reconcile(
            purpose="target_reached_internal",
            desired=desired,
            detail="Eco internal Stand-by entry",
            finish_eco=False,
        )
        self._quiet_retry_after = 0.0
        controller.logger.info(
            "Eco target reached in %s mode (room: %.1f °C, target: %.1f °C); "
            "serial Stand-by entry started for fan-only, Low and fixed; background Quiet follows after confirmation",
            mode,
            controller.temperature.value,
            controller.target_temperature,
        )

    async def async_restart(self, mode: str) -> None:
        controller = self.controller
        desired = controller._active_eco_desired(mode)
        await controller._async_start_pending_reconcile(
            purpose="resume_internal",
            desired=desired,
            detail="Eco internal resume",
            finish_eco=False,
        )
        controller.logger.info(
            "Eco resume to %s started (room: %.1f °C, target: %.1f °C); "
            "Heat/Cool, Fan Auto, fixed oscillation and Quiet will be confirmed serially",
            mode,
            controller.temperature.value,
            controller.target_temperature,
        )

    async def async_maintain(self, status: dict) -> None:
        optional = self.controller._status_attribute_value(
            status,
            "custom.airConditionerOptionalMode",
            "acOptionalMode",
        )
        if optional == "quiet" or time.monotonic() < self._quiet_retry_after:
            return
        self.controller.logger.info(
            "Eco background Quiet was reset to %r; applying Quiet again",
            optional,
        )
        self.controller.transitions.begin(
            "quiet_repair",
            {"optional_mode": "quiet"},
            timeout=60.0,
        )
        await self.controller.api.set_optional_mode("quiet")
        self._quiet_retry_after = time.monotonic() + 15.0

    async def async_cancel_cleanup(self) -> None:
        await self.controller.api.set_optional_mode("off")
