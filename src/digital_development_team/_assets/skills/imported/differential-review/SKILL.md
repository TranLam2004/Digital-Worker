---
name: differential-review
description: "Security review of the DELTA — intent vs diff, attack surface by file type, what is NOT in the diff (callers, removed checks/tests), introduced vs pre-existing."
version: 1
used_by: [security, verifier]
trigger: "state.tier != 'T0'"
budget_tokens: 850
source: imported
origin: https://github.com/trailofbits/skills/tree/82fe8226252622fa807643bdca1710901198553a/plugins/differential-review
license: CC-BY-SA-4.0
imported_at: 2026-10-04
adapted: true
---

## When to apply

Security: every REVIEW, `lite` or `full`. Verifier: VERIFY when `tier ≥ T1`, as an extra attack angle for `adversarial-review`. The object is the **diff**, not the file: ask "how does behaviour differ before vs after, and who is affected" — do not grade code quality.

## Procedure

1. **Intent vs diff**: read `spec.goal` + AC, then `git.diff()`. A hunk serving no AC → record it as a finding (scope creep or concealment).
2. **Classify the surface** of each changed file: auth/authz · input parsing/validation · fs path/upload · crypto/secret · dependency/config/CI · test · UI/log. Risk: HIGH (auth, crypto, outbound calls, removed validation) · MEDIUM (business logic, state, new public API) · LOW (comments, tests, logs). LOW only → go to step 6 and list the angles checked.
3. **Each HIGH/MEDIUM hunk**: which assumption of the old code changed? Check **what is not in the diff**: `codegraph.callers` (or `fs.search`) for every function whose signature/default changed → are callers still correct; is a **removed** line a check/validation/test; does a new default "fail open"?
4. **Attack patterns by type**: authz bypass (role check dropped) · unvalidated input flowing deeper · `except` swallowing errors · path traversal/`..` · secret into log/response · new dependency or loosened pin · shell command built from input.
5. **Introduced vs pre-existing**: a defect the diff creates → real severity, blocking if HIGH. A defect that already existed and the diff merely passes by → mark `pre-existing`, do not block, propose a separate issue. Security confirms "since when" with `git.log`.
6. **Record findings**: `severity` · `file:line` · one sentence "how it is exploited" · `introduced|pre-existing`. Security → `sec_report.findings[]`; Verifier → `evidence.adversarial_findings[]`. No finding → list the angles checked in `adversarial_attempted[]`.

## Known pitfalls

- Reviewing the whole file instead of the delta → burns tokens, reports old defects as new.
- Trusting the description in `impl_log.md`/the commit message instead of reading the hunk.
- Grading style/naming — that is the regex checklists' job, not a security review.
- Skipping **removal** hunks (especially tests, asserts, permission checks).
- Reporting a pre-existing defect as blocking → wrongful block, lost credibility.

## Output checklist

- [ ] Every HIGH/MEDIUM hunk maps to an AC or becomes a finding
- [ ] Callers of every function with a changed signature/default were checked
- [ ] Every removed check/validation/test line is explained
- [ ] Each finding has `file:line` + one exploit sentence + `introduced|pre-existing`
- [ ] No finding → `adversarial_attempted[]` lists the angles checked

This adaptation is distributed under the same CC-BY-SA-4.0 licence as its source.
Source: https://github.com/trailofbits/skills (CC-BY-SA-4.0) — condensed and adapted to DDT tools.
