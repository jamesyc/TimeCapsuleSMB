# Meta-Prompt: Rewrite an Application into a Simpler, Clean-Architecture Version

> **How to use (human operator):**
> 1. Fill in every `{{PLACEHOLDER}}` in *Project Parameters* below. Placeholders appear **only** in that section.
> 2. Save this file as `AGENTS.md` at the repo root. For Claude Code, also create `CLAUDE.md` containing the single line `@AGENTS.md`. For Gemini CLI, create `GEMINI.md` containing `Read AGENTS.md and follow it.` Keep this file under 32 KB (Codex truncates beyond that).
> 3. Recommended extras (the prompt degrades gracefully without them): the Superpowers plugin (`/plugin install superpowers@claude-plugins-official` on Claude Code) and the Context7, Playwright, and Serena MCP servers.
> 4. **Running the loop.** Interactive: start each session with *"Read AGENTS.md and begin."* Unattended (any harness with a CLI):
>    `while :; do claude -p "Read AGENTS.md and begin" || break; done` (or `codex exec ...` / `opencode run ...`).
>    The agent exits after one work unit. If the first line of `PROGRESS.md` starts with `BLOCKED:`, the agent prints the pending question and exits immediately — answer it, delete the `BLOCKED:` line, and the loop resumes.
> 5. **Approvals are yours alone.** You grant a phase gate by adding a line to `DECISIONS.md`: `APPROVED: <phase-0|phase-1|change-list> @ <git SHA> <date>` (or by telling the agent verbatim to write it). The agent may never write an `APPROVED:` line on its own initiative.

---

## Project Parameters (operator fills in — placeholders live only here)

- **App to rewrite:** `src/timecapsulesmb/` (Python CLI package `timecapsulesmb` that deploys a modern Samba 4 server onto Apple AirPort Time Capsules over SSH) **and** `macos/TimeCapsuleSMB/` (SwiftUI macOS GUI frontend that spawns the Python `tcapsule api` backend as a JSON-lines subprocess). Both are rewritten.
- **Legacy build/run/test commands:** Bootstrap: `./tcapsule bootstrap` (builds `.venv` + `tcapsule` console script; the root `tcapsule` only execs the venv binary). Run: `.venv/bin/tcapsule <discover|configure|deploy|activate|flash-patch|doctor|uninstall|api> ...`. Full verification: `make test-parallel` (C compile checks via `make test-c` + pytest-xdist `--dist loadfile`). Focused: `.venv/bin/pytest tests/test_foo.py`. Lint: `make lint` (ruff on `src tests macos/TimeCapsuleSMB/tools tcapsule`; py39 target, line-length 100, rules E9/F63/F7/F82). Swift tests (macOS only): `swift test --package-path macos/TimeCapsuleSMB`. Env vars: `.env` in repo root (gitignored) with `TC_HOST`/`TC_PASSWORD`; `TCAPSULE_HELPER` overrides the helper path; telemetry on by default, `tests/conftest.py` blocks unmocked telemetry posts.
- **Target stack:** same language/framework, latest stable versions — Python current stable (new code does NOT need the legacy py3.9 floor; CI matrix for new code = current Python only unless a CHANGE entry says otherwise) and current SwiftUI/Xcode.
- **New code location:** `/Users/felipe.dos.santos/code/apple-time-capsule-utils` — a **new, separate git repo with no legacy code** (operator's explicit choice). All "new code" checks are scoped to that repo. The legacy repo `TimeCapsuleSMB` stays untouched as the behavior oracle. In-place rewrite clause does not apply.
- **Out of scope:** no new features, no UI redesign, no database migration (none exists). No changes to the on-device NetBSD runtime (`assets/boot/samba4/*` shell scripts) or the prebuilt `bin/` NetBSD binaries — these are deployment payloads copied verbatim as data, not rewritten. No firmware/VM work under `build/`.
- **Iteration budgets:** defaults — Phase 0 ≤ 10 sessions; Phase 1 ≤ 5; Phase 2 ≤ 5 sessions *per slice*; Phase 3 ≤ 5. Counted in `PROGRESS.md`.
- **Essential functionality** is defined by the approved `FEATURES.json` — never by anyone's memory.

---

## 1. NORTH STAR (immutable — never edit this section)

Rewrite the application defined in *Project Parameters* into a **simpler, functionally equivalent** version with: decoupled layers that obey the Dependency Rule; reusable, single-responsibility components; and standardized entities, constants, logging, and error handling as defined in Section 7. Every behavior tagged KEEP in `FEATURES.json` must work identically in the new code; every CHANGE behaves as approved; every DROP is verifiably absent. Simpler means fewer concepts, less duplication, smaller units, explicit boundaries — **never** fewer working features.

At the start of every session, restate this goal in one sentence in `PROGRESS.md` before doing any work.

## 2. Definition of Done (mechanical)

Complete only when **all** of the following hold:

1. Every **KEEP** entry in `FEATURES.json` has `"passes": true`, evidenced by green characterization tests; every **CHANGE** entry passes tests encoding its approved behavior; every **DROP** entry has `"passes": true` with evidence that it is absent from the new code (search command + zero results, recorded in `PROGRESS.md`).
2. The full test suite — including all characterization tests captured from the legacy app — is green.
3. `grep -rn "TRANSITIONAL:" <new-repo>/` returns zero results, and the dependency-check rule (Section 7.1), configured to also forbid new-code imports of legacy modules, passes.
4. The final review (Phase 3 step 3) reports no Critical or Important findings. *Severity: Critical = breaks parity or the DoD; Important = violates Section 7; Minor = style.*
5. The standards self-audit (Section 7) passes for every new-code module.

Only the `passes` field of `FEATURES.json` may ever change after approval. Removing or editing entries, or weakening tests, to reach Done is a violation — it hides missing or broken functionality.

## 3. Session Start Protocol (every session, and after any context compaction)

0. Read the first line of `PROGRESS.md`. If it starts with `BLOCKED:`, print the pending question and **exit immediately**. Do nothing else.
1. `pwd`; confirm repo root. Read Section 1, then (each **if it exists**) `PLAN.md`, the last 3 entries of `PROGRESS.md`, `DECISIONS.md`, and `git log --oneline -15`.
2. Run Capability Discovery (Section 4).
3. **Phase routing** (artifacts + `APPROVED:` lines in `DECISIONS.md` decide — never your recollection):
   - No `FEATURES.json`, or no `APPROVED: phase-0` line → you are in **Phase 0**.
   - `APPROVED: phase-0` present but no `APPROVED: phase-1` → **Phase 1**.
   - `APPROVED: phase-1` present and DoD items not all met → **Phase 2** (or **Phase 3** if every KEEP/CHANGE entry already passes).
4. If `PLAN.md` exists: run the build and test suite (commands at its top). **If anything is broken, fixing it is your only task this session**; if the same breakage survives 3 recorded attempts, escalate (Section 9). Before Phase 1, use the legacy commands from *Project Parameters* instead.
5. Increment the current phase's session counter in the `PROGRESS.md` header and check it against the *Iteration budgets*; if exhausted, escalate.
6. Proceed per your phase: Phases 0/1 → Section 6; Phases 2/3 → Section 8, picking the **first incomplete task in `PLAN.md`** (read any recorded fix-attempt counters for it first).

Never trust a compaction summary for status — trust the files. Anything not written to them or committed is lost.

## 4. Capability Discovery

Enumerate your available **skills, MCP servers, plugins, subagent types, and plan modes**. Use the best available option per activity; missing → use the fallback, note it once in `PROGRESS.md`. Never block on a missing capability.

| Activity | Prefer | Fallback |
|---|---|---|
| Process discipline | **Superpowers skills** (`superpowers:*`): brainstorming → writing-plans → subagent-driven-development / executing-plans, test-driven-development, systematic-debugging, requesting-code-review, verification-before-completion, using-git-worktrees. Invoke by exact name at the points marked below; load the skill, never paraphrase from memory | Inline procedures in this file |
| Current library docs | **Context7** (`resolve-library-id` → `query-docs`, or `ctx7` CLI) before writing code against any third-party library — never trust training-data API signatures | Official docs via web search |
| Legacy code navigation | Native LSP or **Serena MCP** (find_symbol, find_referencing_symbols); list all references before changing any public symbol | grep — last resort, strings/config only |
| Whole-repo overview | **Repomix** (`--compress`) once at Phase 0 start | Directory listing + targeted reads |
| Parity checks (web UI) | **Playwright CLI** (cheaper) or Playwright MCP: drive the same flow on legacy and new, compare | HTTP-level checks (`curl`); else scripted manual steps for the operator |
| Database ground truth | Read-only DB MCP (schema, constraints, indexes) before rewriting data access | Schema dump via CLI |
| History mining | `gh` CLI or GitHub MCP: issue/PR history when legacy behavior looks intentional-but-odd | `git log -L`, `git blame` |
| Cross-session memory | Repo files (canonical, Section 5); a memory MCP only as a secondary index | Repo files alone |

**Subagents**, if available: use for (a) parallel read-only legacy research, (b) one fresh implementer per plan task, (c) fresh-context reviewers with read-only tools. Every dispatch states: objective, expected output format, tools, out-of-scope boundaries; verify results against Section 1 before accepting. No subagents → same work sequentially, checkpointed in `PROGRESS.md`.

## 5. Artifact Contract (the repo is your memory)

Committed with the code:

- `PLAN.md` — **the single ordered work queue.** Build/test/run commands at the top (legacy commands copied from *Project Parameters*, new-code commands as created). Phased tasks, each small and self-contained: the `FEATURES.json` ids it advances, its failing-test step, implementation step, verification command, commit step. Infrastructure tasks (error taxonomy, logger, config, lint rules) are ordinary tasks here even though they map to no feature.
- `FEATURES.json` — every legacy behavior: `{id, description, entry_point, tag: KEEP|DROP|CHANGE, approved_behavior?, evidence?, passes: false}`. Default-FAIL. `passes` flips only per Section 8 step 8 (KEEP/CHANGE) or with absence evidence (DROP). Nothing else may change after `APPROVED: phase-0`.
- `PROGRESS.md` — append-only log; header holds per-phase session counters; first line is reserved for a `BLOCKED:` marker. Every work unit: what changed, evidence (command + result), fix-attempt counters (`task T-12: attempt 2/3 — <approach> — failed`), next step.
- `DECISIONS.md` — append-only decisions with rationale, plus operator `APPROVED:` lines. Never delete entries.
- `STANDARDS.md` — Section 7 copied verbatim + approved project-specific additions.
- `GLOSSARY.md` — the ubiquitous language: one agreed name per domain concept.
- `SUSPECTED_BUGS.md` — legacy behavior that looks wrong; replicated in the new code, listed here. Never silently "fix" it.
- `BACKLOG.md` — out-of-scope ideas. New ideas go here, never into code.

**The oracle is frozen at `APPROVED: phase-0`.** Characterization tests and golden-master normalization rules may change afterward only with an operator-approved CHANGE entry referenced in the commit, with the diff logged in `PROGRESS.md`. MIGRATE commits must show an empty `git diff --stat` on test and normalization paths — reviewers check this mechanically.

## 6. Phases (strict order; each gate requires its `APPROVED:` line)

**Phase 0 — Understand (Initializer).**
First verify the legacy build/run/test commands from *Project Parameters* actually work; record them in `PROGRESS.md`. Map the legacy app: entry points (routes, CLI commands, jobs, handlers), side effects (DB writes, files, network, email), config flags, integrations. Subagents available → dispatch parallel explorers (data model / external interfaces / business rules / test coverage) — *Superpowers: `dispatching-parallel-agents`*. Mine `git blame` and issue history for "why is this weird" code. Then:
1. Write `FEATURES.json`, tagging KEEP / DROP (with evidence of non-use) / CHANGE (with intended behavior). Blind full parity is a failure mode: reimplementing dead code is as bad as dropping live code.
2. Write **characterization tests** pinning the legacy app's *observed* behavior for every KEEP feature — run the app, don't guess; assert observed outputs even when they look buggy (log those in `SUSPECTED_BUGS.md`). Where feasible, build a golden-master harness: scripted input corpus + recorded outputs (responses, DB state deltas, files, events), replayable as a diff, with nondeterminism (timestamps, IDs, ordering) normalized. Parity comparisons must run against isolated, identically seeded environments (script seed/reset into `PLAN.md`); if isolation is infeasible, rely on the recorded corpus, not live dual-runs.
3. Write the `BLOCKED:` marker requesting approval of the tags. **Stop.**
*Gate: inventory covers 100% of discovered entry points; characterization suite green against untouched legacy; `APPROVED: phase-0` recorded.*

**Phase 1 — Design.**
*Superpowers: `brainstorming`, then `writing-plans`.* Produce:
1. `DECISIONS.md` entry: target architecture — Section 7.1's layers mapped to concrete directories under the new repo; the seams where new code intercepts old (router, facade, repository interface).
2. `STANDARDS.md` and `GLOSSARY.md`.
3. **Standards-parity reconciliation:** list every observable difference the Section 7 standards will force (e.g. timeouts where the legacy app hung, normalized error payloads, changed log levels) and add each as a CHANGE entry for approval. Parity wins until approved.
4. `PLAN.md`: vertical slices (one feature/endpoint end-to-end), domain-core-first, written for an executor with zero prior context; exact interfaces between tasks.
5. Verify every planned third-party API against current docs (Context7).
6. Write the `BLOCKED:` marker requesting design/plan approval. **Stop.**
*Gate: `APPROVED: phase-1` recorded.*

**Phase 2 — Execute (strangler-fig).**
Work on a dedicated branch or worktree (*Superpowers: `using-git-worktrees`*) — never on the default branch. The legacy code is modified **only** at approved seams, plus per-slice deletions after the Parity Gate; it stays runnable as the behavior oracle throughout. One `PLAN.md` task at a time via Section 8:
- Build the new implementation beside the old; route one slice through it; the app must build, run, and pass the characterization suite after every task, with mixed old/new code.
- Temporary glue is marked `TRANSITIONAL:` with a written removal condition.
- Commits are either **MIGRATE** (behavior-preserving; golden master passes exactly; no test/normalization edits) or **SIMPLIFY/CHANGE** (implements an approved CHANGE entry; tests updated accordingly). Never both in one commit.
- Each piece of state (DB table, file, queue) has exactly one owning path per slice; if both paths must write it, route writes through one shared adapter.
*Parity Gate per slice: (1) characterization tests green; (2) golden-master diff empty — or, if no golden master was feasible, characterization tests alone — or every diff mapped to an approved CHANGE; (3) error paths exercised, not just happy path; (4) Section 7 self-audit passes. Only then delete that slice's legacy code, recording the gate in the commit/PR description.*
*Phase exit: every KEEP/CHANGE entry `passes: true`, all slices' legacy code deleted → Phase 3.*

**Phase 3 — Verify and Retire.**
1. `grep -rn "TRANSITIONAL:" <new-repo>/` → zero; dependency-check (incl. legacy-import ban) passes.
2. Full characterization + golden-master suite green; every `FEATURES.json` entry `passes: true` with evidence (DROP = absence evidence).
3. **Final review.** Subagents available → *Superpowers: `requesting-code-review`* with baseline + HEAD SHAs, reviewer explicitly told to check layer boundaries and that no MIGRATE commit touched test/normalization paths. No subagents → a dedicated fresh session whose only work unit is a cold re-read of the full diff against Section 7 and `FEATURES.json`, findings recorded in `PROGRESS.md`; no code edits in that session. Handle findings via *`receiving-code-review`*: verify each claim before acting; push back with evidence on suggestions that reintroduce complexity.
4. Regenerate the feature inventory as-built; diff against approved — empty except approved changes.
5. *Superpowers: `verification-before-completion`*: rerun everything fresh, quote actual output. Then *`finishing-a-development-branch`* — the operator chooses merge/PR/keep.

## 7. Coding Standards (The Canon — for all new code)

Distilled from *Clean Architecture*, *Clean Code*, *Software Architecture Patterns*, *Patterns of Enterprise Application Architecture* (PoEAA), *Domain-Driven Design* (DDD), *Design Patterns* (GoF), *Head First Design Patterns* (HFDP), *Effective Java*, *Implementation Patterns*, *Continuous Delivery*, *Refactoring*, *Release It!*, *Building Microservices*, *Production-Ready Microservices*, and Google's *Site Reliability Engineering* (SRE). Copy into `STANDARDS.md`; self-audit each slice before its Parity Gate.

**Precedence:** behavior parity outranks the Canon. Any standard whose application changes observable behavior enters through an approved CHANGE entry (Phase 1 step 3), never unilaterally. Purely internal standards (structure, naming, taxonomy, logger module, correlation IDs) apply unconditionally.

### 7.1 Layers & the Dependency Rule
- Four layers: **domain** (entities — business rules, pure), **application** (use cases / service layer — orchestrates entities, owns transactions, defines the app boundary), **interface adapters** (controllers, presenters, gateways, mappers), **infrastructure** (frameworks, DB, HTTP — details, outermost). *(Clean Architecture; PoEAA)*
- Source dependencies point **only inward**; nothing in an inner layer names anything in an outer layer — no functions, classes, or data formats. *(Clean Architecture)*
- Layers are **closed**: requests pass through the layer immediately below, never skipping. *(Software Architecture Patterns)*
- Crossing against the flow: inner layer declares the interface (port), outer layer implements it (adapter) — Dependency Inversion. *(Clean Architecture)*
- Only simple, isolated DTOs cross boundaries — never entities, raw DB rows, or framework objects. *(Clean Architecture; PoEAA)*
- Don't marry the framework: no framework base classes in domain code; the database is a detail. *(Clean Architecture)*
- **Enforce mechanically**: an import-linter / dependency-check rule in the build (ecosystem's standard tool) makes any layer violation — or any new-code import of a legacy module — a build failure.

### 7.2 Presentation (MVC)
- Model holds data, state, and business logic; View only displays; Controller interprets input, manipulates the Model, selects the View. *(HFDP; PoEAA)*
- The Model never depends on the presentation; it notifies views via Observer. No domain logic in views — a view needing a decision calls a domain method (e.g. `isSalesImproving()`), never computes it. *(PoEAA; HFDP)*
- Simple pages → Page Controller; complex navigation/security → Front Controller; machine-dictated screen flow → Application Controller. Don't add one you don't need. *(PoEAA)*

### 7.3 Reusable Components
- Program to an interface, not an implementation; favor composition over inheritance; encapsulate what varies. *(GoF; Effective Java; HFDP)*
- SOLID: one reason to change per module (SRP); extend by adding code, not modifying working code (OCP); depend on abstractions (DIP). *(Clean Architecture; Clean Code)*
- Group classes released together (REP) and changing together (CCP); don't force users to depend on things they don't use (CRP). *(Clean Architecture)*
- DRY: duplication is the primary enemy — extract shared abstractions; replace repeated switch/if chains with polymorphism. Many small classes and functions that do one thing; one level of abstraction per function. *(Clean Code)*
- **Search before implementing** — the component may already exist. Extend the shared modules (error taxonomy, logger, config); never create parallel mechanisms.

### 7.4 Entities, Value Objects, DTOs, Naming
- Entity = identity + lifecycle + invariants; Value Object = immutable, equality by fields, freely shareable; DTO = flat serializable boundary shape, no logic. Map between them only at layer boundaries via explicit mappers/assemblers. *(DDD; PoEAA; Clean Code)*
- Invalid objects are unconstructible: invariants enforced in complete constructors or factories. Entities cluster into aggregates; external access only through the aggregate root. *(DDD; Implementation Patterns)*
- Ubiquitous language: one name per concept (`GLOSSARY.md`), identical in code, DB, API, docs; intention-revealing, pronounceable names; no abbreviations, prefixes, or Hungarian encodings. Suffix scheme: `*Request`/`*Response` (transport), `*Record` (persistence), bare noun (entity). *(DDD; Clean Code)*

### 7.5 Constants & Configuration
- No magic numbers or strings: every meaningful literal is a named, searchable constant, defined once, placed where a reader expects it. Related constants → enums, never int/string constants; never constant interfaces. *(Clean Code; Effective Java; Implementation Patterns)*
- Environment-dependent values are configuration, not code: same binary in every environment; config read and validated at startup into one typed Config object (missing key = immediate startup failure with a clear message); no ad-hoc env reads mid-execution; defaults held at the highest level and passed down. *(Continuous Delivery; Clean Code)*
- Config files under version control; secrets never in code, VCS, logs, or exception messages — injected at deploy time. *(Continuous Delivery; Effective Java)*

### 7.6 Error Handling
- One error taxonomy module in the domain/application layer: base `AppError` (code, message, context, cause) with a small closed set of subclasses (Validation, NotFound, Conflict, Auth, ExternalService, Internal).
- Exceptions, not return codes; define exception classes by how callers must respond; every exception carries the values that produced it. *(Clean Code; Effective Java)*
- Never swallow: no empty catch blocks; catch only to translate, enrich, or handle-and-log with the original attached as cause. *(Effective Java)*
- Never return or pass null: empty collections, Special Case objects, or Optionals. *(Clean Code; Effective Java)*
- Adapters translate third-party errors into taxonomy errors at the boundary; inner layers never see vendor exception types. *(Clean Code; Effective Java)*
- Fail fast on invariant violations and invalid input. At integration points apply Timeouts, Circuit Breakers, Bulkheads *(Release It!)* — these change observable failure behavior, so they enter via approved CHANGE entries. Top-level handlers (HTTP layer, job runner) are the only place errors become responses/exit codes, mapping error code → status.

### 7.7 Logging & Observability
- One logging module wrapping the platform logger; direct print/console output is banned. Structured key-value/JSON with fixed base fields: timestamp, level, service, correlation_id, event code, duration where relevant. Static event names with data in fields, not interpolated prose. *(Release It!; SRE)*
- Correlation ID generated at every entry point, propagated through all layers and outbound calls. *(Building Microservices; SRE)*
- Levels: ERROR = failed operation needing operator action only (bad user input is WARN at most); INFO = significant state transitions and boundary entry/exit with outcome + duration; DEBUG off in production. *(Release It!)*
- Never log secrets, tokens, or sensitive user data. *(Effective Java; Release It!)*
- Design for — but do not deploy — log rotation/aggregation and four-golden-signals monitoring (latency, traffic, errors, saturation; alert on symptoms, not causes): keep the code aggregation-ready; the infrastructure itself is out of scope. *(Release It!; Production-Ready Microservices; SRE)*

## 8. The Work-Unit Loop (Phases 2–3)

Exactly **one** work unit — the first incomplete task in `PLAN.md` — per session. Finish, verify, commit, log, stop; even if you have time for more.

1. **Pick** the first incomplete `PLAN.md` task. Read its recorded fix-attempt counter.
2. **Drift check**: re-read Section 1; write one line in `PROGRESS.md`: how this task serves the goal. Can't answer → stop; re-plan against Section 1 or escalate.
3. **Search first**: confirm the needed code doesn't already exist in the new repo.
4. **TDD** (*Superpowers: `test-driven-development`*): red → green → refactor. For migration tasks, expected values come from the legacy app's *observed* behavior — run it. No production code without a failing test first (wrote some anyway? delete it, restart the cycle). No placeholders or stubs.
5. **Verify**: full test suite + an end-to-end check through the real UI/API (Playwright/HTTP). Failing tests unrelated to your change are also your job. Bug or divergence from legacy → *Superpowers: `systematic-debugging`* — root cause first, legacy implementation as the working reference. Log each attempt (`attempt N/3`) in `PROGRESS.md`; the counter is cumulative across sessions — at 3, escalate.
6. **Standards self-audit** of touched code against Section 7.
7. **Review**: subagents → fresh reviewer (*Superpowers: `requesting-code-review`*) with baseline + HEAD SHAs and task requirements only. No subagents → before committing, re-read the full diff cold against the task requirements and Section 7; record findings. Critical/Important findings (defined in Section 2) block.
8. **Record evidence** in `PROGRESS.md` (actual command + result). Flip a feature's `passes` to `true` only when its slice clears the Parity Gate.
9. **Commit** (MIGRATE or SIMPLIFY/CHANGE — never both), update artifacts, stop.

Under an outer loop, the mechanical stop is Section 2 — or a `BLOCKED:` marker. Fix problems by editing plan/spec files between iterations, never by mutating this prompt.

## 9. Guardrails & Escalation

**Prohibitions (hard rules):**
- Never big-bang: the app must build, run, and pass the characterization suite after every task.
- Never delete a slice's legacy code before its Parity Gate; never touch the legacy app otherwise, except at approved seams.
- Never delete, weaken, or edit a test, a normalization rule, or a `FEATURES.json` entry to get to green; the oracle is frozen (Section 5) — changes require an approved CHANGE entry.
- Never drop or "fix" inexplicable legacy logic on your own judgment — replicate it, record it in `SUSPECTED_BUGS.md`. Equivalence is proven only by legacy-derived tests, never by tests written against the new code's own behavior.
- Never add features, endpoints, options, or "improvements" absent from `FEATURES.json` — ideas go to `BACKLOG.md`.
- Never write an `APPROVED:` line. Never commit secrets. Never force-push, push, or merge without operator instruction.

**Escalate — write `BLOCKED: <one-line question>` as the first line of `PROGRESS.md`, add context below, and exit — when:**
(a) a change would require editing Section 1, a `FEATURES.json` entry, or a recorded decision; (b) the same failure persists after 3 cumulative fix attempts; (c) an action is destructive or irreversible (data migration, deleting user data, force-push, auth/billing changes); (d) two requirements conflict; (e) a phase/slice iteration budget (*Project Parameters*) is exhausted; (f) a phase gate needs operator approval. Only the operator removes a `BLOCKED:` line.

---

## Operator's answers (session 1, 2026-08-09)

- App to rewrite: **both** `src/timecapsulesmb/` (Python CLI) and `macos/TimeCapsuleSMB/` (SwiftUI GUI)
- New code location: **new repo** `../apple-time-capsule-utils` (= `/Users/felipe.dos.santos/code/apple-time-capsule-utils`), no legacy code in it
- Target stack: same language/framework, latest stable versions
- Out of scope: default — no new features, no UI redesign, no DB migration (plus: on-device `assets/boot/samba4/*` and `bin/` binaries are payload data, not rewritten; no `build/` VM work)
- AGENTS.md handling: keep existing repo AGENTS.md intact; this file is `AGENTS-REWRITE.md` beside it
- Iteration budgets: defaults (Phase 0 ≤ 10; Phase 1 ≤ 5; Phase 2 ≤ 5/slice; Phase 3 ≤ 5)
