"""空间/时间/工序冲突计算。

冲突判定按“舱室相同 + 里程重叠 + 时间重叠”三重门控：
- 横截面矩形相交            -> SPATIAL_OVERLAP（净空被占用）
- 异专业矩形净距不足        -> SEPARATION_VIOLATION（隔离距离）
- 时间重叠但空间错开        -> 无冲突
- 不同舱室                  -> 无冲突（即便里程/时间相同）
- 跨区段包络                -> 逐包络两两比较，自动覆盖
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional

from .contracts import Proposal, TimeWindow, WorkEnvelope
from .topology import GalleryTopology, required_separation

TIME_GATED = True


@dataclass(frozen=True)
class Conflict:
    code: str
    category: str  # SPACE / TIME / DEPENDENCY / TOPOLOGY
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "details": self.details,
        }


def _chainage_overlap(e1: WorkEnvelope, e2: WorkEnvelope) -> Optional[tuple]:
    s = max(e1.chainage.start_m, e2.chainage.start_m)
    e = min(e1.chainage.end_m, e2.chainage.end_m)
    if s < e:
        return (s, e)
    return None


def _time_overlap(w1: TimeWindow, w2: TimeWindow) -> Optional[tuple]:
    s = max(w1.start, w2.start)
    e = min(w1.end, w2.end)
    if s < e:
        return (s, e)
    return None


def envelope_pair_conflicts(
    e1: WorkEnvelope,
    w1: TimeWindow,
    request1: str,
    e2: WorkEnvelope,
    w2: TimeWindow,
    request2: str,
    separation_matrix: Mapping,
) -> List[Conflict]:
    """两个包络之间的空间类冲突（已含舱室/里程/时间门控）。"""
    if e1.compartment_id != e2.compartment_id:
        return []
    chain = _chainage_overlap(e1, e2)
    if chain is None:
        return []
    tover = _time_overlap(w1, w2)
    if tover is None:
        return []

    ctx = {
        "request_ids": [request1, request2],
        "envelope_ids": [e1.envelope_id, e2.envelope_id],
        "compartment_id": e1.compartment_id,
        "segments": [e1.segment_id, e2.segment_id],
        "chainage_overlap_m": [chain[0], chain[1]],
        "time_overlap": [tover[0].isoformat(), tover[1].isoformat()],
    }
    conflicts: List[Conflict] = []
    if e1.footprint.intersects(e2.footprint):
        conflicts.append(
            Conflict(
                "SPATIAL_OVERLAP",
                "SPACE",
                (
                    f"申请 {request1} 的包络 {e1.envelope_id} 与申请 {request2} 的包络 "
                    f"{e2.envelope_id} 在舱室 {e1.compartment_id} 里程 {chain[0]}~{chain[1]}m "
                    f"横截面占位重叠，且作业时间 {tover[0]}~{tover[1]} 重叠，净空冲突"
                ),
                dict(ctx),
            )
        )
        return conflicts

    if e1.utility != e2.utility:
        required = required_separation(e1.utility, e2.utility, separation_matrix)
        if required > 0:
            actual = e1.footprint.gap_to(e2.footprint)
            if actual < required - 1e-9:
                details = dict(ctx)
                details.update(
                    {
                        "required_separation_m": required,
                        "actual_gap_m": round(actual, 4),
                        "utilities": [e1.utility.value, e2.utility.value],
                    }
                )
                conflicts.append(
                    Conflict(
                        "SEPARATION_VIOLATION",
                        "SPACE",
                        (
                            f"申请 {request1}（{e1.utility.label}）与申请 {request2}"
                            f"（{e2.utility.label}）在舱室 {e1.compartment_id} 里程 "
                            f"{chain[0]}~{chain[1]}m 的实际净距 {actual:.2f}m 小于规范要求的 "
                            f"{required:.2f}m，隔离距离不足，且作业时间重叠"
                        ),
                        details,
                    )
                )
    return conflicts


def detect_pair_conflicts(
    proposal: Proposal,
    other: Proposal,
    separation_matrix: Mapping,
) -> List[Conflict]:
    """两个方案之间所有包络对的冲突。"""
    out: List[Conflict] = []
    for e1 in proposal.envelopes:
        for e2 in other.envelopes:
            out.extend(
                envelope_pair_conflicts(
                    e1, proposal.window, proposal.request_id,
                    e2, other.window, other.request_id,
                    separation_matrix,
                )
            )
    return out


def detect_topology_conflicts(
    proposal: Proposal, topology: GalleryTopology
) -> List[Conflict]:
    """方案相对管廊拓扑的净空类违规（超舱、越界、压设施、占通道）。"""
    out: List[Conflict] = []
    for env in proposal.envelopes:
        for code in topology.validate_envelope(env):
            if code.startswith("UNKNOWN_COMPARTMENT:"):
                cid = code.split(":", 1)[1]
                out.append(Conflict(code, "TOPOLOGY", f"方案引用了不存在的舱室 {cid}",
                                    {"compartment_id": cid, "envelope_id": env.envelope_id}))
            elif code.startswith("UNKNOWN_SEGMENT:"):
                sid = code.split(":", 1)[1]
                out.append(Conflict(code, "TOPOLOGY", f"方案引用了不存在的区段 {sid}",
                                    {"segment_id": sid, "envelope_id": env.envelope_id}))
            elif code == "ENVELOPE_OUT_OF_COMPARTMENT":
                out.append(Conflict(code, "TOPOLOGY",
                                    f"包络 {env.envelope_id} 的横截面占位超出舱室 "
                                    f"{env.compartment_id} 净空边界",
                                    {"envelope_id": env.envelope_id}))
            elif code == "ENVELOPE_OUT_OF_SEGMENT":
                out.append(Conflict(code, "TOPOLOGY",
                                    f"包络 {env.envelope_id} 的里程范围超出区段 "
                                    f"{env.segment_id} 的定义范围",
                                    {"envelope_id": env.envelope_id, "segment_id": env.segment_id}))
            elif code == "SEGMENT_COMPARTMENT_MISMATCH":
                out.append(Conflict(code, "TOPOLOGY",
                                    f"包络 {env.envelope_id} 的舱室与其区段 "
                                    f"{env.segment_id} 所属舱室不一致",
                                    {"envelope_id": env.envelope_id}))
            elif code == "FIXTURE_COLLISION":
                out.append(Conflict(code, "TOPOLOGY",
                                    f"包络 {env.envelope_id} 与舱室内永久设施占位冲突",
                                    {"envelope_id": env.envelope_id}))
            elif code == "SAFETY_PASSAGE_BLOCKED":
                out.append(Conflict(code, "TOPOLOGY",
                                    f"包络 {env.envelope_id} 占用了必须保持畅通的安全检修通道",
                                    {"envelope_id": env.envelope_id}))
    return out


def detect_dependency_conflicts(
    proposal: Proposal, known: Mapping[str, Proposal]
) -> List[Conflict]:
    """工序依赖：前置不存在 / 前置工序尚未完工即开始（前后工序冲突）。"""
    out: List[Conflict] = []
    for dep_id in proposal.depends_on:
        dep = known.get(dep_id)
        if dep is None:
            out.append(Conflict(
                "DEPENDENCY_NOT_FOUND", "DEPENDENCY",
                f"申请 {proposal.request_id} 声明的前置工序 {dep_id} 不存在",
                {"request_id": proposal.request_id, "missing_dependency": dep_id},
            ))
            continue
        if dep.window.end > proposal.window.start:
            out.append(Conflict(
                "DEPENDENCY_TIME_ORDER", "DEPENDENCY",
                (f"申请 {proposal.request_id} 计划于 {proposal.window.start} 开工，"
                 f"但其前置工序 {dep_id} 要到 {dep.window.end} 才完工，前后工序时间冲突"),
                {
                    "request_id": proposal.request_id,
                    "dependency": dep_id,
                    "predecessor_end": dep.window.end.isoformat(),
                    "successor_start": proposal.window.start.isoformat(),
                },
            ))
    return out


def detect_dependency_cycles(known: Mapping[str, Proposal]) -> Dict[str, List[Conflict]]:
    """在全部方案的依赖图上检测环，环上每个申请各得一条冲突。"""
    result: Dict[str, List[Conflict]] = {rid: [] for rid in known}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {rid: WHITE for rid in known}
    stack: List[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in known[node].depends_on:
            if dep not in known:
                continue
            if color[dep] == GRAY:
                idx = stack.index(dep)
                cycle = stack[idx:] + [dep]
                for member in cycle[:-1]:
                    result[member].append(Conflict(
                        "DEPENDENCY_CYCLE", "DEPENDENCY",
                        f"工序依赖存在环：{' -> '.join(cycle)}，无法安排先后顺序",
                        {"cycle": cycle},
                    ))
            elif color[dep] == WHITE:
                visit(dep)
        stack.pop()
        color[node] = BLACK

    for rid in known:
        if color[rid] == WHITE:
            visit(rid)
    return result


def evaluate_proposal(
    proposal: Proposal,
    holders: Mapping[str, Proposal],
    topology: GalleryTopology,
    known: Optional[Mapping[str, Proposal]] = None,
    separation_matrix: Optional[Mapping] = None,
) -> List[Conflict]:
    """综合评估单个方案：拓扑净空 + 与所有占位方案的空间时间冲突 + 依赖。"""
    from .topology import DEFAULT_SEPARATION_M

    matrix = separation_matrix or DEFAULT_SEPARATION_M
    known = known if known is not None else holders
    conflicts: List[Conflict] = []
    conflicts.extend(detect_topology_conflicts(proposal, topology))
    for rid, other in holders.items():
        if rid == proposal.request_id:
            continue
        conflicts.extend(detect_pair_conflicts(proposal, other, matrix))
    conflicts.extend(detect_dependency_conflicts(proposal, known))
    return conflicts
