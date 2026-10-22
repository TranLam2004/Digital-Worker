package shop

// Item is one cart line.
type Item struct {
	SKU        string
	Qty        int
	PriceCents int64
}

// Cart is a list of items.
type Cart struct {
	Items []Item
}

func (c *Cart) Add(sku string, qty int, priceCents int64) {
	c.Items = append(c.Items, Item{SKU: sku, Qty: qty, PriceCents: priceCents})
}

// TotalCents sums qty × price over all lines.
func (c *Cart) TotalCents() int64 {
	var t int64
	for _, it := range c.Items {
		t += int64(it.Qty) * it.PriceCents
	}
	return t
}
