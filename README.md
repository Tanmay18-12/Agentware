# MeetingPilot

An unattended agent that turns a meeting-request email into a booked calendar slot, a threaded
reply and a filed follow-up — and refuses to do anything it should not.

Built for **Hackathon Raptors · Agentware 2026 · Track T1 (Inbox & Calendar)**.

```
$ cp .env.example .env
$ uv sync --extra dev
$ make demo          # seed sandbox + full offline end-to-end run
```

`make demo` needs **no API keys and no network**. `MOCK_MODE=1` and `LLM_MODE=cassette` are the
defaults: the mailbox is `sandbox/inbox/`, the calendar is `sandbox/calendar.db`, and the Reader
LLM replays recorded model output (switch to `LLM_MODE=live` with `OPENAI_API_KEY` for real calls).

Dashboard: `make serve` → <http://127.0.0.1:8000>

## Architecture

```
TRIGGERS                     ORCHESTRATOR (10 stages, deterministic)
mailbox poll ──► 1 Ingest  2 Prefilter  3 Screen  4 Reader LLM (0 tools)
CLI inject   ──► 5 Validate  6 Scheduler  7 Decide  8 Act  9 Verify  10 Record
                                     │
                                     ▼
                       POLICY GATEWAY (single choke point, P1–P10)
                          │                    │
                          ▼                    ▼
                 provider adapters      approval queue (human, optional)

every step ──► EVENT BUS ──┬─► hash-chained JSONL audit (traces/)
                           ├─► SQLite state, budgets, idempotency
                           └─► live dashboard over SSE + cost meter
```

- **LLM proposes, code disposes.** The only model that sees raw email text has zero tools and must
  return schema-valid JSON. Dates, slots and side effects are computed in Python.
- **One choke point.** `Orchestrator` never imports a provider. Every side effect is an
  `ActionRequest` evaluated by `PolicyGateway.execute()`.
- **Fail closed.** Exception, low confidence, schema failure or a blocked action ⇒ `ESCALATE`
  (draft + queue) or `QUARANTINE`, never "proceed anyway".
- **Kill switch everywhere.** Checked before every LLM call, every tool call, and between retries.

See [`AGENT.md`](AGENT.md) for the stage/tool tables and [`SAFETY.md`](SAFETY.md) for the
rule → code → test mapping behind the four safety features.

## CLI cheat sheet

```bash
meetingpilot seed                     # seed sandbox calendar + sample emails + cassettes
meetingpilot demo                     # run the full demonstrate script end-to-end
meetingpilot watch                    # poll sandbox/inbox/ forever
meetingpilot inject FILE|--text "..."  # drop one email into the pipeline
meetingpilot kill                     # trip the kill switch (also: dashboard button, touch .kill)
meetingpilot resume --ack             # clear HALTED state, requires explicit acknowledgement
meetingpilot explain RUN_ID           # human-readable timeline
meetingpilot replay RUN_ID            # re-run from recorded inputs, assert identical decisions
meetingpilot verify-chain [RUN_ID]    # detect tampering in the audit trail
meetingpilot serve                    # dashboard + SSE
```

## Safety, in one table

| Feature | Mechanism | Where |
|---|---|---|
| Guardrails | tool registry + stage scoping, P1–P10 at one gateway | `src/meetingpilot/policy_gateway.py` |
| Live monitoring | FastAPI + SSE, single-file dashboard, cost meter, queue | `src/meetingpilot/server.py`, `static/index.html` |
| Kill switch | `asyncio.Event` + `.kill` file, TaskGroup cancellation, `resume --ack` | `src/meetingpilot/kill_switch.py` |
| Audit trail | append-only JSONL, `prev_hash` chain, `explain`/`replay`/`verify-chain` | `src/meetingpilot/event_bus.py` |

## Tests

```bash
make test     # gateway fuzzing, DST scheduler, kill latency, replay/tamper, 32-case trap pack
```

## Honest limits

- Mock mode only out of the box. The Gmail/Google Calendar adapter is not implemented; every
  provider call goes through a local mock so judges never need credentials.
- Only non-`.ics` attachments are ignored, not parsed.
- One calendar, one owner. No multi-user, no delegation, no shared mailboxes.
- Multi-party external invites are escalated, not auto-confirmed.
- `LLM_MODE=live` sends sanitized email text to a third-party model; keep `cassette` for demos.

## License

MIT.
