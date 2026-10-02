"""Remote-command regressions using synthetic responses; no Toyota connections."""

import asyncio
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import test_appsync_transport as transport
import test_button as ha
from custom_components.toyota_na import patch_client as client
client.WAKE_SETTLE_SECONDS = 0
from custom_components.toyota_na.patch_base_vehicle import RemoteRequestCommand as client_commands
import test_service_errors as services


class SubmissionTests(unittest.TestCase):
    def test_explicit_failure_wins_over_correlation_id(self):
        for payload, messages in (
            ({"returnCode": "ONE-RES-40001"}, []),
            ({"returnCode": "FAILED"}, []),
            ({"returnCode": "ONE-RES-10000"}, [
                {"responseCode": "ONE-GLOBAL-RS-00000"},
                {"responseCode": "ONE-GLOBAL-RS-40009", "description": "Vehicle not reachable"},
            ]),
        ):
            with self.subTest(payload=payload, messages=messages):
                execution = {"payload": {"correlationId": "id", **payload}, "status": {"messages": messages}}
                with self.assertRaises(client.RemoteCommandRejected):
                    client._require_remote_execution(execution)

    def test_missing_and_unfamiliar_codes_still_wait_for_callback(self):
        for code in (None, "ONE-RES-10000", "ONE-GLOBAL-RS-00000", "undocumented"):
            with self.subTest(code=code):
                execution = {"payload": {"correlationId": "id", "returnCode": code}}
                self.assertIs(execution, client._require_remote_execution(execution))

    def test_missing_correlation_id_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "correlation ID"):
            client._require_remote_execution({"payload": {"returnCode": "ONE-RES-10000"}})

    def test_submission_trace_does_not_store_identifiers_or_free_text(self):
        trace = {"events": []}
        client._require_remote_execution({
            "payload": {"correlationId": "private-id", "requestNo": "private-request", "returnCode": "private-token"},
            "status": {"messages": [{"responseCode": "private-vin", "description": "private-location"}]},
        }, trace)
        self.assertNotIn("private", json.dumps(trace))
        self.assertTrue(trace["events"][0]["correlation_id_present"])
        self.assertEqual(["unrecognized"], trace["events"][0]["response_codes"])


class RemoteOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def run_command(self, toyota, websocket=None):
        websocket = websocket or transport._WebSocket()
        with patch.object(client.aiohttp, "ClientSession", return_value=transport._WebSocketSession(websocket)):
            return await client.remote_request_24mm(toyota, "TESTVIN24", "engine-start")

    async def test_callback_timeout_is_uncertain_and_never_retries(self):
        toyota = transport._CommandClient()
        with patch.object(client, "_wait_for_remote_command_result", AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(client.RemoteCommandOutcomeUnknown) as error:
                await self.run_command(toyota)
        self.assertNotIn("accepted", str(error.exception))
        self.assertEqual(1, len(toyota.command_calls))
        trace = toyota._remote_command_history[-1]
        self.assertEqual("unknown", trace["outcome"])
        self.assertEqual("callback_timeout", trace["events"][-1]["event"])
        self.assertEqual({}, toyota._remote_command_traces)

    async def test_submission_timeout_is_uncertain_and_never_retries(self):
        toyota = transport._CommandClient()
        toyota.graphql_send_remote_command = AsyncMock(side_effect=TimeoutError)
        with self.assertRaises(client.RemoteCommandOutcomeUnknown):
            await self.run_command(toyota)
        toyota.graphql_send_remote_command.assert_awaited_once()
        self.assertEqual("submission_transport_error", toyota._remote_command_history[-1]["events"][-1]["event"])

    async def test_timeout_releases_vehicle_lock_for_later_user_command(self):
        toyota = transport._CommandClient()
        with patch.object(client, "_wait_for_remote_command_result", AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(client.RemoteCommandOutcomeUnknown):
                await self.run_command(toyota)
        result = await asyncio.wait_for(self.run_command(toyota), 1)
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(["unknown", "completed"], [t["outcome"] for t in toyota._remote_command_history])

    async def test_continuous_progress_obeys_total_deadline(self):
        async def progress(*args):
            await asyncio.sleep(0.001)
            return {"type": "data", "id": "subscription", "payload": {"data": {
                "onPostRemoteCallback": {"vin": "TESTVIN24", "status": "in_progress"},
            }}}
        with (
            patch.object(client, "REMOTE_COMMAND_TIMEOUT", 0.01),
            patch.object(client, "_receive_remote_socket_message", side_effect=progress) as receive,
        ):
            with self.assertRaises(client.RemoteCommandOutcomeUnknown):
                await asyncio.wait_for(client._wait_for_remote_command_result(
                    object(), "TESTVIN24", "subscription",
                ), 1)
        self.assertGreater(receive.call_count, 1)

    async def test_connection_loss_after_submission_is_uncertain(self):
        toyota = transport._CommandClient()
        with patch.object(client, "_wait_for_remote_command_result", AsyncMock(side_effect=RuntimeError("socket closed"))):
            with self.assertRaises(client.RemoteCommandOutcomeUnknown):
                await self.run_command(toyota)
        self.assertEqual(1, len(toyota.command_calls))

    async def test_no_command_sent_when_subscription_fails(self):
        toyota = transport._CommandClient()
        with patch.object(client, "_wait_for_remote_socket_event", AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(TimeoutError):
                await self.run_command(toyota)
        self.assertEqual([], toyota.command_calls)
        self.assertEqual("error", toyota._remote_command_history[-1]["outcome"])

    async def test_embedded_rejection_exits_before_waiting_for_callback(self):
        toyota = transport._CommandClient()
        toyota.graphql_request = AsyncMock(return_value={"executeRemoteCommand": {
            "payload": {"correlationId": "id", "returnCode": "ONE-RES-40001"},
        }})
        toyota.graphql_send_remote_command = lambda vin, command, region: client.graphql_send_remote_command(toyota, vin, command, region)
        with patch.object(client, "_wait_for_remote_command_result", AsyncMock()) as waiter:
            with self.assertRaises(client.RemoteCommandRejected):
                await self.run_command(toyota)
        waiter.assert_not_awaited()
        self.assertEqual("rejected", toyota._remote_command_history[-1]["outcome"])
        self.assertEqual("ONE-RES-40001", toyota._remote_command_history[-1]["events"][-1]["return_code"])

    async def test_records_callback_matching_without_identifiers(self):
        toyota = transport._CommandClient()
        callbacks = [
            {"vin": "private-vin", "status": "COMPLETED"},
            {"vin": "TESTVIN24", "appRequestNo": "private-request", "status": "COMPLETED"},
            {"vin": "TESTVIN24", "status": "IN_PROGRESS", "message": "private-text"},
            {"vin": "TESTVIN24", "status": "COMPLETED"},
        ]
        with self.assertLogs(client.__name__, level="DEBUG") as logs:
            await self.run_command(toyota, transport._CallbackWebSocket(callbacks))
        trace = toyota._remote_command_history[-1]
        self.assertEqual("completed", trace["outcome"])
        self.assertIn("ignored_vehicle", [e["event"] for e in trace["events"]])
        for value in (json.dumps(trace), " ".join(logs.output)):
            self.assertNotIn("private", value)
            self.assertNotIn("TESTVIN24", value)

    async def test_callback_rejection_preserves_reason(self):
        toyota = transport._CommandClient()
        with self.assertRaisesRegex(client.RemoteCommandRejected, "Vehicle rejected"):
            await self.run_command(toyota, transport._StatusWebSocket(["error"]))
        self.assertEqual("rejected", toyota._remote_command_history[-1]["outcome"])

    async def test_history_is_bounded(self):
        toyota = transport._CommandClient()
        for _ in range(12):
            await self.run_command(toyota)
        self.assertEqual(10, len(toyota._remote_command_history))
        trace = toyota._remote_command_history[-1]
        for _ in range(60):
            client._trace_event(trace, "ignored_vehicle")
        self.assertEqual(40, len(trace["events"]))

    async def test_cancellation_is_not_swallowed_or_retried(self):
        toyota = transport._CommandClient()
        with patch.object(client, "_wait_for_remote_command_result", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.run_command(toyota)
        self.assertEqual(1, len(toyota.command_calls))
        self.assertEqual("cancelled", toyota._remote_command_history[-1]["outcome"])
        self.assertEqual({}, toyota._remote_command_traces)


class SubmissionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_error_after_submit_is_uncertain_and_redacted(self):
        toyota = transport._CommandClient()
        trace = {"events": []}
        toyota._remote_command_traces = {"TESTVIN24": trace}
        session = MagicMock()
        session.__aenter__.return_value = session
        session.post.return_value = transport._Response(status=500, body={"message": "private-vin-token-location"})
        with (
            patch.object(client.aiohttp, "ClientSession", return_value=session),
            self.assertLogs(client.__name__, level="DEBUG") as logs,
        ):
            with self.assertRaises(client.RemoteCommandOutcomeUnknown):
                await client.graphql_request(toyota, "SendRemoteCommand", "mutation {}", {}, vin="TESTVIN24", raise_errors=True)
        self.assertEqual(1, session.post.call_count)
        self.assertEqual([{"event": "http_response", "status": 500}], trace["events"])
        self.assertNotIn("private", " ".join(logs.output))
        self.assertNotIn("TESTVIN24", " ".join(logs.output))


class ReachabilityTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, toyota, command):
        websocket = transport._WebSocket()
        with patch.object(client.aiohttp, "ClientSession", return_value=transport._WebSocketSession(websocket)):
            return await client.remote_request_24mm(toyota, "TESTVIN24", command)

    async def test_vehicle_command_wakes_vehicle_first_and_waits_longer(self):
        toyota = transport._CommandClient()
        with patch.object(client, "_wait_for_remote_command_result", AsyncMock(return_value={})) as waiter:
            await self.send(toyota, "engine-start")
        self.assertEqual(["pre-wake", "engine-start"], toyota.calls)
        self.assertEqual(client.VEHICLE_COMMAND_TIMEOUT, waiter.call_args.kwargs["timeout"])
        self.assertIn("pre_wake_sent", [e["event"] for e in toyota._remote_command_history[-1]["events"]])

    async def test_charge_command_keeps_default_wait_without_wake(self):
        toyota = transport._CommandClient()
        with patch.object(client, "_wait_for_remote_command_result", AsyncMock(return_value={})) as waiter:
            await self.send(toyota, "charge-stop")
        self.assertEqual(["charge-stop"], toyota.calls)
        self.assertIsNone(waiter.call_args.kwargs["timeout"])

    async def test_failed_pre_wake_still_sends_command_once(self):
        toyota = transport._CommandClient()
        toyota.wake_vehicles = AsyncMock(side_effect=RuntimeError("asleep"))
        with patch.object(client, "_wait_for_remote_command_result", AsyncMock(return_value={})):
            await self.send(toyota, "engine-start")
        self.assertEqual(["engine-start"], toyota.calls)
        self.assertIn("pre_wake_failed", [e["event"] for e in toyota._remote_command_history[-1]["events"]])

    async def test_auth_failure_during_pre_wake_sends_nothing(self):
        toyota = transport._CommandClient()
        toyota.wake_vehicles = AsyncMock(side_effect=client.AuthError("expired"))
        with self.assertRaises(client.AuthError):
            await self.send(toyota, "engine-start")
        self.assertEqual([], toyota.command_calls)

    async def test_wait_honors_longer_timeout(self):
        async def keepalive(*args):
            await asyncio.sleep(0.001)
            return {"type": "ka"}
        with (
            patch.object(client, "REMOTE_COMMAND_TIMEOUT", 0.005),
            patch.object(client, "_receive_remote_socket_message", side_effect=keepalive) as receive,
        ):
            loop = asyncio.get_running_loop()
            started = loop.time()
            with self.assertRaises(client.RemoteCommandOutcomeUnknown):
                await client._wait_for_remote_command_result(object(), "TESTVIN24", "subscription", timeout=0.05)
        self.assertGreaterEqual(loop.time() - started, 0.05)


class SubmissionErrorTests(unittest.IsolatedAsyncioTestCase):
    async def submit_error(self, error):
        toyota = transport._CommandClient()
        toyota.graphql_send_remote_command = AsyncMock(side_effect=error)
        with patch.object(client.aiohttp, "ClientSession", return_value=transport._WebSocketSession(transport._WebSocket())):
            with self.assertRaises(Exception) as raised:
                await client.remote_request_24mm(toyota, "TESTVIN24", "door-lock")
        return raised.exception, toyota._remote_command_history[-1]["outcome"]

    def response_error(self, status):
        return client.aiohttp.ClientResponseError(
            MagicMock(real_url="x"), (), status=status, message="Schedule overlaps [ONE-RES-40010]",
        )

    async def test_client_rejection_keeps_toyota_reason(self):
        error, outcome = await self.submit_error(self.response_error(400))
        self.assertIsInstance(error, client.aiohttp.ClientResponseError)
        self.assertIn("ONE-RES-40010", error.message)
        self.assertEqual("error", outcome)

    async def test_refused_connection_is_not_uncertain(self):
        error, outcome = await self.submit_error(client.aiohttp.ClientConnectorError(MagicMock(), OSError("refused")))
        self.assertIsInstance(error, client.aiohttp.ClientConnectorError)
        self.assertEqual("error", outcome)

    async def test_server_error_after_send_is_uncertain(self):
        error, outcome = await self.submit_error(self.response_error(503))
        self.assertIsInstance(error, client.RemoteCommandOutcomeUnknown)
        self.assertEqual("unknown", outcome)

    def test_malformed_messages_do_not_hide_accepted_command(self):
        for status in ({"messages": [None, "ok", {"responseCode": "ONE-RES-10000"}]}, "SUCCESS", None):
            with self.subTest(status=status):
                execution = {"payload": {"correlationId": "id"}, "status": status}
                self.assertIs(execution, client._require_remote_execution(execution, {"events": []}))
        with self.assertRaisesRegex(RuntimeError, "correlation ID"):
            client._require_remote_execution("SUCCESS")



def _opening(position=None, lock=None):
    return {
        **({"position": {"status": position}} if position else {}),
        **({"lock": {"status": lock}} if lock else {}),
    }


class AutoFixTests(unittest.IsolatedAsyncioTestCase):
    """Engine start mirrors the lock/close fixes Toyota's app sends."""

    async def make(self, *, auto_fix=True):
        import test_vehicle_behavior as behavior
        from custom_components.toyota_na.patch_vehicle import get_vehicles

        metadata = {**deepcopy(behavior.TWENTY_FOUR_MM_PHEV), "vin": "SYNTHETIC24MM"}
        toyota = SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[metadata]),
            get_telemetry=AsyncMock(return_value={}),
            graphql_get_vehicle_status=AsyncMock(return_value={}),
            remote_request_24mm=AsyncMock(),
            wake_vehicles=AsyncMock(),
            graphql_pre_wake=AsyncMock(), graphql_confirm_subscription=AsyncMock(),
            graphql_refresh_status=AsyncMock(),
            auth=SimpleNamespace(get_guid=AsyncMock(return_value="guid")),
        )
        vehicle, = await get_vehicles(toyota)
        vehicle._feature_flags = {
            **(vehicle._feature_flags or {}), "remoteCommands": 1, "remoteAutoFix": int(auto_fix),
        }
        return vehicle, toyota

    @staticmethod
    def status(door_lock="locked", window="closed", moonroof="closed"):
        return {"vehicleState": {
            "doors": {"driverSide": _opening("closed", door_lock)},
            "windows": {"driverSide": _opening(window)},
            "moonroof": _opening(moonroof),
        }}

    async def test_unlocked_doors_are_locked_with_engine_start(self):
        vehicle, toyota = await self.make()
        vehicle.apply_graphql_status(self.status(door_lock="unlocked"))
        await vehicle.send_command(client_commands.EngineStart)
        toyota.remote_request_24mm.assert_awaited_once_with(
            vehicle.vin, "engine-start", vehicle.region, autofix_commands=["door-lock"],
        )

    async def test_closed_secure_vehicle_sends_no_fixes(self):
        vehicle, toyota = await self.make()
        vehicle.apply_graphql_status(self.status())
        self.assertEqual([], vehicle.remote_autofix_commands())

    async def test_windows_and_moonroof_need_their_capabilities(self):
        vehicle, _ = await self.make()
        vehicle.apply_graphql_status(self.status(window="open", moonroof="open"))
        with patch.object(type(vehicle), "supports_command", lambda self, command: True):
            self.assertEqual(
                ["power-window-close", "sunroof-close"], vehicle.remote_autofix_commands(),
            )
        with patch.object(type(vehicle), "supports_command", lambda self, command: False):
            self.assertEqual([], vehicle.remote_autofix_commands())

    async def test_vehicles_without_auto_fix_send_none(self):
        vehicle, _ = await self.make(auto_fix=False)
        vehicle.apply_graphql_status(self.status(door_lock="unlocked"))
        self.assertEqual([], vehicle.remote_autofix_commands())

    async def test_other_commands_never_carry_fixes(self):
        vehicle, toyota = await self.make()
        vehicle.apply_graphql_status(self.status(door_lock="unlocked"))
        await vehicle.send_command(client_commands.DoorUnlock)
        toyota.remote_request_24mm.assert_awaited_once_with(vehicle.vin, "door-unlock", vehicle.region)

    async def test_popup_request_retries_once_with_fresh_fixes(self):
        vehicle, toyota = await self.make()
        vehicle.apply_graphql_status(self.status())
        toyota.remote_request_24mm.side_effect = [client.RemoteCommandNeedsAutoFix("lock first"), None]
        toyota.graphql_get_vehicle_status.return_value = self.status(door_lock="unlocked")
        await vehicle.send_command(client_commands.EngineStart)
        self.assertEqual(
            [[], ["door-lock"]],
            [call.kwargs["autofix_commands"] for call in toyota.remote_request_24mm.await_args_list],
        )

    async def test_popup_request_without_a_new_fix_is_not_resent(self):
        vehicle, toyota = await self.make()
        vehicle.apply_graphql_status(self.status(door_lock="unlocked"))
        toyota.remote_request_24mm.side_effect = client.RemoteCommandNeedsAutoFix("lock first")
        toyota.graphql_get_vehicle_status.return_value = self.status(door_lock="unlocked")
        with self.assertRaises(client.RemoteCommandNeedsAutoFix):
            await vehicle.send_command(client_commands.EngineStart)
        toyota.remote_request_24mm.assert_awaited_once()

    async def test_wake_uses_the_account_wake(self):
        vehicle, toyota = await self.make()
        self.assertTrue(vehicle.can_wake)
        await vehicle.wake()
        toyota.wake_vehicles.assert_awaited_once_with(vehicle.region, vehicle.vin, vehicle.api_generation)

    async def wake(self, statuses):
        sent = []

        class Response:
            def __init__(self, status):
                self.status = status
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def text(self):
                return json.dumps({"status": {"messages": [{"responseCode": "ONE-RS-10003"}]}})

        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def post(self, url, headers):
                sent.append((url, headers))
                return Response(statuses[len(sent) - 1])

        toyota = SimpleNamespace(_auth_headers=AsyncMock(return_value={
            "AUTHORIZATION": "Bearer t", "X-GUID": "g", "X-BRAND": "T", "x-region": "US",
        }))
        with patch.object(client.aiohttp, "ClientSession", return_value=Session()):
            try:
                await client.wake_vehicles(toyota, "US", "VIN1", "24MM")
            except RuntimeError as err:
                return sent, toyota._last_wake, err
        return sent, toyota._last_wake, None

    async def test_wake_sends_the_app_request_first(self):
        sent, last, error = await self.wake([200])
        self.assertIsNone(error)
        url, headers = sent[0]
        self.assertEqual("https://onecdn.telematicsct.com/v1/remote/route/wake", url)
        self.assertNotIn("X-BRAND", headers)
        self.assertNotIn("x-region", headers)
        self.assertNotIn("VIN", headers)
        self.assertEqual("T", headers["X-APPBRAND"])
        self.assertEqual([("app", 200)], [(a["variant"], a["status"]) for a in last["attempts"]])

    async def test_rejected_app_wake_retries_with_vehicle_headers(self):
        sent, last, error = await self.wake([400, 200])
        self.assertIsNone(error)
        self.assertEqual("VIN1", sent[1][1]["VIN"])
        self.assertEqual("24MM", sent[1][1]["X-GENERATION"])
        self.assertEqual(["app", "vehicle"], [a["variant"] for a in last["attempts"]])

    async def test_rejected_wake_reports_codes_without_identifiers(self):
        sent, last, error = await self.wake([400, 400])
        self.assertIn("ONE-RS-10003", str(error))
        self.assertNotIn("VIN1", json.dumps(last))

class UncertainServiceTests(services.CommandServiceErrorTests):
    async def test_followup_read_is_cancelled_on_integration_unload(self):
        action, args, operation = self.actions[0]
        with (
            patch.object(self.vehicle, operation, AsyncMock(side_effect=client.RemoteCommandOutcomeUnknown("unknown"))),
            patch.object(ha.button, "COMMAND_REFRESH_DELAY", 60),
        ):
            with self.assertRaises(ha.exceptions.HomeAssistantError):
                await action(*args)
            for callback in self.entry.unload_callbacks:
                callback()
            with self.assertRaises(asyncio.CancelledError):
                await self.hass.tasks[0]
        self.assertEqual(0, self.coordinator.refreshes)

    async def test_uncertain_command_schedules_one_read_and_keeps_error(self):
        for action, args, operation in self.actions:
            if operation != "send_command":
                continue
            with self.subTest(action=action, args=args):
                self.entry.data.clear()
                self.hass.tasks.clear()
                self.coordinator.refreshes = 0
                error = client.RemoteCommandOutcomeUnknown(client.REMOTE_COMMAND_UNKNOWN)
                with (
                    patch.object(self.vehicle, operation, AsyncMock(side_effect=error)) as send,
                    patch.object(ha.button, "COMMAND_REFRESH_DELAY", 0),
                    patch.object(ha.lock_platform, "COMMAND_REFRESH_DELAY", 0),
                    patch.object(ha.integration_runtime, "COMMAND_REFRESH_DELAY", 0),
                ):
                    with self.assertRaisesRegex(ha.exceptions.HomeAssistantError, "outcome is unknown"):
                        await action(*args)
                    await asyncio.gather(*self.hass.tasks)
                send.assert_awaited_once()
                self.assertEqual(1, len(self.hass.tasks))
                self.assertEqual(1, self.coordinator.refreshes)
                self.assertEqual(0, self.vehicle.refresh_requests)
                self.assertIn(ha.LAST_WAKE_AT, self.entry.data)
                self.assertFalse(self.lock._state_changing)


if __name__ == "__main__":
    unittest.main()
