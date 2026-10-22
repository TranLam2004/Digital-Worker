---
name: localize-3-tier
description: "Find where to fix in 3 tiers: ripgrep (0 tokens) → skeleton (top-5 files) → full read (function/lines). Returns the top-3 candidates."
version: 1
used_by: [planner]
trigger: always
budget_tokens: 600
source: core
---

## When to apply

Every LOCALIZE. "Fixing the right place matters more than fixing it elegantly" — wrong localization is the #1 cause of failure. The cheap → expensive order is **mandatory**; reversing it burns tokens without gaining accuracy.

`complexity: trivial` → stop after Step 2 (fast-path).

## Procedure

### Step 1 — ripgrep (0 LLM tokens)
1. For each `intake.entry_hints[]`: `fs.search(hint, max=20)`.
2. Stack trace in the issue → parse `file:line` → provisional candidate, confidence 0.7.
3. Collect files with ≥1 hit; count hits per file. More hits ≠ more likely — but 0 hits = eliminated.
4. Filter out `node_modules/`, `vendor/`, `*.min.*`, `dist/`, `build/`, `*.lock`.
5. Result: ≤15 candidate files.

### Step 2 — skeleton (1 call)
1. Take `REPO.md#module_map` → the module matching `intake.affected_area` → its directory.
2. `fs.list(dir, depth=2, skeleton=true)` → file names + function/class signatures, **no bodies**. ≤30 files.
3. Intersect with Step 1 → prefer files that both have hits and sit inside the module.
4. **1 LLM call**: "The issue describes X. Here is the skeleton. Pick the top-5 files, one-line reason each."
5. `trivial` → stop. Candidate #1 = top-1 file, confidence from the LLM, `localize_method: [ripgrep, skeleton, fast_path]`.

### Step 3 — full read (1–2 calls)
1. `fs.read` the 5 files. File > 300 lines → `codegraph.search(symbol)` → `fs.read(path, lines=...)` only the relevant range.
2. For the most suspicious symbol: `codegraph.callers(symbol)` → who calls it. `codegraph.impact(symbol)` → what breaks.
3. **1 LLM call**: "Here are 5 files. Issue X. Rank 3 candidates: file + symbol + line range + confidence + reason. Candidates must differ in **approach**, not just in file."
4. Fill `callers_affected` from codegraph. Fill `hot_files_touched` by comparing with `REPO.md#hot_files`.

### Step 4 — episodes (0 LLM)
1. `memory.search(module=affected_area, type=intake.type, kind=episode, limit=5)`.
2. Failed episode with a `lesson` → check whether a candidate repeats the same mistake → adjust `files` (e.g. add the test/mock file).
3. Passed episode with the same pattern → raise the similar candidate's confidence by +0.1.

## Known pitfalls

- **Reading files before grepping** — costs 3–5× the tokens. Step 1 is 0 tokens; always first.
- **One candidate** — rework #3 has no path to re-localize → early BLOCKED. Minimum 2.
- **3 candidates in the same file, different lines** — that is 1 candidate, not 3. Candidates differ in **approach**: fix in the handler vs in the repository vs in the middleware.
- **Trusting the stack trace absolutely** — the trace shows where it **blew up**, not where it is **wrong**. Confidence 0.7, not 0.95.
- **Ignoring test files** — a bug fix needs a test; candidate `files` must include the matching test file (look for `tests/test_<module>*`).
- **Forgetting callers** — changing a signature without knowing the callers → the Verifier's regression catches it, costing one rework.
- **Skeleton too wide** — > 30 files → the output gets artifactized → useless. Narrow by module first.

## Output checklist

- [ ] `localize_method` lists the steps actually run, in order
- [ ] ≥2 candidates, ≤3
- [ ] Candidates differ in approach, not just file
- [ ] Candidate #1 has `symbols` + `lines`
- [ ] `callers_affected` filled (from codegraph or `fs.search` of the function name)
- [ ] `hot_files_touched` compared against REPO.md
- [ ] Bug → `steps[0].action == write_failing_test`
- [ ] Test file in `candidates[0].files`
- [ ] Total LLM calls ≤ 3 (trivial: ≤ 1)

## With a CodeGraph index

When `.codegraph/` exists (built at onboarding), use it in Step 3 instead of text search: `codegraph.search` resolves a symbol to its definition in one call; `codegraph.callers` replaces `fs.search(function_name)` for `callers_affected`; `codegraph.impact` gives the blast radius that breaks ties between candidates with similar confidence. Without the index, fall back to `fs.search(function_name)` and fill `callers_affected` from its hits.
