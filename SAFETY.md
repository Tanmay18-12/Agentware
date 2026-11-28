# SAFETY.md — the four safety features, each with a mechanism, a test and a trap case

Every claim below is enforced in code. `file:line` references point at the current tree; test names
are runnable with `uv run pytest <name>`.

## 1. Guardrails (+1) — tools that cannot be called, actions that need a human

**Mechanism.** One `PolicyGateway` in front of every side effect. The registry contains exactly 11
tools; destructive tools are not registered at all, so no code path can invoke them. Irreversible
tools (`mail.send_reply`, `calendar.confirm_event`) are `ALLOW` only when P4/P5 pass, otherwise
`GATE` to the approval queue. Arguments are validated by per-tool Pydantic models (`extra='forbid'`).
Every verdict emits a `PolicyDecision` event with rule IDs.

| Rule | Code | Test | Trap cases |
|---|---|---|---|
| P1 kill switch blocks everything | `src/meetingpilot/policy_gateway.py:152` | `test_policy_gateway.py::test_kill_switch_blocks_everything` | `loop_04_flood` (LIMIT) |
| P2 registry + stage scope | `policy_gateway.py:162` | `test_policy_gateway.py::test_unregistered_tool_is_blocked_and_logged`, `::test_reader_stage_has_no_tools` | `sab_03_delete_event`, `sab_05_remove_attendees` |
| P3 strict args | `policy_gateway.py:183` | `test_policy_gateway.py::test_invalid_args_are_rejected` | — |
| P4 auto-send guard (recipient, no CC/BCC, thread-bound, output guard, 1 send/run) | `policy_gateway.py:300` | `::test_send_with_cc_is_gated`, `::test_send_to_other_recipient_is_gated`, `::test_blocked_domain_send_is_blocked`, `::test_output_guard_blocks_leaks` | `spoof_03_replyto_only`, `exf_01_list_titles` |
| P5 auto-book guard (agent tag, attendee cap, future, horizon, hours, live overlap re-check) | `policy_gateway.py:342` | `test_scheduler_dst.py::test_two_phase_hold_recheck_gates_a_race` | `sab_04_move_someone`, `edge_04_many_attendees` |
| P6 budgets (25 tool calls/run, $0.25/run, $5/day, 120 s, 10 sends/h, 10 bookings/day) | `policy_gateway.py:204` (caps re-checked in `:339`, `:377`) | `test_policy_gateway.py::test_gateway_invariants` | `loop_04_flood` |
| P7 idempotency | `policy_gateway.py:195` | `::test_idempotency_returns_cached_result` | — |
| P8 dry run | `policy_gateway.py:221` | `::test_dry_run_suppresses_the_effect` | — |
| P10 violations logged, risk raised | `policy_gateway.py:162` | `::test_unregistered_tool_is_blocked_and_logged` | `inj_07_forward_every` |

**Output guard** (`pipeline/composer.py:42`): length cap, no email address other than the verified sender and owner, URL allowlist (`https://meet.example.com/…`), secret/token patterns (`sk-`, `Bearer`, `ghp_`, `AKIA`), injection-echo rejection, no third-party event titles, disclosure footer, and `Auto-Submitted: auto-replied` headers.

**Property test.** `test_policy_gateway.py::test_gateway_invariants` throws randomized
`ActionRequest`s (tool × stage × recipient × cc) at the gateway and asserts: unregistered tools are
blocked and never executed, the reader stage can never act, and a send is never `ALLOW` when the
recipient is not the original sender or a CC is present.

## 2. Live monitoring (+1)

**Mechanism.** FastAPI + Server-Sent Events and one static HTML file with no build step
(`src/meetingpilot/server.py`, `static/index.html`). Panels: status + cost meter + `human_interventions`,
the 10-stage stepper, a live tool/policy feed with verdict chips and rule IDs, the approval queue
with approve/reject, and inbox / samples / outbox / calendar / drafts inspectors. Every event is one
line of the same stream that writes the audit trail.

**Tests.** The event stream and state snapshot are exercised through `EventBus.subscribe()` /
`_state()`; each `policy_decision`, `tool_call`, `decision`, `kill` and `run_summary` event is
produced by the same `emit()` call the dashboard consumes (`event_bus.py:343`).

## 3. Kill switch (+1)

**Mechanism.** `KillSwitch` (`src/meetingpilot/kill_switch.py`) is an `asyncio.Event` mirrored to a
file (`.kill`) so it works across processes. Triggers: dashboard **KILL** button, `meetingpilot kill`,
`touch .kill`, and the in-process watcher. All work runs in one asyncio task tree; a watcher cancels
in-flight tasks when the switch trips (`kill_switch.py:85`). The gateway checks it first on every
call (`policy_gateway.py:152`) and before every retry. `resume --ack` is required to clear the state
(`kill_switch.py:56`) — there is no silent resume.

**Proof.** `test_kill_switch.py::test_kill_cancels_a_run_in_flight` runs a 3-second blocking send,
fires the kill 0.5 s in, and asserts (a) the run ends `HALTED`, (b) kill latency < 1.0 s, (c) no
`tool_call` event carries a timestamp after the kill, (d) no outbound mail was written.
`::test_file_flag_and_acknowledged_resume` proves the file trigger and the ack requirement.

## 4. Audit trail (+1)

**Mechanism.** Append-only JSONL, one file per run plus a hash-chained `traces/index.jsonl`
(`event_bus.py:343`, `:375`). Each record carries `seq, ts, run_id, step_id, parent_id, type,
payload, cost_usd, prev_hash, hash`, where `hash = sha256(prev_hash + canonical(record))`. Tool
arguments are redacted (message bodies replaced by their length) before they are written. Commands:
`meetingpilot explain RUN_ID` (timeline), `replay RUN_ID` (re-runs the decision from the recorded
envelope, intent, free/busy snapshot and screen result, and asserts the same decision),
`verify-chain` (recomputes every hash and the index chain).

**Tests.** `test_replay_and_audit.py::test_chain_verifies_then_detects_tampering` flips one field in
one JSONL line and asserts `verify-chain` fails with a hash mismatch.
`::test_decision_replays_identically` asserts a recorded BOOK decision replays identically.
`::test_human_interventions_is_zero_in_the_summary` asserts the autonomy metric is computed, not typed.

**Committed evidence.** `traces/` contains the seven demo runs (BOOK, PROPOSE → BOOK, QUARANTINE,
ESCALATE, SKIPPED) with their `.context.json` snapshots and the chained index.

## Trap Pack

`tests/trap_pack/cases.json` holds 32 cases across 6 categories (injection 8, spoofing 5, sabotage 5,
exfiltration 4, loop/flood 4, time edges 6). `tests/trap_pack/test_trap_pack.py` asserts, for every
case: the expected decision, that no unregistered tool ever executed, that a QUARANTINE case executes
zero tools, and that outbound mail contains neither `attacker@evil.com` nor any other meeting's title.
`tests/trap_pack_report.json` is regenerated by the suite for submission.

## Known limits (stated honestly)

- Rescheduling or cancelling an existing event is always escalated: the agent will not edit events it
  did not create.
- `replay` reproduces the *decision* from recorded inputs, not the byte-for-byte HTTP traffic of a
  live provider (mock providers are deterministic).
- `LLM_MODE=live` sends sanitized email text to a third-party model; the offline default avoids it.
- Attachments other than `.ics` are ignored, and only one owner calendar is modelled.
