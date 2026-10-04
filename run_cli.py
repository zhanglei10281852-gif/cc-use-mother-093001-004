import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))
from tunnel_booking.contracts import OccupancyEnvelope, TunnelSection
from tunnel_booking.demo import run_demo

# 基础契约冒烟
section = TunnelSection("T-1", 0, 120)
space = OccupancyEnvelope("REQ-1", section.section_id, 0.8, 0.5)
print(json.dumps({"section": section.section_id, "request": space.request_id,
                  "area": space.width_m * space.height_m}, ensure_ascii=False))

# 端到端演示：跨区段预约 → 冲突解释 → 抢修暂停 → 恢复顺序 → 验收释放 → 审计验证
for step in run_demo():
    print(json.dumps(step, ensure_ascii=False))
