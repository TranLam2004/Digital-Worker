package shop;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class ShopTest {
    @Test
    void restockAndAvailable() {
        Inventory inv = new Inventory();
        inv.restock("A", 5);
        assertEquals(5, inv.available("A"));
    }

    @Test
    void reserveWithinStock() {
        Inventory inv = new Inventory();
        inv.restock("A", 5);
        inv.reserve("A", 2);
        assertEquals(3, inv.available("A"));
    }

    @Test
    void withTax() {
        assertEquals(1080, Pricing.withTax(1000, 8));
    }
}
