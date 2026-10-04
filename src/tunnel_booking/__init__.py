"""综合管廊空间预约与作业许可系统。"""
from .contracts import OccupancyEnvelope, TunnelSection, Zone
from .geometry import Interval, Rect
from .service import GalleryService, ServiceError

__all__ = [
    "GalleryService",
    "ServiceError",
    "Interval",
    "OccupancyEnvelope",
    "Rect",
    "TunnelSection",
    "Zone",
]
