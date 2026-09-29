import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from tunnel_booking.contracts import OccupancyEnvelope, TunnelSection


class TunnelContractTests(unittest.TestCase):
    def test_section_has_positive_length(self):
        self.assertEqual(TunnelSection("S", 2, 5).end_m, 5)

    def test_invalid_envelope_is_rejected(self):
        with self.assertRaises(ValueError):
            OccupancyEnvelope("R", "S", -1, 1)


if __name__ == "__main__": unittest.main()
