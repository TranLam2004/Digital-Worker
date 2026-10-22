---
name: ask-questions-if-underspecified
description: "Before specifying a vague issue — list 6 gaps, search the repo first, split safe assumptions from blockers, batch blockers into one A/B ask."
version: 1
used_by: [analyst]
trigger: "intake.confidence < 0.7"
budget_tokens: 500
source: imported
origin: https://github.com/sickn33/antigravity-awesome-skills/tree/main/skills/ask-questions-if-underspecified
license: MIT
imported_at: 2026-10-04
adapted: true
---

## When to apply

SPEC when `intake.confidence < 0.7` or `intake.complexity == unknown`. Runs **before** `spec-writing`. Goal: the **minimum** number of questions needed to avoid building the wrong thing; never ask what you can look up.

## Procedure

1. **List the gaps** under 6 headings, one line each, "known / unknown": (a) goal and definition of done; (b) scope — which files/modules, what is **not** done; (c) constraints — compatibility, performance, APIs that must not change; (d) interface/format — input/output, what errors return; (e) edge cases — empty, negative, duplicate, concurrent; (f) existing repo patterns to follow.
2. **Search the repo first**: `fs.search` function names/error messages, `codegraph.search`/`codegraph.callers` for related functions, `blackboard.read(plan.yaml)` candidates, REPO.md Conventions. Each item the repo answers → strike it from the list.
3. **Comparative or analogy phrases** ("same as X", "like the other screen", "consistent with the old flow") → locate X (`fs.search`/`codegraph.search`) and write out every plausible reading explicitly (copy X's behaviour? its layout? its validation? its data source?). Blocker **only if the readings lead to different code**; if they converge on the same diff, pick one and record it as `assumed`. Put the readings in the `question` of `spec.ambiguities[]`.
4. **Classify what remains**: *safe assumption* (a wrong pick is cheap to fix, breaks no API) → record in `spec.ambiguities[] {question, assumed}` and move on; *real blocker* (a wrong pick = rework or broken user-facing behaviour) → step 5.
5. **Ask once** via `escalation-form`: ≤ 3 questions, each A/B(/C) with the **recommendation in bold** + a one-line consequence; point to a concrete `file:line`/field. Call `user.ask` only when ≥1 blocker exists; none → do not ask.
6. Answer received (or proceeding on `assumed`) → **restate in 1–3 sentences** in `spec.goal`: scope, constraints, definition of done. Then `spec-writing`.

## Known pitfalls

- Asking "how would you like it?" — an open question makes the human think for you.
- Asking in several rounds; asking what is already in the code or in `item.body`.
- Treating every ambiguity as a blocker → turns automation into manual work.
- Silently picking one reading of "same as X" — the requester meant another, and the patch becomes rework.
- Asking, then writing the spec as before without reflecting the answer.

## Output checklist

- [ ] All 6 headings reviewed; nothing the repo answers was asked
- [ ] Every comparative/analogy phrase has its readings listed; blocker only if they diverge in code
- [ ] Every remaining ambiguity is in `spec.ambiguities[]` with `assumed` **or** was asked
- [ ] ≤ 1 `user.ask` round, ≤ 3 questions, each with a recommendation
- [ ] `spec.goal` restates scope + definition of done

Source: https://github.com/sickn33/antigravity-awesome-skills (MIT) — condensed and adapted to DDT tools.
