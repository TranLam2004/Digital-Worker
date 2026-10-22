---
name: security-5c
description: "Security incident pipeline — Contain → Confirm → Correct → Check → Communicate. Block first, PoC before the fix, repo-wide variant hunt. Not a bug fix."
version: 1
used_by: [security, dev, analyst, planner]
trigger: "intake.type == 'security_incident'"
budget_tokens: 1000
source: core
---

## When to apply

`intake.type == security_incident`. Every pipeline agent loads this; each role reads its part.

> **A bug is between us and the code. A security incident is between us and an outsider.** The clock is already running.

Always T3. Always G0 + G1. No fast-path. No timeout.

| Aspect | Bug | **Security incident** |
|---|---|---|
| First step | reproduce | **CONTAIN — block first, fix later** |
| Scope | related files | **whole repo — variant hunt** |
| Done when | tests pass | tests pass **+ PoC no longer reproduces + variants swept + timeline** |
| Secrets | — | **redacted before the LLM; never rotated by an agent** |

## Procedure — by role

### ① CONTAIN — Planner (LOCALIZE) + Security
1. Planner: from the intake, ask **"how do we block this in <5 minutes without knowing the root cause?"**
   - Feature flag off · disable endpoint/route · rate limit · revoke token/session · IP block · maintenance mode
2. Yes → `plan.needs_containment: true`, `plan.containment_proposal: "<concrete measure>"`.
3. Runner → `user.ask` type `risk` **at once** (A: <measure> · B: not needed because <reason> · recommend A) — **do not wait for the fix**.
4. Containment is a **separate work item**, ahead of the fix: a human does it (outside the repo) or the Dev (flag in code, own allowed_files).
5. Security records `incident_5c.contain.status: proposed|applied|not_needed` + `measure`.

### ② CONFIRM — Analyst (SPEC) + Dev + Security
6. Analyst: **AC1 is always** "PoC/test reproducing the vulnerability FAILS (exploitable) before the patch, PASSES after". `verify_by: test`, `test_ref` = PoC test.
7. Analyst: AC2 "Every variant from the hunt is fixed or has its own issue".
8. Dev: **write the PoC first**. Run it on current code → must **FAIL / be exploitable**. No repro → not the vulnerability as described → `user.ask` type `ambiguity`.
9. Security: `git.log(search="<flawed pattern>")` → introducing commit → `timeline_from`. Redacted attached logs show exploitation? → `exploited_evidence: none_found|suspected|confirmed`.
10. `confirmed|suspected` → Security `verdict: needs_human`; Leader forces T3, no timeout.

### ③ CORRECT — Dev (IMPLEMENT) + Security
11. Dev: fix the root cause within `allowed_files`. **Never** patch only the symptom (one check where the flawed pattern exists in 5 places).
12. Security: name the **CWE/pattern** (SQLi? XSS? IDOR? path traversal? hardcoded secret?). `fs.search(pattern_regex)` over the **whole repo** → `variants_found[]`.
13. Variant inside `allowed_files` → Dev fixes it in the same patch. Outside → Security `user.ask` type `risk` (A: widen the allowlist, fix all N files · B: one P0 issue per variant · recommend B if > 2 files).
14. Security: `variants_fixed_in_this_patch: true|false`.

### ④ CHECK — Verifier + Security
15. Verifier: AC1 → PoC on the **patched** code → PASS. Regression passes.
16. Security: `exec.run:scan` SAST (semgrep/bandit) + `gitleaks` + dependency audit → `sast_clean`, `dependency_scan_clean`.
17. Security: `incident_5c.check.poc_no_longer_reproduces: true`, `regression_pass: true`.

### ⑤ COMMUNICATE — Security
18. `secret_rotation_needed`: secret exposed in code/log/commit → `true` + **a checklist for humans**: "revoke X at Y, rotate Z, update env W". **The agent never does it.**
19. `user_notification_needed`: user data exposed/affected → `true`.
20. 5-line advisory in `sec_report` (or `work/<id>/advisory.md`): **Impact** · **Timeline** (since commit X, detected at Y) · **Fixed** what · **Variants** left/none · **Still needs a human** for what.

## Known pitfalls

- **Treating it as a bug** — skip containment and variant hunt, fix one place, merge. **Most teams fail here.**
- **Containing after the fix** — a 20-minute fix leaves the hole open 20 minutes. Contain first, in <5.
- **PoC after the fix** — it passes from the start and proves nothing.
- **Variant hunting by eye** — use a repo-wide `fs.search` regex; SQLi in one query → almost surely in others.
- **Rotating secrets yourself** — `exec.run` revoke → tool denied → wasted turn. Propose; a human does it.
- **Forgotten redaction** — a real token in an attached log enters the LLM context. The `redact_secrets` hook must run; Security checks `redaction_applied: true`, else `needs_human`.
- **Fixing the symptom** — `if user_id == requested_id` on one endpoint when IDOR spans the controller. Root cause = the authorization middleware.

## Output checklist

**Planner:** `needs_containment` + `containment_proposal` (or why not needed)
**Analyst:** AC1 = PoC test · AC2 = variants · `estimated_tier: T3`
**Dev:** PoC FAILS before → fix → PoC PASSES after · root cause, not symptom
**Security:** `mode: incident_5c` · all 5 Cs have a status · `variants_found[]` · `exploited_evidence` · `secret_rotation_needed` · `redaction_applied: true` · 5-line advisory
**Verifier:** AC1 evidence = PoC test output before/after
**Leader:** `final_tier: T3` · `requires_human: true` · `human_timeout_sec: null`
