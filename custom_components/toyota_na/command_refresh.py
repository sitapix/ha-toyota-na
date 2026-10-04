"""Bounded cloud reads after vehicle commands."""

import asyncio
import logging

from toyota_na.exceptions import AuthError
from toyota_na.vehicle.base_vehicle import RemoteRequestCommand, VehicleFeatures

_LOGGER = logging.getLogger(__name__)
ENGINE_STATUS_INTERVAL = 20
ENGINE_STATUS_TIMEOUT = 90

APPSYNC_STATUS_TIMEOUT = 20
APPSYNC_STATUS_INTERVAL = 2
APPSYNC_COMMANDS = (
    RemoteRequestCommand.DoorLock, RemoteRequestCommand.DoorUnlock,
    RemoteRequestCommand.EngineStart, RemoteRequestCommand.EngineStop,
)


def _command_state_matches(vehicle, command):
    if command in (RemoteRequestCommand.EngineStart, RemoteRequestCommand.EngineStop):
        feature = vehicle.features.get(VehicleFeatures.RemoteStartStatus)
        return getattr(feature, "on", None) is (command == RemoteRequestCommand.EngineStart)
    values = [getattr(vehicle.features.get(key), "locked", None) for key in (
        VehicleFeatures.FrontDriverDoor, VehicleFeatures.FrontPassengerDoor,
        VehicleFeatures.RearDriverDoor, VehicleFeatures.RearPassengerDoor,
    )]
    if command == RemoteRequestCommand.DoorLock:
        return all(value is True for value in values)
    return any(value is False for value in values)


async def _refresh_appsync(coordinator, vin, command):
    # Completion callbacks confirm the operation, not its current telemetry.
    # Read immediately, then allow cloud telemetry to catch up with bounded reads.
    async with asyncio.timeout(APPSYNC_STATUS_TIMEOUT):
        for attempt in range(4):
            if attempt:
                await asyncio.sleep(APPSYNC_STATUS_INTERVAL)
            vehicle = next((v for v in coordinator.data or [] if v.vin == vin), None)
            if vehicle is None or not vehicle.can_receive_status:
                return
            try:
                status = await vehicle._client.graphql_get_vehicle_status(
                    vin, vehicle.backdoor_type, vehicle.region,
                )
            except AuthError:
                await coordinator.async_request_refresh()
                return
            except Exception as err:
                _LOGGER.debug("Post-command vehicle status failed: %s", type(err).__name__)
                continue
            # A regular coordinator poll may have replaced this vehicle while awaiting.
            vehicle = next((v for v in coordinator.data or [] if v.vin == vin), None)
            if vehicle is None:
                return
            if status:
                vehicle.apply_graphql_status(status)
                coordinator.async_set_updated_data(coordinator.data)
                state = status.get("vehicleState") or {}
                telemetry = state.get("engine") if command in (
                    RemoteRequestCommand.EngineStart, RemoteRequestCommand.EngineStop,
                ) else state.get("doors")
                if telemetry and _command_state_matches(vehicle, command):
                    return


async def refresh_after_command(coordinator, vin=None, command=None, *, delay=10):
    try:
        vehicle = next((v for v in coordinator.data or [] if v.vin == vin), None)
        if command in APPSYNC_COMMANDS and getattr(vehicle, "uses_appsync", False):
            await _refresh_appsync(coordinator, vin, command)
            return
        if command in (RemoteRequestCommand.EngineStart, RemoteRequestCommand.EngineStop):
            vehicle = next((item for item in coordinator.data or [] if item.vin == vin), None)
            if vehicle is None:
                return
            if not vehicle.uses_appsync:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + ENGINE_STATUS_TIMEOUT
                for _ in range(4):
                    await asyncio.sleep(min(ENGINE_STATUS_INTERVAL, max(0, deadline - loop.time())))
                    remaining = deadline - loop.time()
                    vehicle = next((item for item in coordinator.data or [] if item.vin == vin), None)
                    if remaining <= 0 or vehicle is None or not vehicle.can_receive_status:
                        return
                    try:
                        running = await asyncio.wait_for(vehicle.poll_engine_status(), min(20, remaining))
                    except AuthError:
                        await coordinator.async_request_refresh()
                        return
                    except Exception as err:
                        _LOGGER.debug("Post-command engine status failed: %s", err)
                        continue
                    coordinator.async_set_updated_data(coordinator.data)
                    if running is (command == RemoteRequestCommand.EngineStart):
                        return
                return
        await asyncio.sleep(delay)
        await coordinator.async_request_refresh()
    except Exception as err:
        _LOGGER.debug("Post-command refresh failed: %s", err)
