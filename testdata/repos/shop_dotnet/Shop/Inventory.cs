namespace Shop;

public class InsufficientStock(string sku, int requested, int available)
    : Exception($"{sku}: requested {requested}, available {available}");

public class Inventory
{
    private readonly Dictionary<string, int> _stock = new();
    private readonly Dictionary<string, int> _reserved = new();

    public void Restock(string sku, int qty)
    {
        if (qty <= 0) throw new ArgumentOutOfRangeException(nameof(qty));
        _stock[sku] = _stock.GetValueOrDefault(sku) + qty;
    }

    public int Available(string sku) => _stock.GetValueOrDefault(sku) - _reserved.GetValueOrDefault(sku);

    public void Reserve(string sku, int qty)
    {
        if (qty <= 0) throw new ArgumentOutOfRangeException(nameof(qty));
        _reserved[sku] = _reserved.GetValueOrDefault(sku) + qty;
    }
}
