package shop;

public final class Pricing {
    private Pricing() {}

    /** Adds ratePercent tax, rounding half up to the cent. */
    public static long withTax(long amountCents, long ratePercent) {
        return (amountCents * (100 + ratePercent) + 50) / 100;
    }
}
