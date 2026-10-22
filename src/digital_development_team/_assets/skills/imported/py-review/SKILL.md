---
name: py-review
description: "Verifier scans new Python lines with a severity-ranked pattern table (bare except, mutable default, shell=True, eval, missing `from e`); confirms in context."
version: 1
used_by: [verifier]
trigger: "any_match(spec.allowed_files, '*.py')"
budget_tokens: 900
source: imported
origin: https://github.com/CodeSigils/py-review-skill/tree/26afe610c8116a1a50157e7b5c00ca87a8451d05/skills
license: MIT
imported_at: 2026-10-04
adapted: true
---

## When to apply

VERIFY when the diff has `.py` files. Consider only **new/changed lines in the diff**; untouched old code is not your business. Goal: correctness and maintainability defects backed by evidence, not style.

## Procedure

1. `git.diff()` → the `.py` files and their `+` hunks. None → record "py-review: no .py lines in diff" in `adversarial_attempted[]` and stop.
2. Per file, `fs.search(pattern, path=<file>, regex=true)` for each row; keep only hits on `+` lines of the diff:

| Pattern (regex) | Problem | Severity |
|---|---|---|
| `except\s*:` · `except Exception\s*:\s*(pass\|\.\.\.)` | swallows errors, hides bugs | major |
| `raise \w+\(` inside `except … as e` without ` from e` | loses the cause chain | minor |
| `^\s*assert .*` used to validate runtime input | `-O` strips asserts | major |
| `def \w+\(.*=\s*(\[\]\|\{\}\|set\(\))` | mutable default, state leaks between calls | major |
| `open\(` outside a `with` / never `close()`d · same for socket, lock, session | resource leak | minor |
| `shell=True` · `eval\(` · `exec\(` · `pickle\.loads\(` on input | untrusted execution/deserialization | blocking |
| `== None` · `type\(\w+\) ==` | wrong semantics, breaks with subclasses | minor |
| **hard-coded** URL/host/path/timeout/token in new code (not tests) | config mixed into code, leak risk | major |
| hand-rolled retry/timeout at a call site when the repo already has a wrapper | inconsistent error semantics | minor |
| `for … in batch:` where one exception stops the whole loop (errors not collected) | partial failure loses data | minor |

3. Each hit: `fs.read(path, lines="a-b")` ±5 lines to confirm a real defect, not a legitimate exception (a boundary that logs + re-raises; a test fixture; a harmless local constant). Not confirmed → drop it; it counts as a hit only.
4. Record each confirmed hit in `evidence.adversarial_findings[]`: `severity` · `file:line` · pattern · one-sentence consequence. `blocking`/`major` → `verdict` cannot be `pass`, even if no AC catches it. Record in `adversarial_attempted[]`: "py-review: N patterns, M hits, K confirmed".
5. **Never print a secret** you find; record only location + type and recommend revocation.
## Known pitfalls

- Reporting style (naming, import order) as findings → dilutes the evidence.
- Findings on **unchanged** code → the Dev cannot fix them within scope; the Leader stops trusting the report.
- `except Exception` at a boundary with `log.exception` + re-raise is legitimate.
- **Newly added** `# noqa`/`# type: ignore`/`# nosec` → not this table's job; hand it to `adversarial-review` to judge intent.

## Output checklist

- [ ] Only `.py` files in the diff, only `+` lines
- [ ] Each finding: pattern + `file:line` + severity + consequence
- [ ] No purely stylistic finding
- [ ] `adversarial_attempted[]` has a py-review line with pattern/hit counts

Source: https://github.com/CodeSigils/py-review-skill (MIT) — condensed and adapted to DDT tools.
