"""测试共用的建廊与方案构造辅助。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

T0 = datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc)

OPS = {"name": "运营", "roles": ["ops_approver", "acceptor"], "emergency_level": 0}
TECH = {"name": "复核", "roles": ["tech_reviewer"], "emergency_level": 0}
CONTRACTOR = {"name": "施工方", "roles": [], "emergency_level": 0}
REPAIR_L2 = {"name": "抢修队", "roles": [], "emergency_level": 2}
REPAIR_L0 = {"name": "普通队", "roles": [], "emergency_level": 0}


def corridor_sections():
    """两段连续区段：SEC-A K0+000~K0+200，SEC-B K0+200~K0+400。"""
    return [
        {
            "section_id": "SEC-A", "start_m": 0, "end_m": 200,
            "width_m": 3.0, "height_m": 2.5, "min_separation_m": 0.3,
            "zones": [
                {"zone_id": "Z-PWR", "name": "既有电力电缆", "utility": "POWER",
                 "rect": {"x_m": 0.0, "y_m": 1.8, "width_m": 0.4, "height_m": 0.6},
                 "required_clearance_m": 0.5},
                {"zone_id": "Z-TEL", "name": "既有通信桥架", "utility": "TELECOM",
                 "rect": {"x_m": 2.6, "y_m": 1.8, "width_m": 0.4, "height_m": 0.6},
                 "required_clearance_m": 0.5},
            ],
        },
        {
            "section_id": "SEC-B", "start_m": 200, "end_m": 400,
            "width_m": 3.0, "height_m": 2.5, "min_separation_m": 0.3,
            "zones": [
                {"zone_id": "Z-WTR", "name": "既有给水管道", "utility": "WATER",
                 "rect": {"x_m": 0.0, "y_m": 0.0, "width_m": 0.5, "height_m": 0.5},
                 "required_clearance_m": 0.4},
            ],
        },
    ]


def occ(request_id, start_m, end_m, x, w, utility="POWER", process="CABLE_LAYING",
        h_from=0.0, h_to=8.0, height=1.8, y=0.0):
    return {
        "request_id": request_id,
        "chainage_start_m": start_m,
        "chainage_end_m": end_m,
        "rect": {"x_m": x, "y_m": y, "width_m": w, "height_m": height},
        "utility": utility,
        "process": process,
        "time_start": (T0 + timedelta(hours=h_from)).isoformat(),
        "time_end": (T0 + timedelta(hours=h_to)).isoformat(),
    }


def build_service(db_path=":memory:", clock=None):
    from tunnel_booking.service import GalleryService
    svc = GalleryService(db_path, clock=clock)
    for s in corridor_sections():
        svc.add_section(actor=OPS, section=s)
    return svc


def approve_only(svc, plan_id, version=1, valid_hours=24.0):
    svc.tech_review(actor=TECH, plan_id=plan_id, version=version, approve=True)
    return svc.ops_approve(actor=OPS, plan_id=plan_id, version=version,
                           valid_hours=valid_hours)["permit"]["permit_id"]


def approve_and_check_in(svc, plan_id, version=1, valid_hours=24.0):
    permit_id = approve_only(svc, plan_id, version, valid_hours)
    svc.check_in(actor=CONTRACTOR, permit_id=permit_id)
    return permit_id
