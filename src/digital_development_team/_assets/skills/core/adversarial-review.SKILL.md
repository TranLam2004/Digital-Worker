---
name: adversarial-review
description: Verifier attacks the patch from ≥3 angles to find at least one defect or prove none exists with evidence. Beats sycophancy by structure, not will.
version: 1
used_by: [verifier]
trigger: always
budget_tokens: 900
source: core
---

## When to apply

Every VERIFY; the Verifier's **primary** skill (`differential-review`, `py-review` add angles when triggered).

The task is **inverted**: **"find at least one case where the patch is WRONG, or prove none exists"**. "No issues" without evidence is **your FAIL**. You do not see the Dev's reasoning or `impl_log.md`, by design; do not look for them.

## Procedure

### Phase 1 — Read without judging (1 pass)
1. Read `spec.yaml` → one checklist line per AC.
2. Read `machine_gate` → did build/lint/test pass? **Pass ≠ AC met.** A test can be `assert True`.
3. `git.diff()` → read the **whole** diff once. Note the files, functions and tests added/changed.

### Phase 2 — Mechanical checks (tools only, 0 LLM)
4. **Scope:** files in diff ⊆ `allowed_files`? No → verdict `fail` (the harness records `scope_violation`); stop this phase.
5. **Test tamper:** diff touches **existing** tests (removed `assert`, added `@skip`/`.skip(`/`xfail`, changed expected value)? → each such line is **suspicious by default**; no AC requires it → `fail`.
6. **Disabled checks:** new `# noqa`, `# type: ignore`, `eslint-disable`, `verify=False`, `--no-verify` → finding `major`.
7. `test.run()` (default `spec.test_command`; **never change it** — `-k test_happy` makes "pass" meaningless) → copy `summary_line` verbatim.
8. `test.run(cmd=spec.regression_command)` → `regression.result` + `failed_tests[]`.

### Phase 3 — Grade each AC
9. Each AC → exactly one `criteria[]` line:
   - `verify_by: test` → that test present and passing in the output? Evidence = `summary_line` or the `PASSED test_x` line.
   - `diff_inspection` → cite the proving hunk: `"src/api/users.py:47-52 adds try/except UserNotFound → return 404"`.
   - `lint`/`typecheck` → from `machine_gate`.
   - `manual` → `unclear`. Do not guess.
10. Evidence < 10 characters or prose ("looks right") → the harness discards it. Rewrite.

### Phase 4 — Attack (≥3 angles fitting the patch, all in `adversarial_attempted[]`)

| Angle | Question | How to check |
|---|---|---|
| **Boundary input** | Empty, null, negative, 0, MAX_INT, unicode, too long? | Tested? No → `test.run(filter=...)` if parametrised, else finding "AC misses input X" |
| **Error path** | Swallowed? `except: pass`? What does the caller get? | Read every `try/except`/`catch` in the diff |
| **Callers** | Signature changed? Who calls it? | `fs.search(function_name)` → every call site still correct? |
| **State / concurrency** | Shared state, race, idempotency? | Only if the patch touches global/cache/db |
| **Reward hacking** | "How would I pass the tests while still wrong?" | Hardcoded return? Catch-all? Happy-path-only tests? |
| **Fake pass** | Would the test also pass without the fix? | Asserts a constant, catches the exception it should raise, tests a mock → its AC `fail` |
| **Private internals** | Test uses the public path? | Monkeypatches `_private`, pokes `__dict__`, re-implements the logic → `major` |
| **AC citation** | Patch cites an AC id as justification? | Open that AC: does it say that? No → orphan hunk |
| **Scope creep** | Which hunk serves no AC? | Map hunk → AC. Orphan → `minor`; several → `major` |
| **Repeated warning** | Same tool warning 3+ times? | A signal: trace it to its source |
| **Security lite** | Secret? Sensitive data logged? Unvalidated input? | grep the diff |

Found → `adversarial_findings[]` with `severity` + `location` + `description`. Nothing → record the angle anyway: "tried X, no issue because Y".

### Phase 5 — Verdict
11. All AC `pass` + regression `pass` + no `blocking` finding → `pass`.
12. ≥1 AC `fail`, a `blocking` finding, scope violation or test tamper → `fail`.
13. Any `unclear`, or tests could not run → `inconclusive` + `skip_reason`.
14. `fail` → `failure_feedback_for_dev`: **≤3 sentences with `file:line`, saying WHAT is wrong, NOT how to fix it.**

## Known pitfalls

- **Trusting comments** — `// fixed null case` is not evidence. A passing test is.
- **Generic `adversarial_attempted`** — "reviewed carefully" is not an angle. Be concrete: "input id=-1 → path X → returns 404".
- **Lower bar for small patches** — a wrong 3-line patch is still wrong; canaries are small.

## Output checklist

- [ ] Every spec AC has exactly one `criteria[]` line
- [ ] Every `evidence` ≥10 characters, command output or `file:line`
- [ ] `test.run` ran with the **exact** `spec.test_command`
- [ ] `regression.ran = true` (or `skip_reason`)
- [ ] `adversarial_attempted` ≥ 3 angles, one concrete sentence each
- [ ] Scope check: diff ⊆ allowed_files
- [ ] Test tamper check: no old test edited outside an AC
- [ ] Every new test would fail without the fix (fake-pass angle)
- [ ] `fail` → `failure_feedback_for_dev` ≤3 sentences, with `file:line`
- [ ] No `pass` rests on a comment or on machine_gate alone
