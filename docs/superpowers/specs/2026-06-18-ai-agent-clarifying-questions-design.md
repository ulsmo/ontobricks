# AI Agent clarifying questions before running

**Date:** 2026-06-18
**Status:** Design — pending implementation plan
**Author:** benoitcayladbx

## Problem

A collaborative task assigned to the AI Agent fires immediately: the router
picks a specialized agent and runs it as a background job, then posts one report
comment. When the task is ambiguous the router can guess wrong (e.g. "evaluate
whether the `Person` class is necessary" was routed to the whole-ontology
Generator instead of the in-place Ontology Assistant). The user never gets a
chance to confirm scope or supply missing detail before work happens.

## Goal

Before running, the AI Agent posts a short plan plus any clarifying questions in
the task's Discussion thread, parks the task, and resumes automatically when the
user replies. It may ask follow-ups across several replies until it is confident,
then runs the chosen specialized agent and reports the outcome.

## Decisions (locked)

| Question | Decision |
|----------|----------|
| Loop model | **Park & resume** — post questions, park the task, resume on reply |
| Trigger | **Always confirm** — every AI-Agent task posts a plan + questions and waits for a go-ahead before running |
| Approval | **Any reply resumes** — the reply text is fed to the agent as answers/approval |
| Rounds | **Multi-round** — the agent may ask follow-ups until confident, then run |
| Routing | **Locked at start** — the router picks the agent once; clarification happens within that agent's scope |
| Architecture | **Thread is the source of truth** (Approach A) — the comment thread *is* the clarification conversation; no separate transcript storage |

### Implementation refinements (zero schema change)

To ship without a Postgres `CHECK`-constraint migration or base/file-store
changes, the locked decisions above are realized as follows:

- **No new task status.** The parked ("waiting on you") state reuses the
  existing `in_progress` status. A distinct, user-visible `waiting_input` status
  is a follow-up (it requires altering the `domain_tasks.status` CHECK and the
  store validation in three places).
- **Locked route without persistence.** The router runs at temperature 0 and is
  deterministic, so on resume we simply re-run it on the same title/description —
  the same agent is chosen every time. No `agent_key` column is added.
- **Linkage via the existing `comment_id`.** Every AI-Agent task is anchored to a
  thread root comment: tasks born from a comment already have one; for standalone
  tasks `create_task` first inserts a domain-level **kickoff comment** (the task
  statement) and stores its id as the task's `comment_id`. A human reply whose
  thread root equals a task's `comment_id` triggers resume.

## Flow

1. Task assigned to the AI Agent (`assignee == AI_AGENT_PRINCIPAL`) →
   `start_agent_task` launches a background job (as today).
2. **Route once:** the existing `agent_task_router` picks the specialized agent
   and records the choice. The choice is persisted on the task and not
   re-evaluated on later turns.
3. **Plan / ask:** a new single-shot **planner** step receives the task
   (title + description), the chosen agent's spec, and the conversation history
   reconstructed from the thread. It returns `{ready: bool, message: str}`.
   - First pass always returns `ready=false` with a plan + questions (the
     "always confirm" rule), so the very first turn never runs the agent.
   - Posts `message` as a comment under the task thread, keeps the task
     `in_progress` (the parked state), and ends the job.
4. **Resume on reply:** when a human posts a comment under a parked AI-Agent task
   thread, a resume hook in `CommentService.add_comment` launches a resume job
   (`resume_agent_task`). Only human-authored API comments flow through
   `add_comment`; the agent's own comments are written via the store directly, so
   they never self-trigger.
5. **Re-plan:** the resume job re-runs the (deterministic) router, rebuilds the
   Q&A history from the thread, and calls the planner again.
   - `ready=false` → post the follow-up question, stay `in_progress`.
   - `ready=true` → run the chosen specialized agent with the user's accumulated
     answers folded into its input, post the outcome report, set status `done`.

## Components

### `src/agents/agent_task_planner/` (new)

Single-shot LLM agent, mirroring `agent_task_router` (`engine.py`, `tools.py`,
`__init__.py`).

- **Input:** task title/description, chosen `AgentSpec`, conversation history
  (ordered list of `{role, text}` derived from the thread — AI Agent comments are
  `assistant`, human replies are `user`).
- **Output:** `PlanResult { ready: bool, message: str, error: str }`.
  - `ready=false`: `message` is the plan and/or the next question(s).
  - `ready=true`: `message` is a brief "running now" confirmation; the runner then
    dispatches the specialized agent.
- Degrades gracefully: on parse/LLM failure returns `error`; the runner posts an
  error note and leaves the task `waiting_input` so a retry reply re-triggers.

### `src/back/objects/registry/agent_task_runner.py`

- Extract the post-route worker logic into one `_run` worker that does
  route → history → plan → branch, used by both the initial and the resume job.
- `start_agent_task` (existing): launches the worker; the first pass always parks
  with a plan + questions (history has no user answers yet).
- `resume_agent_task` (new): launches the same worker for a parked task.
- A module-level in-process guard (set of active `domain_task_id`s) prevents a
  reply from starting a second concurrent run while one is in flight.
- `_thread_history(svc, folder, version, root_comment_id)`: reconstruct the
  ordered Q&A from the thread rooted at the task's `comment_id` (the root comment
  plus its replies, ordered by `created_at`). AI Agent author (`AI_AGENT_LABEL`)
  → `assistant`; everyone else → `user`.
- The chosen agent is obtained by re-running the deterministic router each pass
  (routing is locked; no persistence needed).
- Folding answers into agent input: the user's accumulated answers are appended
  to the task text passed to `_dispatch_agent`.
  - **Ontology Assistant:** `user_message` = task text + accumulated user answers.
  - **Generator / rules / icons / mapper:** the confirmation gates the run; the
    answers ride along in the task text where the bridge accepts free text.

### `src/back/objects/registry/CommentService.py`

- `create_task`: when `is_ai_agent(assignee)` and there is no originating
  `comment_id`, first insert a domain-level **kickoff comment** (the task
  statement) and pass its id as the task's `comment_id`, so every AI-Agent task
  has a thread root.
- `add_comment`: after a successful insert, best-effort detect whether the new
  (human) comment's thread root matches an active AI-Agent task and, if so, call
  `resume_agent_task`. The AI Agent's own comments are written through the store
  (`_report`), not `add_comment`, so they never trigger resume. Failures are
  logged, not surfaced.

## Data flow & linkage

- Every AI-Agent task carries a `comment_id` thread root (originating comment, or
  the kickoff comment for standalone tasks).
- Parked/active tasks = `assignee == AI_AGENT_PRINCIPAL` AND
  `status == "in_progress"`.
- A new human comment whose thread root equals such a task's `comment_id`
  triggers resume.
- The thread itself stores the full clarification transcript — no extra table.

## Error handling

- **Planner LLM failure:** post an error comment; leave the task `in_progress`
  so the next human reply re-triggers planning.
- **Specialized agent failure:** unchanged from today — post a failure comment;
  the task status reflects the failure.
- **Concurrent reply while running:** the in-process guard drops the second
  resume so the agent never runs twice for one task at once.
- **No reply ever:** the task stays `in_progress` indefinitely (acceptable;
  surfacing parked tasks distinctly in "My Tasks" is a separate follow-up).
- **Invariant:** the agent never runs without at least one human reply on the
  thread (honors "always confirm").

## Testing

- **Planner unit tests:** ready vs. ask decisions; graceful degradation on
  unparseable/empty LLM output.
- **Runner tests:** first pass parks with a plan; resume that still needs info
  asks again and stays `waiting_input`; resume that is ready runs the agent and
  marks `done`; thread-history reconstruction maps authors to roles correctly.
- **CommentService tests:** standalone AI-Agent task creation inserts a kickoff
  comment and links it; a human reply under an active AI-Agent task triggers
  `resume_agent_task`; a reply under a `done`/non-agent task does not.

## Out of scope / follow-ups

- Re-routing on resume (locked route for v1).
- A distinct, user-visible `waiting_input` task status (needs a CHECK-constraint
  migration + store validation changes); v1 reuses `in_progress`.
- Surfacing parked AI-Agent tasks in the "My Tasks" worklist (sentinel assignee
  is not a user email).
- Auto-applying results for the non-conversational agents (generator, rules,
  icons, mapper still produce proposals; only the Ontology Assistant applies).
- Dedicated UI affordances (Approve/Run buttons) — v1 infers from comment text.
