# TODOS

## P2 — Post-Workbench UX Overhaul Roadmap

### [CEO-E6] Real-Time SSE / WebSocket Log & Progress Streaming
- **What**: Replace `1400ms` / `1800ms` / `2500ms` HTTP polling on `/api/jobs/<id>`, `/api/jobs/<id>/logs`, and `/api/runtime/status` with a multiplexed Server-Sent Events (SSE) endpoint streaming log chunks, `active_tasks` updates, and `log_alert_seq` counters.
- **Why**: Eliminates idle polling requests across multi-tab sessions and gives sub-second step progress updates during fast ComfyUI 4-step/8-step runs.
- **Pros**: Lower steady-state CPU/network overhead when 5+ tabs are open; immediate log arrival without 1.4s jitter.
- **Cons**: Flask's built-in WSGI threaded dev server holds one thread per open SSE stream; requires explicit connection caps, heartbeat timeouts, reconnect backoff, and `Last-Event-ID` cursor replay.
- **Context**: Deferred during the 2026-11-26 Workbench UX Overhaul autoplan because `DX-H5` + `ENG-H5` (`?include_logs=0` + generation-tracked `/api/jobs/<id>/logs?offset=` + 0-stat settled `summaries()`) already eliminates the 100KB/poll I/O storm with zero new transport infrastructure. Start in `workbench/server.py` alongside the existing cursor generator.
- **Depends on / blocked by**: Completion of Workbench UX Overhaul Stages 1–3 (`GET /api/jobs/<id>/logs` cursor contract).

### [CEO-E10] Visual Story-to-`film.json` Guided Editor / Script Wizard
- **What**: Add a guided form/timeline builder on `/` that constructs valid `film.json` (cast, scenes, shots, durations, dialogue) with live dialogue-budget feedback, writing clean JSON to `#film-json`.
- **Why**: Non-technical creators currently hand-edit raw JSON or rely on external LLMs to author `film.json`, creating syntax errors and slow iteration.
- **Pros**: Dramatically widens creator accessibility; pairs naturally with existing `inspect_dialogue_budget()` preflight.
- **Cons**: Significant UI surface area (shot reordering, cast binding sync, undo/redo) outside the zero-build UX shell overhaul scope.
- **Context**: Surfaced in Phase 1 CEO Review (10x vision). Keep `#film-json` as the canonical source of truth so power users and coding agents (`SKILL.md`) continue pasting/importing JSON seamlessly.
- **Depends on / blocked by**: Workbench UX Overhaul Stage 3 (`create.js` draft & upload conflict tray stabilization).

### [CEO-E11] Single-Segment Pilot Mode & Multi-Take Side-by-Side Comparison
- **What**: Allow running a single chosen segment as a low-cost pilot before approving the remaining segments, and retain previous segment takes (`clips/segN.takeK.mp4`) with side-by-side Plyr A/B playback and 1-click promote.
- **Why**: Lets creators validate motion/acting/voice on one segment before committing 10x compute, and avoids throwing away a decent take while fishing for a better seed.
- **Pros**: Direct creative quality boost and cost savings on live RunningHub runs; preserves discarded reroll outputs visibly.
- **Cons**: Requires multi-take directory/metadata schema in `state.json` and disk cleanup controls, plus UI card complexity.
- **Context**: Surfaced in Phase 1 CEO Review. Current `workbench/segments.py` already stages new takes inside `inputs/segment-<rev>/` before atomic promotion — start there to keep prior takes archived instead of overwritten.
- **Depends on / blocked by**: Workbench UX Overhaul Stages 1–3.

### [CEO-E12] Saved Next-Job Queue & Multi-Node Pool Decoupling
- **What**: Allow creators to stage a queue of pending jobs (without violating single-active-job mutual exclusion by default, or opt-in dispatching independent jobs across separate ComfyUI nodes).
- **Why**: Overnight batch creators want to enqueue 3–5 short films while keeping safe serial execution and pause/stop controls.
- **Pros**: High leverage for heavy local/Colab GPU users; zero babysitting between jobs.
- **Cons**: Introduces persistent queue state, failure-stop policy (halt queue on error vs continue), and UI queue management.
- **Context**: Explicitly excluded from the 2026-11-26 Workbench UX Overhaul by Non-Goal #2 (single active job mutual exclusion). Start in `workbench/jobs.py` once `RunState` settlement and pause/stop are proven in production.
- **Depends on / blocked by**: Workbench UX Overhaul Stage 1 (`RunState` lifecycle).

### [ENG-E1] Cross-Platform Lifetime Lease for Direct CLI (`h3ref.py film` / `one`)
- **What**: Acquire a cross-platform exclusive lifetime lease file (`inputs/worker.lock` via `fcntl.flock` on POSIX and `msvcrt.locking` / Win32 `CreateFile` exclusive handle on Windows) across direct CLI `h3ref.py film` and `h3ref.py one` entry points, matching managed workbench workers.
- **Why**: Currently `h3ref.py film` probes `worker.lock` only transiently on POSIX (`h3ref/film.py:1345`), Windows skips it, and `cmd_one()` holds no lifetime lease, meaning concurrent direct CLI + browser Resume could race on `state.tmp` and submit duplicate paid tasks.
- **Pros**: Makes direct CLI recovery 100% mutually exclusive with browser Resume on macOS, Linux, and Windows.
- **Cons**: Requires cross-platform Win32 handle inheritance testing and careful cleanup around `h3ref.py` subprocess invocations.
- **Context**: Surfaced in Phase 3 Eng Review (`Codex #2`). Mitigated in this release by designating Workbench Resume (`POST /api/jobs/<id>/resume`) as the primary recovery path, adding non-blocking POSIX lock checks in `cmd_one`, and documenting no-concurrent-CLI rules in `docs/workbench.md`.
- **Depends on / blocked by**: Workbench UX Overhaul Stage 1 (`ENG-H2`).

## Completed
