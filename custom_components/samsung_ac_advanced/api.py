"""Samsung SmartThings API."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from .const import OUTBOUND_COMMAND_SPACING
from .setpoint import resolve_setpoint_endpoint

BASE_URL = "https://api.smartthings.com/v1"


class SamsungACAPIError(RuntimeError):
    """Raised when SmartThings rejects a Samsung AC request."""


class SamsungACAPI:
    """Samsung SmartThings REST API."""

    def __init__(self, device_id: str, token: str) -> None:
        self.device_id = device_id
        self.token = token
        self._latest_status: dict[str, Any] | None = None

        # Physical Samsung ACs do not reliably process commands that arrive in
        # rapid succession. This gate applies only to outgoing HA -> AC command
        # traffic. Status reads are deliberately not delayed by it.
        self._command_lock = asyncio.Lock()
        self._last_command_sent_at = 0.0

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                headers=self.headers,
                json=payload,
            ) as response:
                try:
                    result = await response.json(content_type=None)
                except Exception as err:
                    text = await response.text()
                    raise SamsungACAPIError(
                        f"SmartThings returned HTTP {response.status}: {text or 'invalid JSON'}"
                    ) from err

        if not isinstance(result, dict):
            raise SamsungACAPIError("SmartThings returned an invalid JSON response")

        error = result.get("error")
        if response.status >= 400 or isinstance(error, dict):
            code = error.get("code", "unknown") if isinstance(error, dict) else "http_error"
            message = (
                error.get("message", "Unknown SmartThings error")
                if isinstance(error, dict)
                else f"HTTP {response.status}"
            )
            raise SamsungACAPIError(
                f"SmartThings API error {code}: {message} (HTTP {response.status})"
            )

        return result

    async def _send_command(self, command: dict[str, Any]) -> dict[str, Any]:
        """Send one SmartThings command request with outbound-only pacing.

        Every public command method sends exactly one Samsung command. The
        lock/clock below guarantees at least OUTBOUND_COMMAND_SPACING seconds
        between separate outgoing commands regardless of which integration
        entity or Eco routine issued them. It never delays status reads.
        """

        async with self._command_lock:
            now = time.monotonic()
            remaining = OUTBOUND_COMMAND_SPACING - (now - self._last_command_sent_at)
            if remaining > 0:
                await asyncio.sleep(remaining)

            self._last_command_sent_at = time.monotonic()
            return await self._request_json(
                "POST",
                f"{BASE_URL}/devices/{self.device_id}/commands",
                payload={"commands": [command]},
            )

    async def get_status(self) -> dict[str, Any]:
        """Get current Samsung AC status without transport delay."""

        result = await self._request_json(
            "GET", f"{BASE_URL}/devices/{self.device_id}/status"
        )
        self._latest_status = result
        return result

    async def get_device(self) -> dict[str, Any]:
        return await self._request_json(
            "GET", f"{BASE_URL}/devices/{self.device_id}"
        )

    async def set_optional_mode(self, mode: str) -> dict[str, Any]:
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "custom.airConditionerOptionalMode",
                    "command": "setAcOptionalMode",
                    "arguments": [mode],
            }
        )

    async def set_oscillation_mode(self, mode: str) -> dict[str, Any]:
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "fanOscillationMode",
                    "command": "setFanOscillationMode",
                    "arguments": [mode],
            }
        )

    async def set_fan_speed(self, mode: str) -> dict[str, Any]:
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "airConditionerFanMode",
                    "command": "setFanMode",
                    "arguments": [mode],
            }
        )

    async def set_ac_mode(self, mode: str) -> dict[str, Any]:
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "airConditionerMode",
                    "command": "setAirConditionerMode",
                    "arguments": [mode],
            }
        )

    async def set_target_temperature(
        self, mode: str, temperature: float
    ) -> dict[str, Any]:
        endpoint = resolve_setpoint_endpoint(self._latest_status, mode)
        return await self._send_command(
            {
                    "component": "main",
                    "capability": endpoint.capability,
                    "command": endpoint.command,
                    "arguments": [temperature],
            }
        )

    async def set_auto_cleaning_mode(self, mode: str) -> dict[str, Any]:
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "custom.autoCleaningMode",
                    "command": "setAutoCleaningMode",
                    "arguments": [mode],
            }
        )

    async def set_filter_alarm_threshold(self, threshold: int) -> dict[str, Any]:
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "samsungce.dustFilterAlarm",
                    "command": "setAlarmThreshold",
                    "arguments": [threshold],
            }
        )

    async def reset_dust_filter(self) -> dict[str, Any]:
        """Reset Samsung's dust-filter cleaning reminder."""

        return await self._send_command(
            {
                    "component": "main",
                    "capability": "custom.dustFilter",
                    "command": "resetDustFilter",
                    "arguments": [],
            }
        )

    async def turn_on(self) -> dict[str, Any]:
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "switch",
                    "command": "on",
                    "arguments": [],
            }
        )

    async def turn_off(self) -> dict[str, Any]:
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "switch",
                    "command": "off",
                    "arguments": [],
            }
        )

    async def set_volume(self, volume: int) -> dict[str, Any]:
        if volume < 0 or volume > 100:
            raise ValueError("Volume must be between 0 and 100")
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "audioVolume",
                    "command": "setVolume",
                    "arguments": [volume],
            }
        )

    async def execute_device_mode_options(self, *options: str) -> dict[str, Any]:
        if not options:
            raise ValueError("At least one Device.Mode option is required")
        return await self._send_command(
            {
                    "component": "main",
                    "capability": "execute",
                    "command": "execute",
                    "arguments": [
                        "mode/vs/0",
                        {"x.com.samsung.da.options": list(options)},
                    ],
            }
        )

    async def set_display_light(self, enabled: bool) -> dict[str, Any]:
        # Samsung exposes inverted action tokens:
        # Light_Off turns the physical display on.
        # Light_On turns the physical display off.
        option = "Light_Off" if enabled else "Light_On"
        return await self.execute_device_mode_options(option)
