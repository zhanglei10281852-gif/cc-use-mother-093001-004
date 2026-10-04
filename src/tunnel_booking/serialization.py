"""领域对象 <-> JSON 可序列化字典。"""
from __future__ import annotations

from typing import List

from .contracts import (
    ChainageInterval,
    Proposal,
    Rect,
    TimeWindow,
    UtilityKind,
    WorkEnvelope,
)


def utility_to_dict(u: UtilityKind) -> dict:
    return {"code": u.value, "label": u.label}


def envelope_to_dict(e: WorkEnvelope) -> dict:
    return {
        "envelope_id": e.envelope_id,
        "compartment_id": e.compartment_id,
        "segment_id": e.segment_id,
        "chainage": {"start_m": e.chainage.start_m, "end_m": e.chainage.end_m},
        "footprint": {
            "x_m": e.footprint.x_m,
            "y_m": e.footprint.y_m,
            "width_m": e.footprint.width_m,
            "height_m": e.footprint.height_m,
        },
        "utility": e.utility.value,
    }


def envelope_from_dict(d: dict) -> WorkEnvelope:
    ch = d["chainage"]
    fp = d["footprint"]
    return WorkEnvelope(
        envelope_id=d["envelope_id"],
        compartment_id=d["compartment_id"],
        segment_id=d["segment_id"],
        chainage=ChainageInterval(ch["start_m"], ch["end_m"]),
        footprint=Rect(fp["x_m"], fp["y_m"], fp["width_m"], fp["height_m"]),
        utility=UtilityKind(d["utility"]),
    )


def proposal_to_dict(p: Proposal) -> dict:
    return {
        "request_id": p.request_id,
        "contractor": p.contractor,
        "window": {"start": p.window.start.isoformat(), "end": p.window.end.isoformat()},
        "envelopes": [envelope_to_dict(e) for e in p.envelopes],
        "depends_on": list(p.depends_on),
        "priority": p.priority,
    }


def proposal_from_dict(d: dict) -> Proposal:
    w = d["window"]
    return Proposal(
        request_id=d["request_id"],
        contractor=d.get("contractor", ""),
        window=TimeWindow.of(w["start"], w["end"]),
        envelopes=tuple(envelope_from_dict(e) for e in d["envelopes"]),
        depends_on=tuple(d.get("depends_on", [])),
        priority=d.get("priority"),
    )


def conflicts_to_list(conflicts) -> List[dict]:
    return [c.to_dict() for c in conflicts]
