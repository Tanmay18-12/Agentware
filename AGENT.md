# AGENT.md — how MeetingPilot decides and acts

Track **T1 (Inbox & Calendar)**. One workflow, finished: **meeting-request email → free/busy check →
booked slot → threaded reply → filed follow-up**, with a code-enforced policy plane in front of every
side effect.

## Invariants

1. **LLM proposes, code disposes.** `pipeline/reader_llm.py` is the only component that sees raw
   email text. It has zero tools and must return a `MeetingIntent` that validates against a strict
   Pydantic model (`extra='forbid'`). Every timestamp, slot and action is computed by Python.
2. **One choke point.** `Orchestrator` never imports a provider. Every side effect becomes an
   `ActionRequest` evaluated by `PolicyGateway.execute()` (`policy_gateway.py:140`).
3. **Fail closed.** Exception, schema failure, low confidence, ambiguity or a blocked action ends in
   `ESCALATE` (draft + queue) or `QUARANTINE` (no actions) — never "proceed anyway".
4. **Offline first.** `MOCK_MODE=1`, `LLM_MODE=cassette` are the defaults; no API key and no network
   are required for the full demo.
5. **Kill switch everywhere.** Checked before every LLM call, every tool call and between retries.

## The 10 stages

| # | Stage | Kind | Output | Failure path |
|---|---|---|---|---|
| 1 | Ingest | code | `EmailEnvelope` (HTML→text, hidden-text flag, 8 000-char body cap, Message-ID dedupe) | malformed → QUARANTINE |
| 2 | Prefilter | code | skip auto-submitted / bulk / no-reply / self / blocked domain / sender flood | skip + log |
| 3 | Screen | code | injection score, spoof flags, auth failures | score ≥ 0.5 → QUARANTINE before any model call |
| 4 | Reader LLM | zero tools | `MeetingIntent` JSON | invalid JSON twice → ESCALATE |
| 5 | Validate | code | bounds, future dates, DST gaps, attendee cap | hard fail → CLARIFY |
| 6 | Schedule | code | ranked `SlotCandidate`s from free/busy + prefs | provider error after retries → ESCALATE |
| 7 | Decide | code | BOOK / PROPOSE / CLARIFY / ESCALATE / FILE / QUARANTINE | default ESCALATE |
| 8 | Act | code via gateway | hold → re-check → confirm, reply, label, follow-up | any block → draft + approval queue |
| 9 | Verify | code | read back event, confirm no overlap, compensate if wrong | mismatch → cancel own event + alert |
| 10 | Record | code | run summary, minutes saved, cost, `human_interventions = 0` | n/a |

## Dual-LLM quarantine

- The Reader receives sanitized text only (`body_text` capped at 8 000 chars, hidden HTML blocks and
  zero-width characters stripped before the call).
- It returns JSON; a schema failure gets exactly **one** repair attempt, then ESCALATE.
- The Act stage never sees the raw message: it sees validated fields (`MeetingIntent`) and the
  original envelope's identity headers only.
- Cassette mode replays a recorded intent keyed by `sha256(message_id)`, so demos are deterministic
  while the live path is unchanged. Stub mode is a rule-based extractor used by the test suite.

## Tool registry (the only callable tools)

| Tool | Tier | Stages | Hard constraints |
|---|---|---|---|
| `mail.list_new`, `mail.read` | READ | ingest, act | configured mailbox only |
| `calendar.freebusy` | READ | schedule, act, verify | busy intervals only — never titles or attendees |
| `calendar.get_own_event` | READ | act, verify | agent-tagged events only |
| `calendar.create_hold` | REVERSIBLE | act | tentative, no invites, agent tag |
| `calendar.cancel_own_hold` | REVERSIBLE | act, verify | agent-tagged events only |
| `mail.draft_reply`, `mail.label`, `followup.file` | REVERSIBLE | act | local files |
| `calendar.confirm_event` | IRREVERSIBLE | act | P5 must pass, otherwise GATE |
| `mail.send_reply` | IRREVERSIBLE | act | P4 must pass, otherwise GATE |

**Not registered, therefore not callable:** `mail.delete`, `mail.forward`, `mail.archive_others`,
`calendar.delete_event`, `calendar.cancel_others`, `calendar.update_others`, `files.delete`,
`payments.charge`, `shell.exec`. Calling one emits a `policy_violation` event and returns `BLOCK (P2, P10)`.

Stage scoping is enforced too: `reader_llm`, `screen`, `prefilter`, `validate` and `record` have
**no** tools; `schedule` has only `calendar.freebusy`.

## Policy rules

`P1` kill switch · `P2` registry + stage scope · `P3` strict args · `P4` auto-send guard · `P5` auto-book
guard · `P6` budgets and rate limits · `P7` idempotency (`sha256(message_id, tool, canonical args)`) ·
`P8` dry run · `P9` every verdict emits a `PolicyDecision` event · `P10` unregistered calls are
violations. `PX` (documented extension) marks a provider failure that was failed closed after retries.

## Failure-mode matrix

| Failure | Behaviour | Evidence |
|---|---|---|
| Malformed email / oversize body | QUARANTINE, run still recorded | `pipeline/ingest.py`, trap pack |
| Prompt injection (hidden, visible, zero-width) | QUARANTINE at stage 3, zero tool calls | `pipeline/screen.py:111` |
| Spoofed From / Reply-To / SPF / DKIM | QUARANTINE (score ≥ 0.5) or ESCALATE | `policy_gateway.py:300` |
| Low confidence / ambiguous time / DST gap | CLARIFY with one question | `pipeline/validator.py:119` |
| Provider 429 / 500 / timeout | 3 attempts with backoff, then BLOCK → ESCALATE | `policy_gateway.py:140` |
| Concurrent booking (check-then-act race) | cancel own hold, retry next candidate (max 2) | `orchestrator.py:525` |
| Post-commit mismatch | cancel own event, mark `COMPENSATED` | `orchestrator.py:788` |
| Kill switch mid-run | tasks cancelled, pending work halted, `HALTED` summary | `kill_switch.py:85` |
| Duplicate delivery | exactly-once effects via P7 + Message-ID dedupe | `event_bus.py`, `test_replay_and_audit.py` |

## Data model

`EmailEnvelope` → `MeetingIntent` → (`SlotCandidate` → `Decision`) → `ActionRequest` →
`PolicyDecision` → `RunEvent`. All are Pydantic v2 models with `extra='forbid'` in `schemas.py`;
`RunEvent` carries `prev_hash` / `hash` for the audit chain.

## Running it

```bash
uv sync --extra dev
make demo        # seed + 7 end-to-end runs, offline
make test        # 147 tests including the 32-case trap pack (also runs in CI)
make serve       # http://127.0.0.1:8000
```
