package shop

// WithTax adds ratePercent tax to amountCents, rounding half up to the cent.
func WithTax(amountCents, ratePercent int64) int64 {
	return (amountCents*(100+ratePercent) + 50) / 100
}

// ApplyDiscount removes pct percent, rounding half up to the cent.
func ApplyDiscount(amountCents, pct int64) int64 {
	return (amountCents*(100-pct) + 50) / 100
}
