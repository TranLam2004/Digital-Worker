---
name: self-review
description: "Dev rereads its own diff before submitting and answers 4 fixed questions. One cheap pass that catches careless errors. Does NOT replace the Verifier."
version: 1
used_by: [dev]
trigger: always
budget_tokens: 300
source: core
---

## When to apply

**Before** every `blackboard.write("patch/")`, after the tests pass. One pass, no more.

Purpose: catch the cheap class of defects (extra files, debug prints, TODOs, over-building, APIs called on a guess) **before** spending a Verifier run. It is not for convincing yourself the patch is correct — that is the Verifier's job, and the Verifier does not trust you.

Economy mode → this skill is switched off. The Verifier still runs.

## Procedure

1. `git.diff(stat=true)` → file count, +/−. More files than `spec.allowed_files`? → an extra file exists; `git.status` to find it.
2. `git.diff()` → read the **entire** diff once, top to bottom.
3. **Run the checklists** ([35-checklists](../../35-checklists/README.md)): the harness already ran the `regex` part of `_base/code` + project-type + domain + learned on your diff — results are in the brief. Any `fail` item → fix it first. Remaining `diff_inspection` items → check them yourself, ≤10 items.
4. Answer **exactly 4 questions**, one line each (what the checklists cannot catch):

   **(a) Right files?** Is every hunk inside `spec.allowed_files`? Does any file carry a hunk it did not need?

   **(b) Nothing extra?** Newly added `print(`/`console.log(`/`debugger`/`TODO`/`FIXME`/`XXX`? Unused imports? Long explanatory comments? "While I was here" code outside the AC? New user-facing text or new config no AC asks for?

   **(c) Nothing broken?** Signature changed → are the plan's `callers_affected` updated? A line deleted/changed that no AC needs? An existing test edited? For every API/method the diff calls: did you read its real signature (`codegraph.node` or `fs.read` of the definition), not trust a plausible name?

   **(d) Not over-built?** An abstraction/helper/config for a single use? Edge cases handled that the AC does not list? A silent **backward-compat shim** (old and new paths kept side by side)? A comment "explaining" something you never verified? Dead code deleted that **you did not create**? Yes to any → remove it; a senior reading the diff should see "exactly enough".

5. **Mutation spot-check present?** `impl_log.md` must have the `mutation:` line from `test-driven-development` for the rule you changed (one condition/value flipped → its test FAILED → reverted). Missing → do it now: flip with `fs.edit`, `test.run(filter=<that test>)` → must fail, revert, rerun green, write the line.
6. Any question answers "problem" → fix → `test.run` again → back to step 1. At most 1 round.
7. Scope beyond the AC that seems worth having (new user-facing text, new config, extra behaviour) → take it out of the diff and record it in `impl_log.md` as an `escalation-form` proposal (A: separate issue · B: include now · recommendation). Never ship it silently.
8. All 4 "fine" → `impl_log.md` ≤10 lines → `git.commit` → `blackboard.write("patch/")`.

## Known pitfalls

- **Self-review turning into self-convincing** — "this is right because I think it is" is not self-review. The 4 questions are about the **diff**, not the **logic**. Logic is the Verifier's job.
- **Skipping because "the patch is small"** — small patches are where debug prints hide.
- **Fixing too much after self-review** — "could be better" → refactor → scope creep. Fix only: extra files, debug/TODO, broken callers, over-building from (d). Nothing else.
- **Looping self-review** — one round. Round two is the Verifier's budget.
- **Calling an API from memory** — a plausible name (`get_by_email`) with a guessed argument order reads fine in review and fails at runtime. Read the definition.
- **A test that would pass without your change** — it guards nothing; that is what the mutation spot-check exposes.

## Output checklist

- [ ] `git.diff(stat)` file count ≤ `allowed_files` count
- [ ] No new `print|console.log|debugger|TODO|FIXME` in the diff
- [ ] No unused imports
- [ ] `callers_affected` handled (or no signature changed)
- [ ] No existing test edited (unless an AC requires it)
- [ ] No API used without reading its signature
- [ ] Mutation spot-check done on the changed rule (test failed under the flip, flip reverted)
- [ ] No abstraction/config/compat shim/edge case beyond the AC; only debris this patch created was cleaned up
- [ ] Scope beyond the AC (new user-facing text, new config) split out as an escalation proposal
- [ ] `impl_log.md` ≤ 10 lines
- [ ] `test.run(spec.test_command)` passed **after** self-review
