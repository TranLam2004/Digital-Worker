'use strict';

class Cart {
  constructor() {
    this.items = [];
  }

  add(sku, qty, priceCents) {
    this.items.push({ sku, qty, priceCents });
  }

  totalCents() {
    return this.items.reduce((t, it) => t + it.qty * it.priceCents, 0);
  }
}

module.exports = { Cart };
