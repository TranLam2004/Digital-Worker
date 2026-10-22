package shop;

import java.util.HashMap;
import java.util.Map;

public class Inventory {
    private final Map<String, Integer> stock = new HashMap<>();
    private final Map<String, Integer> reserved = new HashMap<>();

    public void restock(String sku, int qty) {
        if (qty <= 0) throw new IllegalArgumentException("qty must be positive");
        stock.merge(sku, qty, Integer::sum);
    }

    public int available(String sku) {
        return stock.getOrDefault(sku, 0) - reserved.getOrDefault(sku, 0);
    }

    public void reserve(String sku, int qty) {
        if (qty <= 0) throw new IllegalArgumentException("qty must be positive");
        reserved.merge(sku, qty, Integer::sum);
    }
}
