import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))
from tunnel_booking.contracts import OccupancyEnvelope, TunnelSection

section = TunnelSection("T-1", 0, 120)
space = OccupancyEnvelope("REQ-1", section.section_id, 0.8, 0.5)
print(json.dumps({"section": section.section_id, "request": space.request_id, "area": space.width_m * space.height_m}, ensure_ascii=False))
