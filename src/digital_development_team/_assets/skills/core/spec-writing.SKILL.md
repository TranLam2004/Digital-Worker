---
name: spec-writing
description: "Write spec.yaml as a contract — machine-checkable acceptance criteria, a tight allowlist, a real test_command, a one-line rollback."
version: 1
used_by: [analyst]
trigger: always
budget_tokens: 700
source: core
---

## When to apply

Every SPEC. The spec is a **contract**: the Dev builds to it, the Verifier grades against it, a human decides on it in 15 seconds. Vague spec → Verifier `unclear` → G1 waits for a human → automation just became manual work.

> "Agentic speed does not eliminate engineering discipline; it relocates discipline upstream into specification precision."

## Procedure

### 1. `goal` — one sentence
`blackboard.read("intake.yaml")` + issue body → "This issue is done when ___." Observable behaviour, not implementation.
- ✓ "GET /api/users/{id} returns 404 with the standard error body when the id does not exist"
- ✗ "Fix get_user to catch the exception"

### 2. `acceptance_criteria[]` — one behaviour + one verification each

| `verify_by` | When | Sample AC |
|---|---|---|
| `test` | A test can be written | "Test `test_user_not_found` passes" + `test_ref` |
| `lint` | Style/imports | "Lint passes on the modified files" |
| `typecheck` | Types | "mypy/tsc passes" |
| `diff_inspection` | Code structure | "No other handler in users.py is touched" · "Function X has a docstring" |
| `manual` | **Avoid** — Verifier returns `unclear` | Only if it truly cannot be automated |

Rules:
- **Bug → ≥1 AC `verify_by: test`** with the reproducing test's exact id in `test_ref`. Without it the Verifier can only say `inconclusive`.
- ≤8 AC, ids `AC1`…`ACn`. More → issue too big → `estimated_tier: T2+` + ambiguity "should be split".
- Each AC stands alone — graded separately, none depends on another.
- Last AC is usually `diff_inspection`: "Only files in allowed_files are touched" (harness-enforced; state it anyway).
- `plan.callers_affected` non-empty → one AC per caller: "Test of caller X still passes" (`verify_by: test`, `test_ref` = that test).

### 3. `allowed_files` — tight allowlist
- = `plan.candidates[0].files[].path` + matching test file(s). Every other path is hard-blocked.
- Test file does not exist yet → list it anyway; say "new file" in the AC that uses it.
- **Never** add a file "just in case" — each one is another place the Dev can break.
- > 6 files → suspect the localization or an oversized issue → `estimated_tier: T2`.

### 4. `test_command` — must be real
1. `fs.read("memory/REPO.md")` → `test_commands` has one for this module? Use it verbatim.
2. None → `fs.search("def test_|it\\(|func Test", path=test_dir)` → test file exists? Command = runner + that path.
3. No test file → runner + the new test path from `allowed_files` (the Dev writes it); record the assumption in `ambiguities[]`.
4. **Never guess.** Wrong command → the Verifier cannot run it → the pipeline is worthless.

`regression_command`: `REPO.md#test_commands.full` if < 5 minutes; else the module's subset.

### 5. `rollback_plan` — one line
Default `git revert <sha>`. Feature flag → `disable flag X`. Migration → **not here**: that is T3 + an ambiguity.

### 6. `out_of_scope`
What the issue **hints at** but does **not require**: "normalise the error format across the API — separate issue". Stops over-delivery.

### 7. `ambiguities[]`
≥2 readings → record `question` + `assumed`, pick one, move on. `intake.confidence < 0.7` → `user.ask` via `escalation-form`. High confidence → do not ask; the human sees it at G1.

### 8. `estimated_tier`
Re-estimate after reading the code. `allowed_files` hits `sensitive_paths` → T3. > 6 files / public interface / dependency → T2. Policy takes MAX with `intake.risk_tier`.

## Known pitfalls

- **AC "clean code", "handles errors correctly"** — uncheckable → `unclear`. Rewrite as behaviour: "Empty input → returns 400, does not raise".
- **Test file missing from allowed_files** — the Dev's test write is `tool_denied` → pointless rework. The Analyst's #1 mistake.
- **test_command from the README** — READMEs go stale; CI is the truth, and `REPO.md` was distilled from CI.
- **Writing a CR like a bug** — read a change request twice: "what does the author expect to see differently?" → usually 1–2 more AC on UX/edge cases.
- **Security incident without a PoC AC** — AC1 is **always** "PoC/test reproducing the vulnerability FAILS before the patch, PASSES after". The `security_incident` skill covers the rest.
- **8 AC for a 2-line issue** — over-specification. Trivial → 1–2 AC.

## Output checklist

- [ ] `goal` is one sentence, observable behaviour
- [ ] Every AC has `verify_by`; no `manual` unless unavoidable
- [ ] Bug → ≥1 AC `test` with `test_ref`
- [ ] `allowed_files` ⊇ test file; ≤6 files (or tier raised)
- [ ] `test_command` from REPO.md or its test file verified to exist
- [ ] `rollback_plan` is one line
- [ ] `callers_affected` → matching regression AC
- [ ] `ambiguities` has `assumed` for every question
- [ ] `estimated_tier` re-estimated after reading the code
- [ ] Total ≤ 8 AC
