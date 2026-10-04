"""Command timing, duplicate protection, and telemetry confirmation regressions."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_button as ha
import test_appsync_transport as transport
import test_vehicle_behavior as behavior
from custom_components.toyota_na import command_refresh as refresh
from custom_components.toyota_na import patch_client as client
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand as Command, VehicleFeatures
from toyota_na.vehicle.entity_types.ToyotaLockableOpening import ToyotaLockableOpening


def status(*, running=None, locked=None):
    state = {}
    if running is not None:
        state['engine'] = {'running': running}
    if locked is not None:
        state['doors'] = {key: {'lock': {'status': 'locked' if locked else 'unlocked'}} for key in (
            'driverSide', 'passengerSide', 'rearDriverSide', 'rearPassengerSide')}
    return {'vehicleState': state}


class FastRefreshTests(unittest.IsolatedAsyncioTestCase):
    def make(self, replies):
        api = SimpleNamespace(graphql_get_vehicle_status=AsyncMock(side_effect=replies))
        vehicle = behavior.make_vehicle(api)
        vehicle._generation = ApiVehicleGeneration.MM24
        coordinator = ha.DataUpdateCoordinator([vehicle])
        return vehicle, coordinator, api

    async def test_first_read_is_immediate_and_only_queries_the_target(self):
        for command, response in ((Command.EngineStart, status(running=True)),
                                  (Command.EngineStop, status(running=False)),
                                  (Command.DoorLock, status(locked=True)),
                                  (Command.DoorUnlock, status(locked=False))):
            with self.subTest(command=command):
                vehicle, coordinator, api = self.make([response])
                coordinator.data.append(ha.FakeVehicle(set(), vin='OTHER'))
                with patch.object(refresh.asyncio, 'sleep', AsyncMock()) as sleep:
                    await refresh.refresh_after_command(coordinator, vehicle.vin, command, delay=10)
                api.graphql_get_vehicle_status.assert_awaited_once_with(vehicle.vin, vehicle.backdoor_type, vehicle.region)
                sleep.assert_not_awaited()
                self.assertEqual(0, coordinator.refreshes)
                self.assertTrue(refresh._command_state_matches(vehicle, command))

    async def test_lagging_telemetry_is_read_again_without_resending(self):
        vehicle, coordinator, api = self.make([status(running=False), status(running=True)])
        vehicle.send_command = AsyncMock()
        with patch.object(refresh, 'APPSYNC_STATUS_INTERVAL', 0):
            await refresh.refresh_after_command(coordinator, vehicle.vin, Command.EngineStart)
        self.assertEqual(2, api.graphql_get_vehicle_status.await_count)
        vehicle.send_command.assert_not_awaited()
        self.assertTrue(vehicle.features[VehicleFeatures.RemoteStartStatus].on)

    async def test_missing_status_never_creates_optimistic_state_and_is_bounded(self):
        vehicle, coordinator, api = self.make([{}, {}, {}, {}])
        vehicle.apply_graphql_status(status(running=False))
        with patch.object(refresh, 'APPSYNC_STATUS_INTERVAL', 0):
            await refresh.refresh_after_command(coordinator, vehicle.vin, Command.EngineStart)
        self.assertEqual(4, api.graphql_get_vehicle_status.await_count)
        self.assertFalse(vehicle.features[VehicleFeatures.RemoteStartStatus].on)

    async def test_stalled_status_read_obeys_deadline(self):
        vehicle, coordinator, api = self.make([])
        async def stall(*args):
            await asyncio.Event().wait()
        api.graphql_get_vehicle_status.side_effect = stall
        with patch.object(refresh, 'APPSYNC_STATUS_TIMEOUT', 0.02):
            await asyncio.wait_for(refresh.refresh_after_command(coordinator, vehicle.vin, Command.EngineStart), 0.2)
        self.assertEqual(1, api.graphql_get_vehicle_status.await_count)

    async def test_regular_poll_replacement_receives_the_result(self):
        vehicle, coordinator, api = self.make([])
        replacement, _, _ = self.make([])
        async def read(*args):
            coordinator.data = [replacement]
            return status(running=True)
        api.graphql_get_vehicle_status.side_effect = read
        await refresh.refresh_after_command(coordinator, vehicle.vin, Command.EngineStart)
        self.assertTrue(replacement.features[VehicleFeatures.RemoteStartStatus].on)
        self.assertNotIn(VehicleFeatures.RemoteStartStatus, vehicle.features)

    async def test_cancelled_read_propagates(self):
        vehicle, coordinator, api = self.make([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await refresh.refresh_after_command(coordinator, vehicle.vin, Command.EngineStart)


class WakeTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_wake_waits_two_seconds(self):
        toyota = SimpleNamespace(wake_vehicles=AsyncMock())
        with patch.object(client, 'WAKE_SETTLE_SECONDS', 2), patch.object(client.asyncio, 'sleep', AsyncMock()) as sleep:
            await client._pre_wake_for_command(toyota, 'ONE', 'US')
        toyota.wake_vehicles.assert_awaited_once()
        sleep.assert_awaited_once_with(2)

    async def test_recent_settled_wake_skips_wake_and_pause(self):
        toyota = SimpleNamespace(wake_vehicles=AsyncMock(), _recent_vehicle_wakes={'ONE': 96})
        with patch.object(client.time, 'monotonic', return_value=100), patch.object(client, 'WAKE_SETTLE_SECONDS', 2), patch.object(client.asyncio, 'sleep', AsyncMock()) as sleep:
            await client._pre_wake_for_command(toyota, 'ONE', 'US')
        toyota.wake_vehicles.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_just_woken_vehicle_waits_only_remaining_settle_time(self):
        toyota = SimpleNamespace(wake_vehicles=AsyncMock(), _recent_vehicle_wakes={'ONE': 99})
        with patch.object(client.time, 'monotonic', return_value=100), patch.object(client, 'WAKE_SETTLE_SECONDS', 2), patch.object(client.asyncio, 'sleep', AsyncMock()) as sleep:
            await client._pre_wake_for_command(toyota, 'ONE', 'US')
        toyota.wake_vehicles.assert_not_awaited()
        sleep.assert_awaited_once_with(1)

    async def test_expired_wake_or_other_vehicle_does_not_skip_wake(self):
        for recent in ({'ONE': 60}, {'OTHER': 98}):
            toyota = SimpleNamespace(wake_vehicles=AsyncMock(), _recent_vehicle_wakes=recent)
            with patch.object(client.time, 'monotonic', return_value=100), patch.object(client.asyncio, 'sleep', AsyncMock()):
                await client._pre_wake_for_command(toyota, 'ONE', 'US')
            toyota.wake_vehicles.assert_awaited_once()

    async def test_failed_wake_is_not_cached(self):
        toyota = SimpleNamespace(wake_vehicles=AsyncMock(side_effect=RuntimeError('offline')))
        await client._pre_wake_for_command(toyota, 'ONE', 'US')
        self.assertFalse(getattr(toyota, '_recent_vehicle_wakes', {}))


class ConcurrentCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_active_or_queued_command_is_not_sent(self):
        toyota = transport._CommandClient()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        async def execute(*args, **kwargs):
            calls.append(kwargs['trace']['command'])
            entered.set()
            await release.wait()
        with patch.object(client, '_execute_appsync_operation', side_effect=execute):
            first = asyncio.create_task(client.remote_request_24mm(toyota, 'ONE', 'door-lock'))
            await entered.wait()
            with self.assertRaisesRegex(ValueError, 'already in progress'):
                await client.remote_request_24mm(toyota, 'ONE', 'door-lock')
            queued = asyncio.create_task(client.remote_request_24mm(toyota, 'ONE', 'engine-start'))
            await asyncio.sleep(0)
            with self.assertRaisesRegex(ValueError, 'already in progress'):
                await client.remote_request_24mm(toyota, 'ONE', 'engine-start')
            release.set()
            await asyncio.gather(first, queued)
        self.assertEqual(['door-lock', 'engine-start'], calls)
        self.assertFalse(toyota._pending_remote_commands)

    async def test_cancelled_queue_entry_is_removed_and_does_not_send(self):
        toyota = transport._CommandClient()
        lock = asyncio.Lock()
        await lock.acquire()
        toyota._remote_locks = {'ONE': lock}
        queued = asyncio.create_task(client.remote_request_24mm(toyota, 'ONE', 'engine-start'))
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertFalse(toyota._pending_remote_commands)
        self.assertEqual([], toyota.command_calls)


class EntityFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_stays_pending_through_refresh_and_blocks_second_press(self):
        vehicle = ha.FakeVehicle({Command.EngineStart})
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entity = ha.button.ToyotaCommandButton(Command.EngineStart, 'mdi:engine', ha.ConfigEntry(), coordinator, 'Start', vehicle.vin)
        entity.hass = ha.FakeHass(coordinator)
        entered, release = asyncio.Event(), asyncio.Event()
        async def read(*args, **kwargs):
            entered.set()
            await release.wait()
        with patch.object(ha.button, 'refresh_after_command', side_effect=read):
            await entity.async_press()
            await entered.wait()
            self.assertTrue(entity.extra_state_attributes['command_pending'])
            with self.assertRaises(ha.exceptions.ServiceValidationError):
                await entity.async_press()
            self.assertEqual([Command.EngineStart], vehicle.sent)
            release.set()
            await asyncio.gather(*entity.hass.tasks)
        self.assertFalse(entity.extra_state_attributes['command_pending'])

    async def test_lock_direction_uses_requested_action_even_with_stale_locked_state(self):
        vehicle = ha.FakeVehicle({Command.DoorLock, Command.DoorUnlock})
        vehicle.features[VehicleFeatures.FrontDriverDoor] = ToyotaLockableOpening(closed=True, locked=True)
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entity = ha.lock_platform.ToyotaLock(ha.ConfigEntry(), coordinator, '', vehicle.vin)
        entity.hass = ha.FakeHass(coordinator)
        release = asyncio.Event()
        async def read(*args, **kwargs):
            await release.wait()
        with patch.object(ha.lock_platform, 'refresh_after_command', side_effect=read):
            await entity.async_lock()
            self.assertTrue(entity.is_locking)
            self.assertFalse(entity.is_unlocking)
            with self.assertRaises(ha.exceptions.ServiceValidationError):
                await entity.async_lock()
            release.set()
            await asyncio.gather(*entity.hass.tasks)
        self.assertFalse(entity.is_locking)
        self.assertTrue(entity.is_locked)

class AdditionalWakeAndStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_command_invalidates_recent_wake(self):
        toyota = transport._CommandClient()
        toyota._recent_vehicle_wakes = {'ONE': client.time.monotonic()}
        with patch.object(client, '_execute_appsync_operation', AsyncMock(side_effect=client.RemoteCommandRejected('offline'))):
            with self.assertRaises(client.RemoteCommandRejected):
                await client.remote_request_24mm(toyota, 'ONE', 'door-lock')
        self.assertNotIn('ONE', toyota._recent_vehicle_wakes)
        self.assertFalse(toyota._pending_remote_commands)

    async def test_manual_wake_populates_same_recent_wake_cache(self):
        response = transport._Response(status=200, body={})
        session = Mock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.post = Mock(return_value=response)
        toyota = SimpleNamespace(_auth_headers=AsyncMock(return_value={}))
        with patch.object(client.aiohttp, 'ClientSession', return_value=session):
            await client.wake_vehicles(toyota, 'US', 'ONE', '24MM')
        self.assertIn('ONE', toyota._recent_vehicle_wakes)

    async def test_unrelated_status_does_not_end_engine_confirmation(self):
        vehicle, coordinator, api = FastRefreshTests().make([
            {'telemetry': {'fuelLevel': 80}}, status(running=True)])
        vehicle.apply_graphql_status(status(running=True))
        with patch.object(refresh, 'APPSYNC_STATUS_INTERVAL', 0):
            await refresh.refresh_after_command(coordinator, vehicle.vin, Command.EngineStart)
        self.assertEqual(2, api.graphql_get_vehicle_status.await_count)
