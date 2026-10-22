# REPO.md — shop (.NET)

## Overview
Mini cart / inventory / pricing library. Amounts are integer cents. Pricing rounds half up (accounting rule).

## Layout
- `Shop/` — library · `Shop.Tests/` — xunit

## Build & test
| Step | Command |
|---|---|
| Build | `dotnet build` |
| Lint | `—` |
| Typecheck | `—` |
| Test | `dotnet test` |

## Conventions
- Integer cents everywhere, never floating point for money.
- Errors are explicit types (InsufficientStock), never silent clamping.
