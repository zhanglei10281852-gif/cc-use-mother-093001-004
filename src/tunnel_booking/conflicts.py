"""空间与时间冲突检测引擎。

输入一组占位需求、连续区段表和当前已占用视图，输出可解释的冲突列表：
- NO_SECTION    里程无区段覆盖（区段不连续）
- OUT_OF_BOUNDS 占位超出断面限界
- CLEARANCE     与既有设施净空不足
- OVERLAP       占位包络相交（空间+时间同时重叠）
- SEPARATION    专业间隔离距离不足
- PROCESS_ORDER 前后工序倒置
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .contracts import OccupancyEnvelope, TunnelSection
from .geometry import Interval, format_chainage
from .models import OccupancyRequest, fmt_dt
from .rules import (
    process_label,
    process_must_precede,
    required_separation_m,
    utility_label,
)


@dataclass(frozen=True)
class PlacedEnvelope:
    """占位需求在某区段内的切片，附带时间窗与归属信息。"""

    envelope: OccupancyEnvelope
    time_start: datetime
    time_end: datetime
    plan_id: str = ""
    version: int = 0
    permit_id: str | None = None

    @property
    def section_id(self) -> str:
        return self.envelope.section_id

    def chainage_interval(self, section: TunnelSection) -> Interval:
        s, e = self.envelope.chainage(section)
        return Interval(s, e)


@dataclass(frozen=True)
class Conflict:
    kind: str
    message: str
    blocking: bool = True
    section_id: str | None = None
    chainage: tuple | None = None
    time_range: tuple | None = None
    with_request_id: str | None = None
    with_permit_id: str | None = None
    with_plan_id: str | None = None
    required_m: float | None = None
    actual_m: float | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "blocking": self.blocking,
            "section_id": self.section_id,
            "chainage": list(self.chainage) if self.chainage else None,
            "time_range": list(self.time_range) if self.time_range else None,
            "with_request_id": self.with_request_id,
            "with_permit_id": self.with_permit_id,
            "with_plan_id": self.with_plan_id,
            "required_m": self.required_m,
            "actual_m": self.actual_m,
        }


def _time_overlap(a: PlacedEnvelope, b: PlacedEnvelope) -> tuple | None:
    """时间窗重叠区间（datetime 对），无重叠返回 None。"""
    iv = Interval(a.time_start.timestamp(), a.time_end.timestamp()).intersection(
        Interval(b.time_start.timestamp(), b.time_end.timestamp())
    )
    if iv is None:
        return None
    return (
        datetime.fromtimestamp(iv.start, tz=a.time_start.tzinfo),
        datetime.fromtimestamp(iv.end, tz=a.time_end.tzinfo),
    )


def _fmt_end(dt: datetime) -> str:
    """占位结束时间的可读形式；远未来哨兵表示验收前持续占用。"""
    if dt.year >= 9000:
        return "撤场验收前持续占用"
    return fmt_dt(dt)


def slice_request(
    request: OccupancyRequest,
    sections: list,
    plan_id: str = "",
    version: int = 0,
    permit_id: str | None = None,
) -> list:
    """把一条（可能跨区段的）占位需求切分为各区段内的包络切片。"""
    placed = []
    for sec in sections:
        inter = Interval(sec.start_m, sec.end_m).intersection(
            Interval(request.chainage_start_m, request.chainage_end_m)
        )
        if inter is None:
            continue
        env = OccupancyEnvelope(
            request_id=request.request_id,
            section_id=sec.section_id,
            width_m=request.rect.width_m,
            height_m=request.rect.height_m,
            x_m=request.rect.x_m,
            y_m=request.rect.y_m,
            chainage_start_m=inter.start,
            chainage_end_m=inter.end,
            utility=request.utility,
            process=request.process,
        )
        placed.append(
            PlacedEnvelope(
                envelope=env,
                time_start=request.time_start,
                time_end=request.time_end,
                plan_id=plan_id,
                version=version,
                permit_id=permit_id,
            )
        )
    return placed


def coverage_gaps(request: OccupancyRequest, sections: list) -> list:
    """需求里程范围内未被任何区段覆盖的区间（区段不连续）。"""
    gaps = []
    cursor = request.chainage_start_m
    for sec in sorted(sections, key=lambda s: s.start_m):
        if sec.end_m <= cursor or sec.start_m >= request.chainage_end_m:
            continue
        if sec.start_m > cursor:
            gaps.append((cursor, min(sec.start_m, request.chainage_end_m)))
        cursor = max(cursor, sec.end_m)
        if cursor >= request.chainage_end_m:
            break
    if cursor < request.chainage_end_m:
        gaps.append((cursor, request.chainage_end_m))
    return gaps


def check_static(placed: PlacedEnvelope, section: TunnelSection) -> list:
    """单包络对区段本身的检查：限界 + 对既有设施的净空。"""
    out = []
    rect = placed.envelope.rect
    s, e = placed.envelope.chainage(section)
    where = f"区段 {section.section_id} 里程 {format_chainage(s)}~{format_chainage(e)}"
    if not rect.fits_within(section.width_m, section.height_m):
        out.append(
            Conflict(
                kind="OUT_OF_BOUNDS",
                message=(
                    f"{where}：占位包络超出断面限界（占位右界 {rect.right:.2f}m/顶界 "
                    f"{rect.top:.2f}m，断面净宽 {section.width_m:.2f}m/净高 {section.height_m:.2f}m）"
                ),
                section_id=section.section_id,
                chainage=(s, e),
                with_request_id=placed.envelope.request_id,
                with_permit_id=placed.permit_id,
                with_plan_id=placed.plan_id or None,
            )
        )
    for zone in section.zones:
        if not zone.fixed or zone.utility == placed.envelope.utility:
            continue
        dist = rect.distance_to(zone.rect)
        if dist < zone.required_clearance_m:
            out.append(
                Conflict(
                    kind="CLEARANCE",
                    message=(
                        f"{where}：与既有设施[{zone.name}]净空 {dist:.2f}m，"
                        f"小于要求 {zone.required_clearance_m:.2f}m"
                    ),
                    section_id=section.section_id,
                    chainage=(s, e),
                    with_request_id=placed.envelope.request_id,
                    with_permit_id=placed.permit_id,
                    with_plan_id=placed.plan_id or None,
                    required_m=zone.required_clearance_m,
                    actual_m=round(dist, 4),
                )
            )
    return out


def check_pair(a: PlacedEnvelope, b: PlacedEnvelope, section: TunnelSection) -> list:
    """同一区段内两条包络切片之间的冲突检查。a 为候选，b 为对照。"""
    out = []
    iv_a = a.chainage_interval(section)
    iv_b = b.chainage_interval(section)
    inter = iv_a.intersection(iv_b)
    if inter is None:
        return out
    where = f"区段 {section.section_id} 里程 {format_chainage(inter.start)}~{format_chainage(inter.end)}"
    t_ov = _time_overlap(a, b)
    rect_a, rect_b = a.envelope.rect, b.envelope.rect
    dist = rect_a.distance_to(rect_b)
    req_sep = required_separation_m(
        a.envelope.utility, b.envelope.utility, section.min_separation_m
    )
    same_workface = rect_a.overlaps(rect_b) or dist < req_sep
    b_desc = f"许可 {b.permit_id}" if b.permit_id else f"方案 {b.plan_id}"

    if rect_a.overlaps(rect_b) and t_ov:
        out.append(
            Conflict(
                kind="OVERLAP",
                message=(
                    f"{where}：与{b_desc}的占位包络相交"
                    f"（时段 {fmt_dt(t_ov[0])}~{_fmt_end(t_ov[1])} 重叠）"
                ),
                section_id=section.section_id,
                chainage=(inter.start, inter.end),
                time_range=(fmt_dt(t_ov[0]), fmt_dt(t_ov[1])),
                with_request_id=b.envelope.request_id,
                with_permit_id=b.permit_id,
                with_plan_id=b.plan_id or None,
                actual_m=0.0,
            )
        )
    elif t_ov and dist < req_sep:
        out.append(
            Conflict(
                kind="SEPARATION",
                message=(
                    f"{where}：与{b_desc}的占位净距 {dist:.2f}m，小于"
                    f"{utility_label(a.envelope.utility)}/{utility_label(b.envelope.utility)}"
                    f"最小隔离 {req_sep:.2f}m（时段 {fmt_dt(t_ov[0])}~{_fmt_end(t_ov[1])} 重叠）"
                ),
                section_id=section.section_id,
                chainage=(inter.start, inter.end),
                time_range=(fmt_dt(t_ov[0]), fmt_dt(t_ov[1])),
                with_request_id=b.envelope.request_id,
                with_permit_id=b.permit_id,
                with_plan_id=b.plan_id or None,
                required_m=req_sep,
                actual_m=round(dist, 4),
            )
        )

    if same_workface:
        # 前后工序：先序工序占位未结束，后续工序不得开始
        if process_must_precede(b.envelope.process, a.envelope.process) and (
            a.time_start < b.time_end
        ):
            out.append(
                Conflict(
                    kind="PROCESS_ORDER",
                    message=(
                        f"{where}：工序倒置，'{process_label(b.envelope.process)}'"
                        f"（{b_desc}，至 {_fmt_end(b.time_end)}）未结束，"
                        f"'{process_label(a.envelope.process)}'即于 {fmt_dt(a.time_start)} 进场"
                    ),
                    section_id=section.section_id,
                    chainage=(inter.start, inter.end),
                    time_range=(fmt_dt(a.time_start), fmt_dt(b.time_end)),
                    with_request_id=b.envelope.request_id,
                    with_permit_id=b.permit_id,
                    with_plan_id=b.plan_id or None,
                )
            )
        if process_must_precede(a.envelope.process, b.envelope.process) and (
            b.time_start < a.time_end
        ):
            out.append(
                Conflict(
                    kind="PROCESS_ORDER",
                    message=(
                        f"{where}：工序倒置，'{process_label(a.envelope.process)}'"
                        f"（本方案，至 {_fmt_end(a.time_end)}）未结束，"
                        f"'{process_label(b.envelope.process)}'（{b_desc}）"
                        f"于 {fmt_dt(b.time_start)} 进场"
                    ),
                    section_id=section.section_id,
                    chainage=(inter.start, inter.end),
                    time_range=(fmt_dt(b.time_start), fmt_dt(a.time_end)),
                    with_request_id=b.envelope.request_id,
                    with_permit_id=b.permit_id,
                    with_plan_id=b.plan_id or None,
                )
            )
    return out


def evaluate_requests(
    requests: list,
    sections: list,
    existing: list,
    plan_id: str = "",
    version: int = 0,
    self_blocking: bool = True,
) -> list:
    """对一组占位需求做完整冲突评估。

    existing 为当前占用视图（PlacedEnvelope 列表，permit_id 非空表示已批准占用，
    否则为待审方案，仅作非阻断提示）。返回 Conflict 列表，阻断项在前。
    """
    section_by_id = {s.section_id: s for s in sections}
    conflicts = []

    own_placed = []
    for req in requests:
        for g_s, g_e in coverage_gaps(req, sections):
            conflicts.append(
                Conflict(
                    kind="NO_SECTION",
                    message=(
                        f"里程 {format_chainage(g_s)}~{format_chainage(g_e)} 无舱段覆盖，"
                        f"区段不连续，无法布置占位"
                    ),
                    chainage=(g_s, g_e),
                    with_request_id=req.request_id,
                    with_plan_id=plan_id or None,
                )
            )
        placed = slice_request(req, sections, plan_id=plan_id, version=version)
        own_placed.extend(placed)
        for p in placed:
            conflicts.extend(check_static(p, section_by_id[p.section_id]))

    # 方案内部两两检查
    for i in range(len(own_placed)):
        for j in range(i + 1, len(own_placed)):
            a, b = own_placed[i], own_placed[j]
            if a.section_id != b.section_id:
                continue
            for c in check_pair(a, b, section_by_id[a.section_id]):
                conflicts.append(c if self_blocking else _as_warning(c))

    # 与现有占用/待审方案两两检查
    for a in own_placed:
        for b in existing:
            if a.section_id != b.section_id:
                continue
            for c in check_pair(a, b, section_by_id[a.section_id]):
                conflicts.append(c if b.permit_id else _as_warning(c))

    conflicts.sort(key=lambda c: (not c.blocking, c.kind))
    return conflicts


def _as_warning(conflict: Conflict) -> Conflict:
    return Conflict(
        kind=conflict.kind,
        message=conflict.message + "（对方尚未批准，属潜在冲突提示）",
        blocking=False,
        section_id=conflict.section_id,
        chainage=conflict.chainage,
        time_range=conflict.time_range,
        with_request_id=conflict.with_request_id,
        with_permit_id=conflict.with_permit_id,
        with_plan_id=conflict.with_plan_id,
        required_m=conflict.required_m,
        actual_m=conflict.actual_m,
    )
