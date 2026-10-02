"""Remote-command regressions using synthetic responses; no Toyota connections."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_appsync_transport as transport
import test_button as ha
from custom_components.toyota_na import patch_client as client
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
        self.assertIn("ignored_request_number", [e["event"] for e in trace["events"]])
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
