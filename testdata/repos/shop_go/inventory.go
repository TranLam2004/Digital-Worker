package shop

import (
	"errors"
	"fmt"
)

// InsufficientStock is returned when a reservation exceeds the available stock.
type InsufficientStock struct {
	SKU       string
	Requested int
	Available int
}

func (e *InsufficientStock) Error() string {
	return fmt.Sprintf("%s: requested %d, available %d", e.SKU, e.Requested, e.Available)
}

// ErrQty is returned for a non-positive quantity.
var ErrQty = errors.New("qty must be positive")

// Inventory keeps stock and reservations per SKU.
type Inventory struct {
	stock    map[string]int
	reserved map[string]int
}

func NewInventory() *Inventory {
	return &Inventory{stock: map[string]int{}, reserved: map[string]int{}}
}

func (i *Inventory) Restock(sku string, qty int) error {
	if qty <= 0 {
		return ErrQty
	}
	i.stock[sku] += qty
	return nil
}

func (i *Inventory) Available(sku string) int {
	return i.stock[sku] - i.reserved[sku]
}

// Reserve holds qty units of sku for an order.
func (i *Inventory) Reserve(sku string, qty int) error {
	if qty <= 0 {
		return ErrQty
	}
	i.reserved[sku] += qty
	return nil
}
