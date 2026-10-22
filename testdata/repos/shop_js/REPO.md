# REPO.md — shop (JavaScript)

## Overview
Mini cart / inventory / pricing library. Amounts are integer cents. Pricing rounds half up (accounting rule).

## Layout
- `src/inventory.js` · `src/pricing.js` · `src/cart.js`
- `test/shop.test.js` — node:test

## Build & test
| Step | Command |
|---|---|
| Build | `—` |
| Lint | `—` |
| Typecheck | `—` |
| Test | `node --test` |

## Conventions
- Integer cents everywhere, never floating point for money.
- Errors are explicit types (InsufficientStock), never silent clamping.
