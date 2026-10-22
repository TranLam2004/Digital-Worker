# REPO.md — shop (Go)

## Overview
Mini cart / inventory / pricing library. Amounts are integer cents. Pricing rounds half up (accounting rule).

## Layout
- `inventory.go` · `pricing.go` · `cart.go` — package shop
- `shop_test.go` — tests

## Build & test
| Step | Command |
|---|---|
| Build | `go build ./...` |
| Lint | `go vet ./...` |
| Typecheck | `—` |
| Test | `go test ./...` |

## Conventions
- Integer cents everywhere, never floating point for money.
- Errors are explicit types (InsufficientStock), never silent clamping.
