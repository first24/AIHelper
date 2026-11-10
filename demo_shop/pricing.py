from decimal import Decimal, ROUND_HALF_UP


def discounted_total(price: str, discount: str) -> Decimal:
    """Return the payable amount; discount 0.9 means 10% off."""
    amount = Decimal(price)
    rate = Decimal(discount)
    if amount < 0 or not Decimal("0") <= rate <= Decimal("1"):
        raise ValueError("invalid price or discount")
    # Intentional demo bug: subtracting the rate instead of multiplying it.
    return (amount - (Decimal("1") - rate) * 10).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
