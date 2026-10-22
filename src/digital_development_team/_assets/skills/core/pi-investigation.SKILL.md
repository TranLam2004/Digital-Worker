---
name: pi-investigation
description: "Production incident: a customer symptom with little information. Reproduce, trace to the root cause, apply the minimal fix — fast, because PI is scored by finish rank."
version: 1
used_by: [planner, dev]
trigger: "intake.type == 'production_incident'"
budget_tokens: 350
source: core
---

## When to apply

`intake.type == production_incident`: the report says what the customer sees ("cannot book", "job crashes"), not
where. Points depend on how fast a PASSED fix lands, so investigate in the cheapest order and stop at the first proof.

## Procedure

1. Translate the symptom into searchable terms: the feature name from the application brief, error words, the
   endpoint / command / job it implies. `fs.search` them; `codegraph.search` for the entry point.
2. Read recent signals first: logs or fixtures in the repo, the failing or skipped tests, config that differs by
   environment, the code path from the entry point down (`codegraph.callers` / `codegraph.node`).
3. Reproduce with a test or a command before changing anything. No reproduction after two hypotheses → write down what
   you checked and try the next most likely layer (input validation → business rule → data access → config).
4. Fix the root cause with the smallest change; do not refactor. Keep the reproducing test as the regression test.
5. Planner: put the reproduction command in `plan.steps` and the suspected file first in `candidates`.

## Known pitfalls

- Fixing the visible symptom (catching the exception) instead of the cause.
- Spending turns reading the whole codebase — follow one path from the entry point.

## Output checklist

- [ ] symptom reproduced before the fix
- [ ] root cause named in `impl_log`
- [ ] reproducing test passes after the fix, regression passes
