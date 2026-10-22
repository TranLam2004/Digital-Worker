namespace Shop.Tests;

public class InventoryTests
{
    [Fact]
    public void ReserveWithinStock()
    {
        var inv = new Inventory();
        inv.Restock("A", 5);
        inv.Reserve("A", 2);
        Assert.Equal(3, inv.Available("A"));
    }
}
