---
name: escalation-form
description: How to ask a human well — A/B options + cost + risk + recommendation, so they decide in 10 seconds instead of 10 minutes. Never "I can't do it".
version: 2
used_by: [analyst, dev, leader]
trigger: "needs_user_ask"
budget_tokens: 400
source: core
---

## When to apply

Before you write `escalation.yaml`. Use it when you **cannot continue within the constraints** — not when the work is merely hard.

> Our agents do not ask "may I do it this way?". They ask "A or B — I lean A for this reason". This turns human-in-the-loop from a **bottleneck** into **leverage**.

Each agent gets ≤2 escalations per issue. On the 3rd the harness picks your own recommendation for you and runs you again.

## Procedure

### 1. Determine `type`
| You are facing | type |
|---|---|
| Spec/issue admits ≥2 readings | `ambiguity` |
| An action is risky and needs confirmation | `risk` |
| Cannot do it within the constraints (retries exhausted, no build, …) | `capability` |
| Two issues collide | `conflict` |
| Need to act on the G2 list (delete, migration, CI, dependency) | `destructive_request` |
| Budget nearly exhausted | `budget` |

Decision rule: take the first matching row, top to bottom. `capability` and `budget` must be provable from tool output (retry count, remaining budget); the other types require you to name the exact point of conflict.

### 2. Write `question` — concrete, ≥20 characters
- ✓ "Spec says 'keep backward compatibility' — does that include API v1, deprecated 6 months ago (src/api/v1/)?"
- ✗ "How should I do this?" · "I'm not sure" · "Please take a look"

Name the **exact** point of contradiction. Point to file/line/field in `context_refs` so the human opens the right thing first.

### 3. Write ≥2 options — four fields each
```yaml
- id: A
  action: "Keep v1, add an adapter"              # what to do — one sentence
  cost: "+2 files, +1 test, ~15 min"             # what it costs — numbers
  risk: low                                      # low | medium | high
  consequence: "Longer code, nothing breaks"     # what happens as a result
```
- Options must **genuinely differ in approach**, not "A: do it, B: don't" (except `capability`, where the last option is `abort/skip`).
- `cost` is numeric: files, lines, minutes, tokens. Never "a lot"/"a little".
- ≤4 options. More means you have not finished thinking.
- Optional `route` tells the harness what to do if the option is chosen. Omit it and you are simply run again with the
  answer (the usual case). `abort` stops the issue (use it for the abort/skip option of `capability`). `rework` /
  `re_localize` / `re_spec` send the issue back to IMPLEMENT / LOCALIZE / SPEC. `merge` (Leader at G1 only) merges now.
- `spec_patch: {allowed_files_add, test_command, regression_command}` when the spec blocks you. A recommended
  option that only adds ≤3 test files or files next to an allowed file is applied without a human (once per issue).

### 4. Recommendation — you must take a side
- `recommendation: A` + `recommendation_reason` in one sentence.
- **Never** "up to you". You have the context; the human does not. You must lean.
- `confidence`: how sure you are of the recommendation. Low is acceptable, but it must be present.

### 5. Write `escalation.yaml` with `blackboard.write` → your turn ends. **Do not ask again.**
The harness opens a human gate (G0 before code, G1 from IMPLEMENT on). When you run again, the answer is in your brief
under **Human answers**: the chosen option, its action and the human note. Follow it. Record the choice where the next
stage reads it (`spec.ambiguities[].assumed` for Analyst, `impl_log` for Dev). A form that breaks the rules below is
rejected before any human sees it; fix it and write again.

## Known pitfalls

- **Asking because it is hard, not because you are stuck** — "AC3 is really hard" is not an escalation. Hard is your job. Budget truly gone → `budget`. Impossible within the allowlist → `ambiguity` with a `spec_patch`.
- **Fake options** — "A: follow the spec. B: do nothing" is not two options. B must be a different approach.
- **No recommendation** — the human must think from scratch: 10 minutes instead of 10 seconds. All value lost.
- **Vague cost** — "takes time" is useless. "+15 min, +2 files" can be compared.
- **Asking a 3rd time** — the harness answers with your own recommendation. Asking again after that blocks the issue.
- **Dev asking on behalf of Analyst** — "the spec is ambiguous" was already decided by Analyst (`assumed`). Dev hits ambiguity → read `spec.ambiguities[].assumed` first. Ask only when it is absent.

## Output checklist

- [ ] `type` is the correct category
- [ ] `question` ≥20 characters, points to a concrete location
- [ ] ≥2 options that differ in approach
- [ ] Every option has `action`, `cost` (numeric), `risk`, `consequence`
- [ ] `recommendation` ∈ options + `recommendation_reason`
- [ ] `confidence` has a value
- [ ] `context_refs` points to the files/slots the human should look at
- [ ] `capability` → an abort/skip option exists
- [ ] This is your ask #≤2 in this issue
