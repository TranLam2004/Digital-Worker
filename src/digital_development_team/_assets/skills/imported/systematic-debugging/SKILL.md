---
name: systematic-debugging
description: "Find the root cause before fixing — reproduce, trace data flow backwards, one hypothesis per change, stop after 3 misses. Counters symptom patching."
version: 1
used_by: [dev]
trigger: "intake.type == 'bug'"
budget_tokens: 800
source: imported
origin: https://github.com/obra/superpowers/tree/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/systematic-debugging
license: MIT
imported_at: 2026-10-04
adapted: true
---

## When to apply

`intake.type == bug`, **before** changing any line of code. Iron law: **no fix until the root cause is pinned down** (`file:line` + why). A symptom fix = fail, even with green tests.

## Procedure

1. **Read the error verbatim** from `spec.yaml`/`plan.yaml` (`blackboard.read`). Do not guess. Copy the exact error message into `impl_log.md`.
2. **Reproduce** in the worktree: `test.run(filter=<related test>)` or `exec.run` with the issue's exact repro. Reproduced → record command + key output line in `impl_log.md`. Not reproduced → no fix allowed yet; record what was run and return to step 1.
3. **Trace the data flow backwards** from the crash site to its source: `fs.read` the failing function → `codegraph.callers` (or `fs.search` the function name) → read each caller. At every function/module boundary write one line in `impl_log.md`: "value in, value expected".
4. **Compare with a working path**: `fs.search` for a similar spot that works (same pattern, different input). Where they differ → root-cause candidate.
5. **One hypothesis → one change → check**: state the hypothesis in one sentence, change exactly one place, `test.run(filter=)`. Wrong → undo it (reverse `fs.edit`), next hypothesis. Never change two things at once. Log each hypothesis + outcome in `impl_log.md`.
6. Root cause found → write `Root cause: file:line — <why>` in `impl_log.md`, then **write the failing reproduction test first** (switch to `test-driven-development`), and only then fix.
7. **3 wrong hypotheses in a row** → stop. Write `escalation.yaml` (type `capability`) following `escalation-form`: what was tried, what was ruled out, which part of the architecture is suspect. No 4th attempt.

## Known pitfalls

- "Patch it now, do it properly later" — there is no "later" inside one issue.
- Adding `try/except`, a default value or `if x is None` to hide the error instead of finding why x is None.
- Trusting comments, commit messages or function names instead of running the code and reading the output.
- Changing several places in one go, getting green → no idea which change mattered.
- Taking the stack-trace frame as the root cause — it shows where it **blew up**, not where it went **wrong**.
- Editing existing tests to make them pass.
- After a context compaction or resume, assuming the repro or a hypothesis check already ran — re-run it.

## Output checklist

- [ ] `impl_log.md` has a "Root cause: `file:line` — <why>" line before any fixing diff
- [ ] A reproduction step (command + output) is in `impl_log.md`
- [ ] A fail-before/pass-after test exists for that root cause
- [ ] ≤ 3 hypotheses tried, one change each
- [ ] No file outside `spec.allowed_files` touched

Source: https://github.com/obra/superpowers (MIT) — condensed and adapted to DDT tools.
