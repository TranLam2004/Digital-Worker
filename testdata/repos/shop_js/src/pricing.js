'use strict';

// Round half up to the cent (accounting rule).
function withTax(amountCents, ratePercent) {
  return Math.floor((amountCents * (100 + ratePercent) + 50) / 100);
}

function applyDiscount(amountCents, pct) {
  return Math.floor((amountCents * (100 - pct) + 50) / 100);
}

module.exports = { withTax, applyDiscount };
