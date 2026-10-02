# Remote-command reliability beta

Version **2.9.3b1**, based on orienw/ha-toyota-na commit
`7e4c121e549e0b787daf58500e9c0672bdd379d5` (v2.9.2 code).

## Changes

- A correlation ID no longer hides a known Toyota rejection code in
  `payload.returnCode` or any `status.messages[].responseCode`.
  Recognized failures are explicit ERROR/FAILED/FAILURE/REJECTED values and
  Toyota ONE-* 4xxxx/5xxxx codes. Undocumented codes remain diagnostic facts;
  they are not guessed to mean success or failure. Completion still requires
  a matching callback.
- A callback timeout or lost connection after submission reports an **unknown
  vehicle outcome**, without claiming Toyota accepted or completed the command.
- Buttons, locks, and command services schedule a cloud status read following
  an uncertain command. The error remains visible. Recording the attempted
  wake prevents the usual scheduled wake for that vehicle during the follow-up.
  There is no automatic resend, extra Refresh command, or optimistic engine state.
- Diagnostics include the last ten AppSync operations, with at most forty
  events each: connection/subscription readiness, submission codes, callback
  matching decisions, outcome, and elapsed time. This history is memory-only
  and is cleared by integration reload or HA restart.
- The new history and its debug logs use an allowlist. They omit VINs,
  account identifiers, authentication, location, request/correlation IDs,
  and free-text Toyota messages. Existing vehicle diagnostics are still
  subject to the integration's existing redaction rules; review the complete
  download before sharing it publicly.

The wire command remains `engine-start` for supported 24MM vehicles.
There is no speculative switch to climate-start, added pre-wake, or longer
timeout. An engine-running sensor alone cannot prove a remote-start session.

## Validation and remaining uncertainty

The offline suite uses synthetic protocol responses and Home Assistant stubs.
It covers rejection with a correlation ID, optional request numbers, callback
filtering, timeout/connection loss, no replay, cancellation, bounded diagnostics,
and follow-up reads through entity and service entry points. Run:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests
```

This proves the software regressions are addressed, **not** that remote start
works on a real vehicle. No vehicle commands were used to develop this beta.
The cause of the original timeout remains unconfirmed.

Validation on October 2, 2026: **324 tests passed** with Python 3.14.2.
The integration and changed platforms also imported successfully against a
real Home Assistant Core 2026.9.1 environment. The live instance is 2026.9.4;
runtime behavior there still requires installation and verification.

## Install and roll back

1. Record the installed HACS repository and version and create a Home Assistant
   backup before switching code. Keep the Toyota integration entry in
   Settings > Devices & services, preserving account/device/entity IDs.
2. In HACS, remove the installed repository download, add
   `https://github.com/sitapix/ha-toyota-na` as an Integration custom repository,
   enable prereleases, and download **v2.9.3b1** before restarting Home Assistant.
3. After restart, confirm the existing Toyota entry is loaded and the same
   buttons, lock, and sensors are available. Merely installing this package
   does not submit a test command; existing scheduled-wake options still apply.
4. To undo this beta, switch HACS back to `orienw/ha-toyota-na` **v2.9.2** and
   restart, retaining the same Toyota config entry. Do not restore the entire
   HA backup just to replace integration files.

## One controlled vehicle test

When the owner is present and the vehicle is safely parked outdoors, with
Toyota's normal remote-start prerequisites satisfied:

1. Make one deliberate HA Start attempt. Do not retry an unknown outcome until
   the vehicle's actual state is checked.
2. Observe the vehicle, the HA error (if any), and the subsequent cloud state.
3. Download the Toyota integration diagnostics **before reloading or restarting**.
   The `remote_commands` section is collected even with debug logging disabled.
4. A `submission_response` rejection identifies an immediate Toyota failure.
   `awaiting_callback` followed by `callback_timeout` distinguishes silence from
   `in_progress` callbacks. Ignored vehicle/request events identify filtering.
   A completed callback confirms Toyota's reported completion, which should
   still be compared with physical behavior.
5. If HA fails, compare with one Toyota-app attempt under the same conditions.
   Use those observations to decide whether pre-wake or protocol changes are
   justified. Avoid automatically trying alternate commands.

Protocol reference: [2026 RAV4 AppSync report](https://github.com/widewing/ha-toyota-na/issues/192).
HA coordinator reference: [Fetching data](https://developers.home-assistant.io/docs/integration_fetching_data/).
