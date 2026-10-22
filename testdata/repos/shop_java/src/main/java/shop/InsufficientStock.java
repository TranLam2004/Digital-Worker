package shop;

public class InsufficientStock extends RuntimeException {
    public InsufficientStock(String sku, int requested, int available) {
        super(sku + ": requested " + requested + ", available " + available);
    }
}
