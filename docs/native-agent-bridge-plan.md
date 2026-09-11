# Native Claude and Codex bridge plan

Revision 4, 2026-09-11. Adds bounded startup holding for the interval between
snapshot discovery and Codex rollout readiness. Target release: dch 1.16.0.

## Outcome and limits

Normal dch Codex sessions become discoverable through Claude's native
`ListAgents`. Claude uses native `SendMessage`. Codex uses
`dch --agent-list [--json]` and `dch --agent-send NAME MESSAGE...` from its
shell tool. Both directions use authenticated Claude peer sockets, never PTY
input. A Claude reply remains routable after Codex's send command exits.

No new dependencies. C/POSIX product code lives in the installed dch binary.
The installed Codex CLI owns queue delivery. Both build variants include the
same bridge; lite excludes only the terminal mirror. Python remains existing
test tooling only.

Fable verified ordinary Codex 0.154.0 delivery in 75 ms, TUI pickup in 5.3 s,
and a visible reply in 11 s. These are observations, not latency guarantees.
The user accepts this delay. We will not start an app-server, rewrite Codex
arguments, use `--remote`, or wrap the PTY child.

## Ownership and identity

The existing master starts one sidecar child when the inner command basename
is `codex`. The same seam covers attach-created and `--spawn` sessions. The
sidecar runs from the same installed executable. It owns the Claude record,
key and listener, but never owns the Codex process or its terminal.

The master retains a close-on-exec pipe writer. The sidecar owns the reader.
EOF terminates the sidecar and removes its artifacts. A successful master
restart closes the old writer and creates a replacement sidecar; a failed
restart leaves the old sidecar intact. Close inherited PTY, listener and
client descriptors in the child. Reap sidecar exits without mistaking them
for Codex termination. Sidecar failure must not kill or block the PTY session.
Before orderly replacement or shutdown, the old sidecar attempts `dropped`
receipts for accepted messages within one shared 1.2-second cleanup deadline.
Unexpected process death is not durable and there is no disk spool.

The peer PID is the sidecar PID. Its stable peer name is `DCH_SESSION`, not
the changing Codex title. Obtain `procStart` with the exact command Claude
uses: `LC_ALL=C TZ=UTC ps -o lstart= -p PID`, using fork/exec, never a shell.

Reuse dch's existing Codex snapshot traversal. A fresh dch Codex launch
gets a reserved random session-incarnation environment marker before forkpty.
Match both DCH_SESSION and this marker in its shell snapshot; use the filename
UUID. The marker survives master re-exec and distinguishes reused names and
different socket directories. Do not trust embedded CODEX_THREAD_ID values:
they can be inherited from the launching agent and describe a different thread.
Require exactly one distinct UUID across matching snapshots and refresh that
check on delivery. Multiple snapshots for that same UUID are harmless; two
different UUIDs are ambiguous regardless of timestamp. A nested Codex run can
inherit the marker, so "newest" is not proof of ownership. Do not add rollout
or SQLite readers to guess. Startup resume can work with a unique matching
snapshot. A later /new or /resume that leaves conflicting snapshots refuses
delivery; use a fresh dch session. Do not publish a legacy session if current
ownership cannot be proved.

Publish once a current UUID is known. The snapshot can exist before the first
turn materializes its rollout. If `codex queue` returns the exact
missing-rollout error for the bound UUID, hold the message and retry that UUID
every five seconds. This does not start the first turn. Never fall back to
terminal injection or another thread.

## Native transport

Publish the proven Claude peer record, socket and socket-hash key. Include
`peerProtocol:1`, `peerFeatures:["reply_across_default_dirs"]`, `sessionId`,
`pidDomain`, `procStart`, `messagingSocketPath`, and the stable name with
`nameSource:"user"`. Do not advertise idle notifications or artifacts.

Read two bounded newline-delimited JSON frames: authentication, then payload.
Use a small bounded JSON parser and encoder, local SHA-256 with known-answer
checks, and `getentropy` with a checked `/dev/urandom` fallback. No library or
shell dependency is needed.

Validate authentication, UTF-8/JSON, duplicate security fields, string content,
size limits, sender/reply identity, and optional target session ID. Reject
attachments. Ignore harmless unknown fields and unrecognized priority values.
Do not exact-match feature arrays or reject future optional metadata.

Serve serially with bounded socket and subprocess deadlines. Keep at most 16
accepted inbound messages in FIFO order and one queue subprocess active. Invoke
`codex queue --thread UUID --message TEXT` with argv. Receipt `delivered` means
the queue accepted the message, not that the model processed it. Queue failure
produces an authenticated `refused` receipt. A timeout or signaled subprocess
has an uncertain result and produces `dropped`, with no retry. Receipt `held`
means this sidecar retained the message after a confirmed missing-rollout
failure; it later sends a terminal receipt. Messages behind a held item are
held immediately. The listener remains responsive while the FIFO waits. No
message log, queue database writes, or worker pool.
Mark the serial throughput limit with a `ponytail:` comment.

## Codex sends and replies

Reuse the existing Claude record discovery path for native list/send and
display-name loading. Share parsing and traversal, while preserving the
display loader's best-effort behavior. Messaging additionally validates live
PID, exact process start, record/key consistency and safe sockets. Resolve
exact peer names and fail on ambiguity. Respect applicable Claude/Codex
home directories rather than assuming every installation uses defaults.

`--agent-send` inside a bridged Codex routes through that session's persistent
sidecar. Native user frames use its persistent `from:"uds:..."` address.
An authenticated local request lets the sidecar correlate the matching native
receipt back to the waiting command. While waiting, the sidecar must accept
the incoming receipt connection; do not deadlock waiting on its own listener.
Keep one pending outgoing request and reject competing requests clearly.
Later native user replies go through the ordinary Codex queue path.

Without a unique live source sidecar, sending fails with actionable guidance.
There is no transient sender that claims durable two-way messaging. Receipt
matching checks both `orig_msg_id` and target identity. Recognize `delivered`,
`refused`, `dropped`, `held`, `denied`, and `expired`. Treat `held` as accepted
pending work, not failure, so the sender does not resend. Report every other
non-delivery without automatically duplicating the message.

Codex shell tools authenticate list and send requests to the marker-bound
persistent sidecar. The sidecar performs strict peer process-start validation
and target lookup. This keeps `/bin/ps` outside the Codex sandbox while the
short-lived CLI still proves possession of the source record key.

## Files and security

Use private directories and owner-only keys and sockets. Claude's real record
files can be `0644`; allow same-owner regular records with no group/world
write permission. Do not require `0600` for reading a record. Reject symlinks,
unsafe ownership, wrong process identity and invalid or overlong UDS paths.
Use atomic file publication and unlink only matching device/inode identities.
Do not expose tokens or message text in diagnostics.

Expected product seams are `master.c` for lifecycle, `dch.c` for CLI/shared
lookup, a bridge C/header pair for protocol, and configure/Makefile selection.
Both variants link the same bridge with no new user-facing configuration
matrix. Existing package formulas continue to install one binary and a man
page with no dependency changes.

Replace the old Python proof and its tests once the C integration covers the
same contract. Update README and man page for actual normal-launch behavior,
startup holding, stable names, accepted queue delay and any limits on sessions
started before this version.

## Acceptance before release

- Deterministic checks cover normal launch, unchanged Codex argv, stable names,
  current/stale UUIDs, both directions, persistent replies, refusal receipts,
  auth and malformed frames, harmless future fields, real record modes,
  ambiguity, timeouts, cleanup/replacement safety, restart and child reaping.
- Full and lite builds and existing suites pass. The bridge runs in both;
  lite's terminal-mirror exclusion is verified.
  Existing hot-path performance gates remain unchanged. No invented list/send
  percentile budgets. Record one live latency measurement.
- A fresh ordinary Codex session under the candidate executable completes its
  first turn. A fresh Claude session discovers it with native `ListAgents`,
  sends a unique nonce with native `SendMessage`, and receives Codex's reply
  through `dch --agent-send`. Claude then replies to the persistent sender
  after that command has exited, and Codex receives it without terminal input.
- Verify that a native message sent after discovery but before rollout
  readiness receives `held`, then `delivered` automatically after the user
  starts the first turn, without a resend. Kill only the proof's own sessions
  and confirm artifact cleanup. Complete CI and installed-binary proof before
  publishing v1.16.0. Release tracking lives in ledger #016.

## Self-review

Revision 2's premise that standalone Codex could not consume `codex queue`
was false. That error introduced the daemon, PTY wrapper, signal forwarding,
remote preservation, title tracking and worker pool. All are removed.
Immediate pickup is explicitly no longer required.

Two details remain necessary beyond the lean one-way sketch: a launch marker
rejects stale incarnations with the same dch name, and the persistent sender
address makes real two-way replies work. The marker can be inherited by child
Codex runs; therefore distinct matching UUIDs refuse delivery. Rollout source
metadata distinguishes some subagents but does not prove active terminal
ownership, so automatic retargeting is not supported. These are acceptance
requirements, not future extensibility. Cross-host delivery, attachments,
broadcasting, custom remote Codex transports and general automatic retries are
out of scope. The only retry is the exact missing-rollout diagnostic for the
UUID already pinned to the accepted item.
