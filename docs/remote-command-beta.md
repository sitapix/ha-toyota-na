# Remote-command reliability beta

Version **2.10.0b5.post1**: orienw/ha-toyota-na **v2.10.0b5**
(`15c8bdbf3521297a0a0cf99844f582dc02636ea9`) plus the remote-command fixes
below, which upstream does not include. Earlier builds of these fixes were
published on top of v2.9.2 as 2.9.3b1 and 2.9.3b2.

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

- vehicle commands (start, stop, lock, unlock, hazards, find) send
  the same best-effort pre-wake that Refresh uses, after the callback
  subscription is ready and immediately before the command. A failed pre-wake
  is recorded (`pre_wake_failed`) and the command is still sent once.
- vehicle commands wait up to 180 seconds for Toyota's callback
  instead of 60, because a sleeping vehicle can take about two minutes to act.
  Charge commands keep the 60-second wait.
- an HTTP 4xx response or refused connection while submitting is a
  definite failure that keeps Toyota's reason (for example a charge-schedule
  conflict) instead of being reported as an unknown outcome.
- malformed `status`/`messages` entries no longer crash handling of
  a submission Toyota already accepted.

The wire command remains `engine-start` for supported 24MM vehicles; there is
no speculative switch to climate-start. An engine-running sensor alone cannot
prove a remote-start session.

## Matching Toyota's app (2.10.0b5.post2)

Traced from the decompiled Toyota app 3.5.0 (`com.toyota.oneapp`):

- The app wakes the vehicle with `POST /v1/remote/route/wake` when it opens
  and every five minutes while in use, never with the GraphQL `postPreWake`
  this integration used. Vehicle commands now send that wake, wait a few
  seconds, then submit. A **Wake Vehicle** button sends the same wake so a
  dashboard can warm the vehicle up when it is opened.
- The app accepts any `onPostRemoteCallback` for the VIN; its `appRequestNo`
  is not the submission's `requestNo`. Remote commands no longer drop
  callbacks whose request number differs. Charge-schedule writes, whose
  numbers do match, keep matching.
- On vehicles with the `remoteAutoFix` feature, engine start carries the
  app's auto-fix list: `door-lock` when a door is unlocked,
  `power-window-close` and `sunroof-close` when an opening is open and the
  vehicle supports closing it. When Toyota answers `popup_required`, the
  integration re-reads the vehicle and retries once with any new fixes.

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

Validation on October 2, 2026: **405 tests passed** (2.10.0b5.post1) with Python 3.13.
The integration and changed platforms also imported successfully against a
real Home Assistant Core 2026.9.1 environment. The live instance is 2026.9.4;
runtime behavior there still requires installation and verification.

## Install and roll back

1. Record the installed HACS repository and version and create a Home Assistant
   backup before switching code. Keep the Toyota integration entry in
   Settings > Devices & services, preserving account/device/entity IDs.
2. In HACS, remove the installed repository download, add
   `https://github.com/sitapix/ha-toyota-na` as an Integration custom repository,
   enable prereleases (otherwise HACS installs the old v2.9.2 code from
   `main` without warning), and download **v2.10.0b5.post1** before restarting Home Assistant.
3. After restart, confirm the existing Toyota entry is loaded and the same
   buttons, lock, and sensors are available. Merely installing this package
   does not submit a test command; existing scheduled-wake options still apply.
4. To undo this beta, switch HACS back to `orienw/ha-toyota-na` **v2.10.0b5** (or **v2.9.2**) and
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
5. Note when the vehicle physically starts, measured from the button press.
   `pre_wake_sent` followed by `in_progress` callbacks and a late `completed`
   means the vehicle was slow to wake. Also record the engine status
   `lastUpdateBy`/`startTime` from the downloaded diagnostics.
6. If HA fails, compare with one Toyota-app attempt under the same conditions.
   Avoid automatically trying alternate commands.

Protocol reference: [2026 RAV4 AppSync report](https://github.com/widewing/ha-toyota-na/issues/192).
HA coordinator reference: [Fetching data](https://developers.home-assistant.io/docs/integration_fetching_data/).
