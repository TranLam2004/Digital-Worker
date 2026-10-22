'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { Inventory } = require('../src/inventory');
const { withTax } = require('../src/pricing');
const { Cart } = require('../src/cart');

test('restock and available', () => {
  const inv = new Inventory();
  inv.restock('A', 5);
  assert.equal(inv.available('A'), 5);
});

test('reserve within stock', () => {
  const inv = new Inventory();
  inv.restock('A', 5);
  inv.reserve('A', 2);
  assert.equal(inv.available('A'), 3);
});

test('with tax', () => {
  assert.equal(withTax(1000, 8), 1080);
});

test('cart total', () => {
  const c = new Cart();
  c.add('A', 2, 150);
  assert.equal(c.totalCents(), 300);
});
