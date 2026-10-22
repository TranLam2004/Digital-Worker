'use strict';

class InsufficientStock extends Error {
  constructor(sku, requested, available) {
    super(`${sku}: requested ${requested}, available ${available}`);
    this.sku = sku;
    this.requested = requested;
    this.available = available;
  }
}

class Inventory {
  constructor() {
    this.stock = new Map();
    this.reserved = new Map();
  }

  restock(sku, qty) {
    if (qty <= 0) throw new RangeError('qty must be positive');
    this.stock.set(sku, (this.stock.get(sku) || 0) + qty);
  }

  available(sku) {
    return (this.stock.get(sku) || 0) - (this.reserved.get(sku) || 0);
  }

  reserve(sku, qty) {
    if (qty <= 0) throw new RangeError('qty must be positive');
    this.reserved.set(sku, (this.reserved.get(sku) || 0) + qty);
  }
}

module.exports = { Inventory, InsufficientStock };
