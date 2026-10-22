---
name: repo-onboarding
description: Build REPO.md + smoke tests + sandbox image when receiving an unfamiliar repo. Runs once, ~10 minutes; the opening move of the whole run.
version: 1
used_by: [planner, runner]
trigger: "!exists('memory/REPO.md')"
budget_tokens: 800
source: core
---

## When to apply

As soon as the organizers hand over the source code, **in parallel** with reading the backlog. Runs once. If `memory/REPO.md` already exists → skip.

This is the **opening move that decides the game**: it turns the cost of understanding the project from *once per task* into *once per run*.

## Procedure

### Step 1 — Non-LLM collection (2 min, scripted)
1. `fs.list(".", depth=3)` → directory tree. Count LOC per language (`tokei` or `cloc`).
2. Find manifests: `package.json`, `pyproject.toml`, `requirements.txt`, `pom.xml`, `go.mod`, `Cargo.toml`, `Gemfile`.
3. Find entrypoints: `main.*`, `app.*`, `server.*`, `index.*`, `manage.py`, `cmd/`.
4. Find test dirs: `tests/`, `test/`, `__tests__/`, `spec/`, `*_test.go`.
5. Find CI: `.github/workflows/*.yml`, `.gitlab-ci.yml`, `Jenkinsfile` → **read the test/build commands inside**. This is the most trustworthy source.
6. `git log --stat -200 --format=%H` → count the most frequently touched files → **top-5 hot files**.
7. Read `README.md`, `CONTRIBUTING.md`, `Makefile`, `.env.example` (never `.env`).
8. Detect the primary language + runtime version (from manifest / `.python-version` / `.nvmrc` / CI).

### Step 2 — Build + test (3 min) ⚠️ MOST IMPORTANT
1. Build the sandbox image: base per language → install deps from the lockfile → tag `ddt-runner:<repo-hash>`.
2. Inside the sandbox: try the install command (`pip install -r`, `npm ci`, `go mod download`, `mvn -q install -DskipTests`).
3. Try the test command from CI (Step 1.5). None → try the language default (`pytest`, `npm test`, `go test ./...`, `mvn test`).
4. **Record the EXACT command that worked** + duration + pass/fail counts. This becomes `REPO.md#test_commands`.
5. Run a coverage baseline if the runner supports it → `memory/coverage_baseline.json`.
6. `codegraph init` if the language is supported.

**If Step 2 fails (repo does not build/test):** → **FIRST P0 ISSUE**. Stop onboarding. All three people fix it by hand. This is the only exception to the "no manual fixes" rule. Without a runnable test suite, Stage 5 is worthless.

### Step 3 — LLM synthesises REPO.md (3 min, 2–3 calls)
Follow [`_template.REPO.md`](../../50-knowledge/_template.REPO.md). Input: output of Steps 1 + 2. Output: 8 sections, ~2–4k tokens.

Call 1: one-paragraph architecture + module map (name → responsibility → main files) from the tree + README.
Call 2: observed conventions (naming, error handling, test style) from reading 3 representative files + 3 tests.
Call 3 (if needed): 5 high-risk areas from hot files + codegraph impact.

### Step 4 — Smoke tests (2 min)
Repo has no tests, or suite < 5 tests → generate 3–5 "system is alive" tests:
- the main module imports
- the server starts (if web) / CLI `--help` runs
- the main endpoint returns 200 (if any)
Write to `tests/ddt_smoke_test.*`. Commit to main **before** taking any issue.

### Step 5 — Human read-through (2 min)
P3 opens `REPO.md` and fixes mistakes by hand. Check in particular: are `test_commands` correct, are `hot_files` plausible, is anything missing from `sensitive_paths`.

## Known pitfalls

- **Trusting the README** — READMEs are usually stale. The test command in CI is the real one.
- **Skipping Step 2 "for later"** — no runnable tests = the whole pipeline is guessing. There is no "later".
- **REPO.md too long** — > 4k tokens eats into every stage's budget. Module map ≤ 15 lines. Conventions ≤ 10 lines.
- **Forgetting `sensitive_paths`** — onboarding finds `auth/`, `payment/`, `migrations/` → write them to `80-config/sensitive_paths.yaml`, not only REPO.md.
- **Sandbox build with network=none fails** — deps need network to install. Build the image **with** network, run **without**.

## Output checklist

- [ ] `memory/REPO.md` exists, ≤ 4k tokens, all 8 sections
- [ ] `REPO.md#test_commands` has ≥1 command **that passed** in the sandbox
- [ ] `REPO.md#hot_files` has 5 files → copied to `policy.yaml#exclusive_files`
- [ ] `80-config/sensitive_paths.yaml` updated for this repo
- [ ] Sandbox image `ddt-runner:<hash>` built
- [ ] `memory/coverage_baseline.json` (if coverage available)
- [ ] `.codegraph/` index (if supported)
- [ ] Smoke tests committed to main (if the repo lacked tests)
- [ ] P3 has read and signed off (`reviewed_by: P3` in REPO.md frontmatter)
