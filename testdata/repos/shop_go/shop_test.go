package shop

import "testing"

func TestRestockAndAvailable(t *testing.T) {
	inv := NewInventory()
	if err := inv.Restock("A", 5); err != nil {
		t.Fatal(err)
	}
	if got := inv.Available("A"); got != 5 {
		t.Fatalf("available = %d, want 5", got)
	}
}

func TestReserveWithinStock(t *testing.T) {
	inv := NewInventory()
	_ = inv.Restock("A", 5)
	if err := inv.Reserve("A", 2); err != nil {
		t.Fatal(err)
	}
	if got := inv.Available("A"); got != 3 {
		t.Fatalf("available = %d, want 3", got)
	}
}

func TestWithTax(t *testing.T) {
	if got := WithTax(1000, 8); got != 1080 {
		t.Fatalf("WithTax = %d, want 1080", got)
	}
}

func TestCartTotal(t *testing.T) {
	var c Cart
	c.Add("A", 2, 150)
	if got := c.TotalCents(); got != 300 {
		t.Fatalf("total = %d, want 300", got)
	}
}
