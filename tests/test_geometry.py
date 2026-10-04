import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tunnel_booking.geometry import Interval, Rect, format_chainage


class IntervalTests(unittest.TestCase):
    def test_overlap_and_intersection(self):
        a = Interval(0, 10)
        self.assertTrue(a.overlaps(Interval(5, 15)))
        self.assertFalse(a.overlaps(Interval(10, 20)))  # 半开区间端点相接不算重叠
        self.assertEqual(a.intersection(Interval(5, 15)), Interval(5, 10))
        self.assertIsNone(a.intersection(Interval(10, 20)))

    def test_invalid_interval(self):
        with self.assertRaises(ValueError):
            Interval(5, 5)


class RectTests(unittest.TestCase):
    def test_overlap_and_distance(self):
        a = Rect(0, 0, 1, 1)
        self.assertTrue(a.overlaps(Rect(0.5, 0.5, 1, 1)))
        self.assertFalse(a.overlaps(Rect(2, 0, 1, 1)))
        self.assertAlmostEqual(a.distance_to(Rect(4, 5, 1, 1)), 5.0)  # 3-4-5
        self.assertEqual(a.distance_to(Rect(0.5, 0.5, 1, 1)), 0.0)

    def test_fits_within(self):
        self.assertTrue(Rect(0.5, 0.5, 1, 1).fits_within(3, 2.5))
        self.assertFalse(Rect(2.5, 0, 1, 1).fits_within(3, 2.5))

    def test_chainage_format(self):
        self.assertEqual(format_chainage(150), "K0+150.0")
        self.assertEqual(format_chainage(1234.5), "K1+234.5")


if __name__ == "__main__":
    unittest.main()
