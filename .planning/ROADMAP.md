# OntoBricks Roadmap — Cursor-Native Superpowers (CNS)

> Single source of truth for active and upcoming workstreams. Mirrors GitHub Milestones.
> See `/Users/dermot.smyth/.claude/plans/ultrathink-perform-a-detailed-whimsical-token.md` for the full methodology + analysis.

## How to use this file

- **Each H2** is a GitHub Milestone (`M1: Foundation`, `M2: AI Discipline`, etc.).
- **Each `- [ ]` bullet** is a GitHub Issue + a `.planning/<slug>/PLAN.md` directory in this repo.
- **`<slug>`** is the lowercase-hyphen form of the Issue title (e.g., `ai-feature-skill`, `digitaltwin-split-p1`).
- Update this file in the same PR that closes an Issue. `code-review` skill checks for the update.

## Cross-references

- The full methodology lives in the plan file (above).
- Per-task atomic plans live in `.planning/<slug>/PLAN.md` (produced by `superpowers:writing-plans`).
- AI-feature work additionally has `.planning/<slug>/SPEC.md` (gated by `.cursor/12-ai-feature-lifecycle.mdc`).
- Audit trail is `changelogs/<YYYY-MM-DD>.log`.

---

## M1 — Foundation (close missing artifacts)

Theme: stand up the substrate that the rest of CNS depends on. None of these change product behaviour; all close gaps from §2 of the plan.

- [x] **M1.P1** Bootstrap `.planning/ROADMAP.md` (this file). *Slug: `bootstrap-roadmap`.*
- [x] **M1.P2** Recreate `src/.coding_rules.md` (gap #1). *Slug: `coding-rules-bootstrap`.*
- [x] **M1.P3** Create `changelogs/` + seed entry (gap #2). *Landed 2026-05-12.*
- [x] **M1.P4** `.pre-commit-config.yaml` (gap #8). *Slug: `pre-commit-hooks`.*
- [x] **M1.P5** PR review checklist + PR template (gap #12). *Slug: `pr-review-checklist`.*
- [x] **M1.P6** Conventional Commits + commitlint (gap #10). *Slug: `commitlint`.*
- [x] **M1.P7** Worktree multi-agent guide (gap #13). *Slug: `worktree-guide`.*

## M2 — AI Discipline (critical path) — closes gap #4, #5

Theme: the rule + skill + eval gate that prevents 5-agents-zero-eval regressions from shipping. Critical-path because every later refactor of agent-touching code needs the safety net.

- [x] **M2.P1** `.cursor/12-ai-feature-lifecycle.mdc` — the rule (gap #4 enforcement). *Slug: `ai-feature-rule`.*
- [x] **M2.P2** `.claude/skills/ai-feature/SKILL.md` — the orchestrator. *Slug: `ai-feature-skill`.*
- [x] **M2.P3** SPEC.md template + 5 retroactive SPEC scaffolds. *Slug: `agent-specs-retroactive`.*
- [ ] **M2.P4** Baseline eval datasets per agent (≥20 examples each). *Slug: `agent-eval-datasets`. **OPEN — major work.***
- [x] **M2.P5** `.github/workflows/eval-gate.yml` — G2 CI gate. *Slug: `g2-eval-gate`.*
- [ ] **M2.P6** MCP integration test harness — full 40+ tool coverage (T-M3 expansion). *Slug: `mcp-harness-full`. **Partial — schema tests landed, smoke tests scaffolded.***
- [ ] **M2.P7** Eval drift cron + `mcp-ontobricks` smoke probe. *Slug: `eval-drift-cron`. **Depends on M2.P4.***

## M3 — Quality enforcement in CI

Theme: turn the discipline into automation so it can't decay.

- [ ] **M3.P1** ruff + mypy in CI with baseline. *Slug: `ruff-mypy-ci`. **OPEN — requires baseline coordination.***
- [x] **M3.P2** Changelog presence gate (gap #9). *Slug: `changelog-presence-gate`.*
- [ ] **M3.P3** E2E in nightly CI. *Slug: `nightly-e2e`. **Scaffolded — `nightly.yml` exists; needs DAB staging target.***

## M4 — Monolith splits (architecture cleanup)

**Hard precondition:** M2 must be done (eval datasets + G2 gate). Refactoring agent-touching code without eval is reckless.

- [ ] **M4.P1** Decompose `DigitalTwin.py` (3525 LOC). *Slug: `digitaltwin-split`.*
- [ ] **M4.P2** Decompose `SparqlTranslator.py` (2407 LOC). *Slug: `sparql-translator-split`.*
- [ ] **M4.P3** Decompose `SettingsService.py` (2148 LOC). *Slug: `settings-service-split`.*

## T-M — Testing milestones (from Section 9)

Built on top of M1–M4. Each phase fills coverage gaps surfaced in §9.0.

- [x] **T-M0** Test foundations (P1–P6). *Landed 2026-05-12 on `cns/test-foundations`.*
- [ ] **T-M1.P1** SHACL parser/generator/service unit tests. **Partial — sample landed (25 tests), can expand to ~80.**
- [ ] **T-M1.P2** SparqlTranslator direct unit tests (2407 LOC → ~120 tests). *Slug: `sparql-translator-units`.*
- [ ] **T-M1.P3** DigitalTwin direct unit tests (3525 LOC → ~70 tests). *Slug: `digitaltwin-units`.*
- [ ] **T-M1.P4** `src/back/core/logging/` unit tests. *Slug: `logging-units`.*
- [ ] **T-M1.P5** `src/back/core/errors/` direct unit tests. *Slug: `errors-units`.*
- [ ] **T-M2** Integration tier (P1–P6: Delta sync, Lakebase, R2RML, OpenAPI/GraphQL contracts, error propagation). *Slugs: `delta-sync-integration`, `lakebase-integration`, `r2rml-complex`, `openapi-contract`, `graphql-contract`, `error-propagation`.*
- [ ] **T-M3 (full)** MCP integration — all 40+ tools × 4 tests each (extends sample). *Slug: `mcp-harness-full` (shared with M2.P6).*
- [ ] **T-M4** Agent eval harness — drives M2.P4 + thresholds. *Slug: `agent-evals`.*
- [ ] **T-M5** E2E nightly user journeys (7 marquee flows). *Slug: `e2e-journeys`.*
- [ ] **T-M6** Hypothesis property tests for W3C translators. *Slug: `w3c-property-tests`.*

---

## Out-of-scope (deferred)

- Mutation testing (mutmut) — needs CI budget; revisit after M4.
- Performance benchmarks (pytest-benchmark) — useful but not blocking.
- Real-Databricks `external` tier in CI (https://fevm-ontobricks-int.cloud.databricks.com) — workflow scaffolded, needs `ONTOBRICKS_INT_*` GitHub secrets to enable.

## Conventions

- **One PR per phase.** No mega-PRs spanning milestones.
- **Branch name = slug.** `git checkout -b ai-feature-skill` for `M2.P2`.
- **PR title prefix = milestone.** `feat(M2.P2): ai-feature skill orchestrator`. Enforced by commitlint (M1.P6).
- **PR body links the Issue + plan file.** `.planning/<slug>/PLAN.md` is the resumption substrate.
- **Closing the loop:** when the PR merges, mark the bullet `[x]` and add a one-line note (`Landed YYYY-MM-DD on <branch-or-PR>`).

## Last updated

2026-05-14 — this file bootstrapped as part of the CNS rollout (commit `cns/test-foundations` HEAD + this PR).
