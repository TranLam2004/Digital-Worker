# REPO.md — shop (Java)

## Overview
Mini cart / inventory / pricing library. Amounts are integer cents. Pricing rounds half up (accounting rule).

## Layout
- `src/main/java/shop/` — Inventory, Pricing
- `src/test/java/shop/ShopTest.java` — JUnit 5

## Build & test
| Step | Command |
|---|---|
| Build | `mvn -q -o compile` |
| Lint | `—` |
| Typecheck | `—` |
| Test | `mvn -q -o test` |

## Conventions
- Integer cents everywhere, never floating point for money.
- Errors are explicit types (InsufficientStock), never silent clamping.
