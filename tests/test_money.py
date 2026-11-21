"""整数分摊与按件拆分：余分字典序、零权重、守恒与非负。"""
import unittest

import _helper  # noqa: F401  引导 sys.path
from app.money import allocate_by_weight, per_unit_split


class MoneyTests(unittest.TestCase):
    def test_sum_invariant(self):
        for amount in (0, 1, 7, 100, 3000, 99999):
            weights = {"a": 3, "b": 5, "c": 2}
            got = allocate_by_weight(amount, weights)
            self.assertEqual(sum(got.values()), amount)
            self.assertTrue(all(v >= 0 for v in got.values()))

    def test_remainder_lexicographic_on_tie(self):
        # 等权 10 分给 3 键：3,3,4 -> 余分 1 分归字典序最前的 a
        got = allocate_by_weight(10, {"c": 1, "a": 1, "b": 1})
        self.assertEqual(got, {"a": 4, "b": 3, "c": 3})

    def test_largest_remainder_then_lexicographic(self):
        # 100 按 1:1:1 -> 34,33,33，余分归 a
        got = allocate_by_weight(100, {"a": 1, "b": 1, "c": 1})
        self.assertEqual(got, {"a": 34, "b": 33, "c": 33})

    def test_zero_weights_and_zero_amount(self):
        self.assertEqual(allocate_by_weight(0, {"a": 0, "b": 0}), {"a": 0, "b": 0})
        with self.assertRaises(ValueError):
            allocate_by_weight(5, {"a": 0, "b": 0})

    def test_negative_amount_rejected(self):
        with self.assertRaises(ValueError):
            allocate_by_weight(-1, {"a": 1})

    def test_per_unit_split_sum_and_order(self):
        parts = per_unit_split(10, 3, "x-l0")
        self.assertEqual(parts, [4, 3, 3])
        self.assertEqual(sum(parts), 10)
        parts2 = per_unit_split(900, 2, "x-l0")
        self.assertEqual(parts2, [450, 450])

    def test_per_unit_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            per_unit_split(10, 0, "x")
        with self.assertRaises(ValueError):
            per_unit_split(-1, 2, "x")


if __name__ == "__main__":
    unittest.main()
