"""舱室/区段拓扑与专业间隔离距离配置。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional

from .contracts import (
    ChainageInterval,
    Compartment,
    Rect,
    Segment,
    UtilityKind,
)

#: 不同专业管线之间的最小水平/空间净距（米），对称矩阵。
DEFAULT_SEPARATION_M: Dict[frozenset, float] = {
    frozenset({UtilityKind.POWER, UtilityKind.COMM}): 0.30,
    frozenset({UtilityKind.POWER, UtilityKind.WATER}): 0.30,
    frozenset({UtilityKind.POWER, UtilityKind.FIRE}): 0.50,
    frozenset({UtilityKind.COMM, UtilityKind.WATER}): 0.15,
    frozenset({UtilityKind.COMM, UtilityKind.FIRE}): 0.30,
    frozenset({UtilityKind.WATER, UtilityKind.FIRE}): 0.50,
}


def required_separation(a: UtilityKind, b: UtilityKind, matrix: Mapping) -> float:
    if a == b:
        return 0.0
    return float(matrix.get(frozenset({a, b}), 0.0))


@dataclass
class GalleryTopology:
    """整条综合管廊的空间拓扑：舱室横截面 + 连续区段定义。"""

    compartments: Dict[str, Compartment] = field(default_factory=dict)
    segments: Dict[str, Segment] = field(default_factory=dict)

    def add_compartment(self, comp: Compartment) -> None:
        if comp.compartment_id in self.compartments:
            raise ValueError(f"舱室 {comp.compartment_id} 已存在")
        self.compartments[comp.compartment_id] = comp

    def add_segment(self, segment: Segment) -> None:
        if segment.segment_id in self.segments:
            raise ValueError(f"区段 {segment.segment_id} 已存在")
        if segment.compartment_id not in self.compartments:
            raise ValueError(f"区段 {segment.segment_id} 引用了不存在的舱室 {segment.compartment_id}")
        self.segments[segment.segment_id] = segment

    def validate_footprint(
        self, compartment_id: str, chainage: ChainageInterval, footprint: Rect
    ) -> Iterable[str]:
        """返回占位违背拓扑/净空约束的原因代码列表（空列表表示通过）。"""
        codes = []
        comp = self.compartments.get(compartment_id)
        if comp is None:
            return [f"UNKNOWN_COMPARTMENT:{compartment_id}"]
        if not comp.contains(footprint):
            codes.append("ENVELOPE_OUT_OF_COMPARTMENT")
        for fx in comp.fixtures:
            if footprint.intersects(fx):
                codes.append("FIXTURE_COLLISION")
                break
        if comp.safety_passage is not None and footprint.intersects(comp.safety_passage):
            codes.append("SAFETY_PASSAGE_BLOCKED")
        return codes

    def validate_envelope(self, env) -> Iterable[str]:
        codes = list(self.validate_footprint(env.compartment_id, env.chainage, env.footprint))
        seg = self.segments.get(env.segment_id)
        if seg is None:
            codes.append(f"UNKNOWN_SEGMENT:{env.segment_id}")
        else:
            if seg.compartment_id != env.compartment_id:
                codes.append("SEGMENT_COMPARTMENT_MISMATCH")
            if (
                env.chainage.start_m < seg.chainage.start_m
                or env.chainage.end_m > seg.chainage.end_m
            ):
                codes.append("ENVELOPE_OUT_OF_SEGMENT")
        return codes

    def to_dict(self) -> dict:
        def rect(r: Rect) -> dict:
            return {
                "x_m": r.x_m,
                "y_m": r.y_m,
                "width_m": r.width_m,
                "height_m": r.height_m,
            }

        return {
            "compartments": [
                {
                    "compartment_id": c.compartment_id,
                    "name": c.name,
                    "width_m": c.width_m,
                    "height_m": c.height_m,
                    "fixtures": [rect(f) for f in c.fixtures],
                    "safety_passage": rect(c.safety_passage) if c.safety_passage else None,
                }
                for c in self.compartments.values()
            ],
            "segments": [
                {
                    "segment_id": s.segment_id,
                    "compartment_id": s.compartment_id,
                    "start_m": s.chainage.start_m,
                    "end_m": s.chainage.end_m,
                }
                for s in self.segments.values()
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GalleryTopology":
        topo = cls()

        def rect(d: Optional[dict]) -> Optional[Rect]:
            if not d:
                return None
            return Rect(d["x_m"], d["y_m"], d["width_m"], d["height_m"])

        for c in data.get("compartments", []):
            topo.add_compartment(
                Compartment(
                    c["compartment_id"],
                    c["name"],
                    c["width_m"],
                    c["height_m"],
                    tuple(rect(f) for f in c.get("fixtures", [])),
                    rect(c.get("safety_passage")),
                )
            )
        for s in data.get("segments", []):
            topo.add_segment(
                Segment(
                    s["segment_id"],
                    s["compartment_id"],
                    ChainageInterval(s["start_m"], s["end_m"]),
                )
            )
        return topo
