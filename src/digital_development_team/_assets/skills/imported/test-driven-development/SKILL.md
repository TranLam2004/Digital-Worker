---
name: test-driven-development
description: "RED → GREEN → REFACTOR per acceptance criterion; a test you never saw fail proves nothing. Plus evidence-before-claims and a mutation spot-check."
version: 1
used_by: [dev]
trigger: always
budget_tokens: 750
source: imported
origin: https://github.com/obra/superpowers/tree/8ca22dba9a94f28898bbce59f2537ff4d87c747d/skills/test-driven-development
license: MIT
imported_at: 2026-10-04
adapted: true
---

## When to apply

Every IMPLEMENT, for each AC with `verify_by: test` in `spec.yaml`. Iron law: **no production code without a failing test first**. Code written first with "tests later" = start over.

## Procedure

Loop **per AC**, never batched:

1. **Ground** — `fs.read("memory/REPO.md")` Conventions + one neighbouring module and its test → copy their naming, error handling, test style, fixtures. Before calling any API/method, confirm its real signature with `codegraph.node(symbol)` or `fs.read` of its definition — a plausible name is not evidence. Not found → it does not exist; do not call it.
2. **RED** — exactly one test for one behaviour, named for it (`test_reserve_more_than_available_raises`). Real code; mock only when unavoidable. `test.run(filter=<test name>)` (`affected=true` is fine in the inner loop) → must **fail for the right reason** (the behaviour assertion), not ImportError/SyntaxError. Wrong reason → fix the test; do not touch production code yet.
3. **GREEN** — the **minimum** code to pass, cheapest rung first: *skip* (does the AC really need it?) → *reuse* a function already in the repo (`fs.search`) → *stdlib* → *an existing dependency* (never a new one) → *a few lines* → only then write new code. Do not run ahead of the next AC; no incidental refactoring. The ladder **never** weakens validation at a trust boundary, data-loss handling, or a security check. `test.run(filter=)` → pass.
4. **REFACTOR** — only while green: remove duplication, rename. No new behaviour. `test.run(filter=)` stays green.
5. All AC done → `test.run(spec.test_command)` **full**, no filter.
6. **Mutation spot-check** — in each rule you changed, flip one condition or value (`>` → `>=`, drop a guard, change a constant) with `fs.edit`; `test.run(filter=<its test>)` → must **FAIL**. Revert, rerun → green. Still green under the flip → the test does not pin the rule; strengthen it and repeat. Record `mutation: <file:line> <flip> → <test> FAILED, reverted` in `impl_log.md`.
7. **Evidence before claims** (before `blackboard.write("patch/")`): the proof is `test.run(spec.test_command)` run **fresh in this turn**, after the last `fs.edit` (the mutation revert included). Read the whole summary (passed/failed/error), copy `summary_line` into `impl_log.md`. Never write "should pass" or "should work".

## Known pitfalls

- Test passes on its first run → it does not test the new behaviour; fix the test.
- "Too simple to need a test" — simple code still breaks, and the regression goes unguarded.
- Keeping code written first "as a reference" — still test-after.
- Editing existing tests to pass; adding new `skip`, `xfail`, `print`.
- Calling a method because its name sounds right (`repo.get_by_email`) → AttributeError or swapped arguments at runtime.
- Claiming done from an earlier `test.run` (before the last edit).
- After a context compaction or resume, assuming an earlier step was done — re-run the verification (`git.diff()`, `test.run`) before any claim.

## Output checklist

- [ ] Every AC with `verify_by: test` has ≥1 test that **failed, then passed**
- [ ] Every API/method called was checked against its definition
- [ ] New code follows REPO.md Conventions and neighbouring code
- [ ] Mutation spot-check per changed rule: test FAILED under the flip, flip reverted, line in `impl_log.md`
- [ ] Full `test.run(spec.test_command)` passed **after** the last edit; output in `impl_log.md`
- [ ] No existing test edited (unless an AC requires it); no new `skip`/`xfail`/`print`
- [ ] Code is just enough for the AC; no extra behaviour

Source: https://github.com/obra/superpowers (MIT), GREEN ladder from https://github.com/DietrichGebert/ponytail (MIT) — condensed and adapted to DDT tools.
