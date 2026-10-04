"""冲突计算引擎单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tunnel_booking.contracts import (
    ChainageInterval,
    Compartment,
    Proposal,
    Rect,
    Segment,
    TimeWindow,
    UtilityKind,
    WorkEnvelope,
)
from tunnel_booking.conflicts import (
    detect_dependency_conflicts,
    detect_dependency_cycles,
    detect_pair_conflicts,
    detect_topology_conflicts,
    evaluate_proposal,
)
from tunnel_booking.topology import DEFAULT_SEPARATION_M, GalleryTopology


def make_envelope(eid, utility, seg="S1", chain=(0.0, 100.0), rect=(1.0, 1.0, 0.4, 0.4),
                  compartment="C1"):
    return WorkEnvelope(
        envelope_id=eid, compartment_id=compartment, segment_id=seg,
        chainage=ChainageInterval(*chain), footprint=Rect(*rect), utility=utility,
    )


def make_proposal(rid, envelopes, start="2026-10-05T08:00", end="2026-10-05T18:00",
                  depends_on=()):
    return Proposal(
        request_id=rid, contractor=f"C-{rid}",
        window=TimeWindow.of(start, end), envelopes=tuple(envelopes),
        depends_on=tuple(depends_on),
    )


def make_topology():
    topo = GalleryTopology()
    topo.add_compartment(Compartment("C1", "综合舱", 3.0, 2.8,
                                     fixtures=(Rect(0.0, 2.5, 3.0, 0.3),),
                                     safety_passage=Rect(0.0, 0.0, 0.6, 2.0)))
    topo.add_compartment(Compartment("C2", "电力舱", 2.0, 2.5))
    topo.add_segment(Segment("S1", "C1", ChainageInterval(0.0, 120.0)))
    topo.add_segment(Segment("S2", "C1", ChainageInterval(120.0, 240.0)))
    return topo


class GeometryTests(unittest.TestCase):
    def test_rect_intersects_and_gap(self):
        a = Rect(0, 0, 1, 1)
        self.assertTrue(a.intersects(Rect(0.9, 0.9, 1, 1)))
        self.assertFalse(a.intersects(Rect(1.0, 0.0, 1, 1)))
        self.assertAlmostEqual(a.gap_to(Rect(2, 0, 1, 1)), 1.0)
        self.assertAlmostEqual(Rect(0, 0, 1, 1).gap_to(Rect(2, 2, 1, 1)),
                               (2.0) ** 0.5)

    def test_interval_overlap_is_half_open(self):
        a = ChainageInterval(0, 10)
        self.assertFalse(a.overlaps(ChainageInterval(10, 20)))
        self.assertTrue(a.overlaps(ChainageInterval(5, 15)))


class PairConflictTests(unittest.TestCase):
    def test_spatial_overlap_same_time_same_space(self):
        p1 = make_proposal("A", [make_envelope("E1", UtilityKind.POWER)])
        p2 = make_proposal("B", [make_envelope("E2", UtilityKind.COMM)])
        codes = {c.code for c in detect_pair_conflicts(p1, p2, DEFAULT_SEPARATION_M)}
        self.assertIn("SPATIAL_OVERLAP", codes)

    def test_separation_violation_when_gap_too_small(self):
        # 电力占位 x=1.0 宽 0.4 -> 右边 1.4；通信 x=1.7 宽 0.3 -> 净距 0.3，恰好满足。
        ok = make_envelope("E2", UtilityKind.COMM, rect=(1.7, 1.0, 0.3, 0.3))
        p1 = make_proposal("A", [make_envelope("E1", UtilityKind.POWER)])
        p2 = make_proposal("B", [ok])
        self.assertEqual(detect_pair_conflicts(p1, p2, DEFAULT_SEPARATION_M), [])
        # 净距缩到 0.2 < 0.3 -> SEPARATION_VIOLATION。
        close = make_envelope("E3", UtilityKind.COMM, rect=(1.6, 1.0, 0.3, 0.3))
        p3 = make_proposal("B", [close])
        codes = {c.code for c in detect_pair_conflicts(p1, p3, DEFAULT_SEPARATION_M)}
        self.assertIn("SEPARATION_VIOLATION", codes)

    def test_different_compartment_no_conflict(self):
        p1 = make_proposal("A", [make_envelope("E1", UtilityKind.POWER, compartment="C1")])
        p2 = make_proposal("B", [make_envelope("E2", UtilityKind.COMM, compartment="C2")])
        self.assertEqual(detect_pair_conflicts(p1, p2, DEFAULT_SEPARATION_M), [])

    def test_time_disjoint_no_conflict(self):
        p1 = make_proposal("A", [make_envelope("E1", UtilityKind.POWER)],
                           "2026-10-05T08:00", "2026-10-05T12:00")
        p2 = make_proposal("B", [make_envelope("E2", UtilityKind.POWER)],
                           "2026-10-05T12:00", "2026-10-05T18:00")
        self.assertEqual(detect_pair_conflicts(p1, p2, DEFAULT_SEPARATION_M), [])

    def test_chainage_disjoint_no_conflict(self):
        p1 = make_proposal("A", [make_envelope("E1", UtilityKind.POWER, chain=(0, 60))])
        p2 = make_proposal("B", [make_envelope("E2", UtilityKind.POWER, chain=(60, 120))])
        self.assertEqual(detect_pair_conflicts(p1, p2, DEFAULT_SEPARATION_M), [])

    def test_cross_segment_envelope_pair_conflict_detected(self):
        # 跨区段包络（0~130 横跨 S1/S2）与落在 S2 的占位在 120~130 处冲突。
        cross = make_envelope("E1", UtilityKind.POWER, seg="S1", chain=(0.0, 130.0))
        other = make_envelope("E2", UtilityKind.POWER, seg="S2", chain=(120.0, 180.0))
        p1 = make_proposal("A", [cross])
        p2 = make_proposal("B", [other])
        codes = {c.code for c in detect_pair_conflicts(p1, p2, DEFAULT_SEPARATION_M)}
        self.assertIn("SPATIAL_OVERLAP", codes)

    def test_conflict_message_is_explainable(self):
        p1 = make_proposal("A", [make_envelope("E1", UtilityKind.POWER)])
        p2 = make_proposal("B", [make_envelope("E2", UtilityKind.COMM)])
        conflicts = detect_pair_conflicts(p1, p2, DEFAULT_SEPARATION_M)
        self.assertTrue(conflicts)
        c = conflicts[0].to_dict()
        self.assertIn("compartment_id", c["details"])
        self.assertIn("chainage_overlap_m", c["details"])
        self.assertIn("time_overlap", c["details"])
        self.assertIn("舱室", c["message"])


class TopologyConflictTests(unittest.TestCase):
    def setUp(self):
        self.topo = make_topology()

    def test_footprint_out_of_compartment_and_safety_passage(self):
        env = make_envelope("E1", UtilityKind.WATER, rect=(0.0, 0.0, 0.8, 1.0))
        codes = {c.code for c in detect_topology_conflicts(make_proposal("A", [env]), self.topo)}
        self.assertIn("SAFETY_PASSAGE_BLOCKED", codes)

    def test_footprint_hits_fixture(self):
        env = make_envelope("E1", UtilityKind.WATER, rect=(0.8, 2.4, 0.5, 0.4))
        codes = {c.code for c in detect_topology_conflicts(make_proposal("A", [env]), self.topo)}
        self.assertIn("FIXTURE_COLLISION", codes)

    def test_envelope_out_of_segment(self):
        env = make_envelope("E1", UtilityKind.POWER, seg="S1", chain=(100.0, 130.0))
        codes = {c.code for c in detect_topology_conflicts(make_proposal("A", [env]), self.topo)}
        self.assertIn("ENVELOPE_OUT_OF_SEGMENT", codes)

    def test_unknown_compartment_and_segment(self):
        env = WorkEnvelope("E1", "ZZ", "NOPE", ChainageInterval(0, 10),
                           Rect(0, 0, 1, 1), UtilityKind.POWER)
        codes = {c.code for c in detect_topology_conflicts(make_proposal("A", [env]), self.topo)}
        self.assertTrue(any(c.startswith("UNKNOWN_COMPARTMENT") for c in codes))
        self.assertTrue(any(c.startswith("UNKNOWN_SEGMENT") for c in codes))


class DependencyConflictTests(unittest.TestCase):
    def test_missing_dependency(self):
        p = make_proposal("A", [make_envelope("E1", UtilityKind.POWER)], depends_on=("GHOST",))
        codes = {c.code for c in detect_dependency_conflicts(p, {})}
        self.assertIn("DEPENDENCY_NOT_FOUND", codes)

    def test_predecessor_not_finished(self):
        pre = make_proposal("P", [make_envelope("E0", UtilityKind.POWER)],
                            "2026-10-05T08:00", "2026-10-05T18:00")
        succ = make_proposal("S", [make_envelope("E1", UtilityKind.POWER)],
                             "2026-10-05T12:00", "2026-10-05T20:00",
                             depends_on=("P",))
        codes = {c.code for c in detect_dependency_conflicts(succ, {"P": pre})}
        self.assertIn("DEPENDENCY_TIME_ORDER", codes)

    def test_predecessor_finished_ok(self):
        pre = make_proposal("P", [make_envelope("E0", UtilityKind.POWER)],
                            "2026-10-05T08:00", "2026-10-05T12:00")
        succ = make_proposal("S", [make_envelope("E1", UtilityKind.COMM)],
                             "2026-10-05T12:00", "2026-10-05T18:00",
                             depends_on=("P",))
        # 时间不重叠 + 依赖满足 -> 无任何冲突。
        conflicts = evaluate_proposal(succ, {"P": pre}, make_topology(),
                                      known={"P": pre})
        self.assertEqual(conflicts, [])

    def test_dependency_cycle(self):
        a = make_proposal("A", [make_envelope("EA", UtilityKind.POWER)], depends_on=("B",))
        b = make_proposal("B", [make_envelope("EB", UtilityKind.COMM)], depends_on=("A",))
        result = detect_dependency_cycles({"A": a, "B": b})
        self.assertTrue(result["A"])
        self.assertEqual(result["A"][0].code, "DEPENDENCY_CYCLE")


if __name__ == "__main__":
    unittest.main()
