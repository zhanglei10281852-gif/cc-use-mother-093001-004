"""演示用管廊拓扑构建：小型综合舱 + 连续区段 + 安全通道与永久设施。"""
from __future__ import annotations

from .contracts import ChainageInterval, Compartment, Rect, Segment
from .topology import GalleryTopology


def build_demo_topology() -> GalleryTopology:
    """一条小型综合管廊：

    - 综合舱 C1：内净空 3.0m(宽) x 2.8m(高)；
      底部 0.6m 宽纵向检修通道必须保持畅通；顶部有一根永久结构横梁占位。
    - 连续区段：S1 里程 0~120m，S2 里程 120~240m（同舱相邻，可跨区段预约）。
    """
    topo = GalleryTopology()
    topo.add_compartment(
        Compartment(
            compartment_id="C1",
            name="综合舱（电力/通信/给水/消防共舱）",
            width_m=3.0,
            height_m=2.8,
            fixtures=(Rect(0.0, 2.55, 3.0, 0.25),),  # 顶部结构梁
            safety_passage=Rect(0.0, 0.0, 0.6, 2.0),  # 底部纵向检修通道
        )
    )
    topo.add_segment(
        Segment("S1", "C1", ChainageInterval(0.0, 120.0))
    )
    topo.add_segment(
        Segment("S2", "C1", ChainageInterval(120.0, 240.0))
    )
    return topo
