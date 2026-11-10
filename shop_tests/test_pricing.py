import unittest
from decimal import Decimal

from demo_shop.pricing import discounted_total


class PricingTests(unittest.TestCase):
    def test_nine_tenths(self):
        self.assertEqual(discounted_total("19.99", "0.9"), Decimal("17.99"))

    def test_no_discount(self):
        self.assertEqual(discounted_total("19.99", "1"), Decimal("19.99"))

    def test_free(self):
        self.assertEqual(discounted_total("19.99", "0"), Decimal("0.00"))

    def test_round_half_up(self):
        self.assertEqual(discounted_total("0.05", "0.5"), Decimal("0.03"))

    def test_invalid(self):
        for price, rate in [("-1", "0.9"), ("1", "1.1"), ("1", "-0.1")]:
            with self.subTest(price=price, rate=rate), self.assertRaises(ValueError):
                discounted_total(price, rate)
