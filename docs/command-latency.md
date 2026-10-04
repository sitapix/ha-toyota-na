# Remote-command latency — 2.10.0b5.post4

Lock, unlock, Start, and Stop previously waited five seconds after waking the
vehicle, then waited ten more seconds after completion before reading status.
Repeated presses could queue another copy of a command while the first one
was still running or its status had not caught up.

This release:

- Uses a two-second wake settling period. A successful wake for the same
  vehicle is reused for 30 seconds; a newly completed wake waits only the
  remainder of its settling period. Rejected and uncertain commands clear
  this cache. Real-world sleeping-vehicle reliability still needs observation.
- Reads AppSync lock/engine telemetry immediately after command completion or
  an uncertain result. If telemetry has not caught up, reads again up to four
  times total, two seconds apart, with a 20-second overall deadline. It does
  not request another vehicle wake or repeat the remote command. Unrelated
  telemetry does not end engine/lock confirmation early.
- Keeps command buttons' `command_pending` attribute true through the bounded
  follow-up read and rejects duplicate button presses. The lock's native
  locking/unlocking state follows the requested direction and blocks another
  lock action while pending. Errors and cancellation clean up pending state.
- Rejects duplicate active or queued AppSync commands through service entry
  points as well, while preserving serialization of distinct commands.
- Includes queue time in sanitized command diagnostics.

No optimistic physical state is assigned. Toyota's callback and subsequent
telemetry remain the sources of completion and vehicle-state information.
Legacy vehicle status polling and remote-start prerequisite checks are kept.

Validation: the regression suite, including local HTTP fixtures, passes on
Python 3.14.2. The integration imports with real Home Assistant Core 2026.9.4.
No physical lock, unlock, or engine command was sent during development.

Install through HACS and restart Home Assistant so its Python modules load the
new version. Rollback: download `v2.10.0b5.post3` through HACS and restart, keeping
the existing Toyota config entry and all entity IDs.
