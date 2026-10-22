---
name: beyond-mvs
description: "Contest CR/BUG: the MVS is the minimum. Every MVS scenario must pass, and the grader also runs scenarios beyond it — cover the obvious neighbours too."
version: 2
used_by: [analyst, verifier]
trigger: "default(match.type, '') in ['CR', 'BUG']"
budget_tokens: 450
source: core
---

## When to apply

The item comes from the contest Game Master and `match.json` lists `mvs` (CR and BUG). Passing the MVS is required but
not sufficient: the grader also checks scenarios the MVS does not show.

## Procedure

1. Analyst: copy each `match.mvs[].id` into an AC's `mvs_ids` list. Set `verify_by: test`, an exact `test_ref`,
   and a `test_command` executing those scenarios (for example `pytest -q tests/test_api.py::test_empty`).
   Every MVS must be covered. Several scenarios may share an AC only if its command executes all of them.
   The harness rejects missing mappings and reruns these commands after rebase.
2. For each changed rule, list its neighbours and cover them: empty / zero / one element · boundary values (just below,
   at, just above) · invalid or missing input · the unchanged happy path that already worked · the same rule reached
   through a second entry point (other endpoint, caller, CLI).
3. Analyst: state these as ACs with `verify_by: test` and put the test file in `allowed_files`.
   Verifier: run them and record every AC result with nonempty evidence and `evidence_type: test_output` for MVS.
   Keep tests in the committed patch so the harness can rerun them; a neighbour that fails is a `fail`.
4. Regression must still pass: a fix that breaks something else scores zero for BUG.

## Known pitfalls

- Hard-coding the MVS example values (`if x == 3: return 2`) passes the MVS and fails the grader.
- Changing an error message or type the MVS does not mention can break hidden scenarios — keep the existing contract
  unless the item says otherwise.

## Output checklist

- [ ] every MVS scenario ↔ one AC / executed check
- [ ] edge cases of each changed rule covered
- [ ] regression passes
