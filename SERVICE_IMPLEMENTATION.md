# Persistent service implementation

`python -m ai_dlc serve --config <service-v1.json>` starts one long-lived Agent
process. The same file is accepted through `--service` for consistency with
`validate-service`. `--once` acquires ownership, replays durable state, processes
one inbox/scheduler cycle, checkpoints, and exits without opening a socket.

The bootstrap also constructs the model protocol registry and shared session
concurrency controller without making model calls. Until the approval/tool loop
in issue #9 is connected, `MODEL_WORKFLOW_UNCONNECTED` keeps readiness false even
when a protocol adapter is installed. See [model contracts](MODEL_IMPLEMENTATION.md).

## Lifecycle and recovery

The service acquires the existing `FileJournal` instance lock before recovery or
HTTP intake. A second process fails with `STATE_LOCKED`; it never steals a lock
based on a PID or timestamp. Startup enumerates identities from their first
immutable journal records, verifies each journal and blob chain, and rebuilds its
snapshot. A previous checkpoint other than `stopped` is reported as an unclean
restart.

Recorded executions in `reserved`, `dispatching`, `running`, or `uncertain` are
never relaunched from a checkpoint. They enter `RECOVERY_ACTION_REQUIRED` until a
configured runner can inspect the recorded identity and reconcile the effect.
Pending webhook deliveries remain in the durable inbox and are scheduled again;
their workflow event IDs and processed receipts make redelivery idempotent.

## Scheduling and shutdown

The scheduler preserves FIFO order inside a repository and rotates across
repositories. Global, per-repository, model, and provider limits are enforced in
the same process. Queue wait and human wait consume no active execution time.
Status reports include the capacity reason and next action for pending work.

SIGINT/SIGTERM first closes webhook intake and scheduler admission, then requests
cooperative cancellation and waits for the configured termination grace. Active
and never-started work left at the deadline is checkpointed with
`SHUTDOWN_INTERRUPTED`; it is not marked complete. A checkpoint or inbox I/O
failure closes dispatch and readiness immediately.

## HTTP probes

- `POST /hooks/github` authenticates and commits a delivery before returning 202.
- `GET /health/live` reports whether the service loop is alive.
- `GET /health/ready` reports whether configured credentials, model/runner
  prerequisites, durable state, recovery, and GitHub processing are ready.

Liveness can be 200 while readiness is 503. This is intentional: a process that
can explain a missing runner, model adapter, credential, or recovery observation
must not claim that it can dispatch work. Reverse-proxy mode only binds the
configured loopback address. Direct mode loads the configured TLS certificate
and key and requires TLS 1.2 or newer.

No database, container, worker daemon, or message broker is introduced. The
operator-owned state directory remains the durable source of truth.
