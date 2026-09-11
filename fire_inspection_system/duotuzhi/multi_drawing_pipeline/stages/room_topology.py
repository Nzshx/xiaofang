from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ezdxf import bbox as ezbbox

from ..common import Registration, Sheet, clean_text, entity_point, entity_text


ROOM_NAME_RE = re.compile(
    r"房|室|厅|廊|走道|走廊|井|厕|库|楼梯|前室|门厅|电梯|"
    r"舞台|观众|看台|平台|厨房|卫生间|办公室|化妆|休息|"
    r"控制|配电|水泵|风机|设备",
)
ROOM_NOISE_RE = re.compile(
    r"平面图|系统图|示意图|详图|大样|图例|说明|编号|标高|"
    r"设计|施工|专业|火灾报警|照明|联网线|给排水|喷淋",
)
# Columns are route obstacles, but they do not split one room into two rooms.
# Keep them out of the room-topology index; the architectural obstacle stage
# still handles columns for later route planning.
WALL_LAYER_RE = re.compile(r"WALL|CURTAIN|GLZ|墙|幕墙|隔断", re.I)
GEOMETRY_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE"}
ENDPOINT_TOLERANCE = 0.06


@dataclass(frozen=True)
class RoomAnchor:
    name: str
    point: tuple[float, float]


@dataclass
class WallIndex:
    segments: list[tuple[tuple[float, float], tuple[float, float]]]
    cell_size: float
    grid: dict[tuple[int, int], list[int]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        segments: list[tuple[tuple[float, float], tuple[float, float]]],
        cell_size: float,
    ) -> "WallIndex":
        index = cls(segments, max(cell_size, 1.0))
        for number, (start, end) in enumerate(segments):
            min_x, max_x = sorted((start[0], end[0]))
            min_y, max_y = sorted((start[1], end[1]))
            gx0, gx1 = math.floor(min_x / index.cell_size), math.floor(max_x / index.cell_size)
            gy0, gy1 = math.floor(min_y / index.cell_size), math.floor(max_y / index.cell_size)
            # Very long frame lines are not room partitions. Avoid allowing one
            # entity to fill an unbounded number of grid cells.
            if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > 4096:
                continue
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    index.grid.setdefault((gx, gy), []).append(number)
        return index

    def candidates(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> Iterable[tuple[tuple[float, float], tuple[float, float]]]:
        min_x, max_x = sorted((start[0], end[0]))
        min_y, max_y = sorted((start[1], end[1]))
        gx0, gx1 = math.floor(min_x / self.cell_size), math.floor(max_x / self.cell_size)
        gy0, gy1 = math.floor(min_y / self.cell_size), math.floor(max_y / self.cell_size)
        seen: set[int] = set()
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                for number in self.grid.get((gx, gy), []):
                    if number not in seen:
                        seen.add(number)
                        yield self.segments[number]

    def distance_within(self, point: tuple[float, float], radius: float) -> float:
        """Return nearest wall distance within *radius*, or infinity."""
        if not self.segments:
            return math.inf
        x, y = point
        best = math.inf
        for start, end in self.candidates((x - radius, y - radius), (x + radius, y + radius)):
            best = min(best, _point_segment_distance(point, start, end))
        return best


@dataclass
class SheetRoomTopology:
    sheet: Sheet
    rooms: tuple[RoomAnchor, ...]
    walls: WallIndex


def normalize_room_name(value: object) -> str:
    text = clean_text(value)
    text = re.sub(r"^[A-Z]?\d+[-－]", "", text, flags=re.I)
    text = re.sub(r"[（(]?\d+[）)]?$", "", text)
    return text.strip("-－_：:()（）")


def is_room_name(value: object) -> bool:
    text = normalize_room_name(value)
    return bool(text and len(text) <= 18 and not ROOM_NOISE_RE.search(text) and ROOM_NAME_RE.search(text))


def _walk_entities(
    entities: Iterable[Any],
    *,
    parents: tuple[str, ...] = (),
    depth: int = 0,
    max_depth: int = 2,
) -> Iterable[tuple[Any, tuple[str, ...]]]:
    for entity in entities:
        yield entity, parents
        if entity.dxftype() != "INSERT" or depth >= max_depth:
            continue
        name = clean_text(str(entity.dxf.get("name", "")))
        try:
            children = entity.virtual_entities()
        except Exception:
            continue
        yield from _walk_entities(
            children, parents=(*parents, name), depth=depth + 1, max_depth=max_depth,
        )


def _sheet_for_point(sheets: list[Sheet], point: tuple[float, float]) -> Sheet | None:
    candidates = [sheet for sheet in sheets if sheet.contains(*point)]
    if not candidates:
        return None
    return min(candidates, key=lambda sheet: (sheet.max_x - sheet.min_x) * (sheet.max_y - sheet.min_y))


def _segments(entity: Any) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    try:
        kind = entity.dxftype()
        if kind == "LINE":
            start, end = entity.dxf.start, entity.dxf.end
            return [((float(start.x), float(start.y)), (float(end.x), float(end.y)))]
        if kind == "LWPOLYLINE":
            points = [(float(x), float(y)) for x, y in entity.get_points("xy")]
            closed = bool(entity.closed)
        elif kind == "POLYLINE":
            points = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
            closed = bool(entity.is_closed)
        else:
            return []
        result = list(zip(points, points[1:]))
        if closed and len(points) > 2:
            result.append((points[-1], points[0]))
        return [segment for segment in result if math.dist(*segment) >= 20.0]
    except Exception:
        return []


def build_room_topologies(
    doc: Any,
    sheets: Iterable[Sheet],
    *,
    max_depth: int = 2,
) -> dict[str, SheetRoomTopology]:
    sheet_list = list(sheets)
    room_rows: dict[str, list[RoomAnchor]] = {sheet.sheet_id: [] for sheet in sheet_list}
    wall_rows: dict[str, list[tuple[tuple[float, float], tuple[float, float]]]] = {
        sheet.sheet_id: [] for sheet in sheet_list
    }
    seen_rooms: set[tuple[str, str, int, int]] = set()
    seen_walls: set[tuple[str, int, int, int, int]] = set()
    for entity, parents in _walk_entities(doc.modelspace(), max_depth=max_depth):
        kind = entity.dxftype()
        if kind in {"TEXT", "MTEXT", "ATTRIB"}:
            text = entity_text(entity)
            point = entity_point(entity)
            if not point or not is_room_name(text):
                continue
            sheet = _sheet_for_point(sheet_list, point)
            if not sheet:
                continue
            name = normalize_room_name(text)
            signature = (sheet.sheet_id, name, round(point[0] / 20.0), round(point[1] / 20.0))
            if signature not in seen_rooms:
                seen_rooms.add(signature)
                room_rows[sheet.sheet_id].append(RoomAnchor(name, point))
            continue
        if kind not in GEOMETRY_TYPES:
            continue
        semantic = "|".join((clean_text(str(entity.dxf.get("layer", ""))), *parents))
        if not WALL_LAYER_RE.search(semantic):
            continue
        for start, end in _segments(entity):
            midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
            sheet = _sheet_for_point(sheet_list, midpoint)
            if not sheet:
                continue
            values = (round(start[0] / 20.0), round(start[1] / 20.0), round(end[0] / 20.0), round(end[1] / 20.0))
            reverse = (values[2], values[3], values[0], values[1])
            signature = (sheet.sheet_id, *values)
            reverse_signature = (sheet.sheet_id, *reverse)
            if signature in seen_walls or reverse_signature in seen_walls:
                continue
            seen_walls.add(signature)
            wall_rows[sheet.sheet_id].append((start, end))
    result: dict[str, SheetRoomTopology] = {}
    for sheet in sheet_list:
        diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
        result[sheet.sheet_id] = SheetRoomTopology(
            sheet,
            tuple(room_rows[sheet.sheet_id]),
            WallIndex.build(wall_rows[sheet.sheet_id], max(diagonal * 0.02, 1.0)),
        )
    return result


def _intersection_parameter(
    start: tuple[float, float], end: tuple[float, float],
    wall_start: tuple[float, float], wall_end: tuple[float, float],
) -> float | None:
    px, py = start
    rx, ry = end[0] - px, end[1] - py
    qx, qy = wall_start
    sx, sy = wall_end[0] - qx, wall_end[1] - qy
    denominator = rx * sy - ry * sx
    if abs(denominator) < 1e-9:
        return None
    qpx, qpy = qx - px, qy - py
    t = (qpx * sy - qpy * sx) / denominator
    u = (qpx * ry - qpy * rx) / denominator
    return t if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None


def _point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return math.dist(point, start)
    parameter = max(0.0, min(1.0, (
        (point[0] - start[0]) * dx + (point[1] - start[1]) * dy
    ) / length_squared))
    projection = (start[0] + parameter * dx, start[1] + parameter * dy)
    return math.dist(point, projection)


def line_crosses_wall(start: tuple[float, float], end: tuple[float, float], walls: WallIndex) -> bool:
    for wall_start, wall_end in walls.candidates(start, end):
        parameter = _intersection_parameter(start, end, wall_start, wall_end)
        if parameter is not None and ENDPOINT_TOLERANCE < parameter < 1.0 - ENDPOINT_TOLERANCE:
            return True
    return False


def bbox_crosses_room_wall(bbox: Sequence[float], room: tuple[float, float], walls: WallIndex) -> bool:
    x1, y1, x2, y2 = map(float, bbox)
    checkpoints = (
        ((x1 + x2) / 2.0, (y1 + y2) / 2.0),
        (x1, y1), (x2, y1), (x2, y2), (x1, y2),
    )
    return any(line_crosses_wall(point, room, walls) for point in checkpoints)


def entity_bbox(entity: Any) -> list[float] | None:
    try:
        bounds = ezbbox.extents([entity], fast=True)
        if bounds.has_data:
            return [float(bounds.extmin.x), float(bounds.extmin.y), float(bounds.extmax.x), float(bounds.extmax.y)]
    except Exception:
        pass
    point = entity_point(entity)
    return [point[0], point[1], point[0], point[1]] if point else None


def map_bbox(values: Sequence[float], registration: Registration) -> list[float]:
    x1, y1, x2, y2 = map(float, values)
    angle = math.radians(registration.rotation_deg)
    cosine, sine = math.cos(angle), math.sin(angle)
    points = []
    for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        points.append((
            registration.scale_x * (cosine * x - sine * y) + registration.translate_x,
            registration.scale_y * (sine * x + cosine * y) + registration.translate_y,
        ))
    return [min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)]


def _wall_clearance_fallback(
    base: dict[str, Any],
    rough_target_point: tuple[float, float],
    target: SheetRoomTopology,
    registration: Registration,
    reason: str,
) -> dict[str, Any]:
    """Keep the axis result and record target-wall proximity for review.

    Missing room text or a nearby wall is not evidence that axis registration
    moved an object into another room.  This fallback therefore never moves or
    rejects the object; it only records whether the mapped point is near the
    registration-error band around an architectural wall.
    """
    target_diagonal = math.hypot(
        target.sheet.max_x - target.sheet.min_x,
        target.sheet.max_y - target.sheet.min_y,
    )
    safety_margin = max(
        float(registration.p95_residual or 0.0) * 1.25,
        target_diagonal * 0.0005,
        1.0,
    )
    if len(target.walls.segments) < 4:
        return {
            **base,
            "status": "axis_mapping_kept",
            "wall_safety_margin": safety_margin,
            "target_wall_clearance": None,
            "reason": f"{reason}；房间证据不足，保留轴网配准位置且不做房间纠偏",
        }
    clearance = target.walls.distance_within(rough_target_point, safety_margin)
    if clearance > safety_margin:
        return {
            **base,
            "status": "axis_mapping_kept",
            "wall_safety_margin": safety_margin,
            "target_wall_clearance": (
                clearance if math.isfinite(clearance) else f">{safety_margin:.3f}"
            ),
            "reason": f"{reason}；目标点距墙较远，保留轴网配准位置",
        }
    return {
        **base,
        "status": "axis_mapping_kept_near_wall",
        "wall_crossing_detected": False,
        "wall_safety_margin": safety_margin,
        "target_wall_clearance": clearance,
        "reason": f"{reason}；目标点靠墙，但这不是跨墙证据，保留轴网配准位置且不纠偏",
    }


def guard_room_mapping(
    source_point: tuple[float, float],
    rough_target_point: tuple[float, float],
    source: SheetRoomTopology | None,
    target: SheetRoomTopology | None,
    registration: Registration,
    *,
    source_bbox: Sequence[float] | None = None,
) -> dict[str, Any]:
    base = {
        "status": "unverified", "source_room_name": "", "target_room_name": "",
        "wall_crossing_detected": False, "adjust_x": 0.0, "adjust_y": 0.0,
        "wall_safety_margin": None, "target_wall_clearance": None,
    }
    if source is None or target is None:
        return {
            **base,
            "status": "axis_mapping_kept",
            "reason": "缺少源或目标分图房间拓扑，保留轴网配准位置且不做房间纠偏",
        }
    maximum_distance = max(math.hypot(
        source.sheet.max_x - source.sheet.min_x,
        source.sheet.max_y - source.sheet.min_y,
    ) * 0.03, 1.0)
    candidates = sorted(
        ((math.dist(source_point, room.point), room) for room in source.rooms
         if math.dist(source_point, room.point) <= maximum_distance),
        key=lambda pair: pair[0],
    )
    source_match = next(
        ((_distance, room) for _distance, room in candidates
         if not line_crosses_wall(source_point, room.point, source.walls)),
        None,
    )
    if source_match is None:
        return _wall_clearance_fallback(
            base, rough_target_point, target, registration,
            "源图对象附近没有可见且未隔墙的房间名称",
        )
    source_room_distance, source_room = source_match
    matches = [room for room in target.rooms if room.name == source_room.name]
    if not matches:
        return _wall_clearance_fallback(
            {**base, "source_room_name": source_room.name},
            rough_target_point, target, registration,
            "建筑目标图中没有同名房间",
        )
    target_room = min(matches, key=lambda room: math.dist(rough_target_point, room.point))
    target_room_distance = math.dist(rough_target_point, target_room.point)
    target_diagonal = math.hypot(
        target.sheet.max_x - target.sheet.min_x,
        target.sheet.max_y - target.sheet.min_y,
    )
    if target_room_distance > max(target_diagonal * 0.03, 1.0):
        return _wall_clearance_fallback(
            {**base, "source_room_name": source_room.name},
            rough_target_point, target, registration,
            "同名房间文字距设备过远，不作为房间纠偏证据",
        )
    source_box_crosses = bool(
        source_bbox and bbox_crosses_room_wall(source_bbox, source_room.point, source.walls)
    )
    rough_bbox = map_bbox(source_bbox, registration) if source_bbox else None
    center_crosses = line_crosses_wall(rough_target_point, target_room.point, target.walls)
    box_crosses = bool(
        rough_bbox and bbox_crosses_room_wall(rough_bbox, target_room.point, target.walls)
    )
    new_crossing = center_crosses or (box_crosses and not source_box_crosses)
    common = {
        **base,
        "source_room_name": source_room.name,
        "target_room_name": target_room.name,
        "source_room_distance": source_room_distance,
        "target_room_distance": target_room_distance,
        "source_bbox_crosses_wall": source_box_crosses,
        "target_bbox_crosses_wall": box_crosses,
        "center_crosses_wall": center_crosses,
        "wall_crossing_detected": new_crossing,
    }
    if not new_crossing:
        return {**common, "status": "verified_same_room", "reason": "源/目标同名房间且墙线可见关系一致"}

    angle = math.radians(registration.rotation_deg)
    cosine, sine = math.cos(angle), math.sin(angle)
    dx = source_point[0] - source_room.point[0]
    dy = source_point[1] - source_room.point[1]
    safe_point = (
        target_room.point[0] + registration.scale_x * (cosine * dx - sine * dy),
        target_room.point[1] + registration.scale_y * (sine * dx + cosine * dy),
    )
    adjust_x = safe_point[0] - rough_target_point[0]
    adjust_y = safe_point[1] - rough_target_point[1]
    safe_bbox = (
        [rough_bbox[0] + adjust_x, rough_bbox[1] + adjust_y,
         rough_bbox[2] + adjust_x, rough_bbox[3] + adjust_y]
        if rough_bbox else None
    )
    safe_center_crosses = line_crosses_wall(safe_point, target_room.point, target.walls)
    safe_box_crosses = bool(
        safe_bbox and bbox_crosses_room_wall(safe_bbox, target_room.point, target.walls)
    )
    maximum_adjustment = max(
        float(registration.p95_residual or 0.0) * 2.0,
        target_diagonal * 0.003,
        1.0,
    )
    if (
        not safe_center_crosses
        and (not safe_box_crosses or source_box_crosses)
        and math.hypot(adjust_x, adjust_y) <= maximum_adjustment
    ):
        return {
            **common, "status": "adjusted_to_same_room",
            "adjust_x": adjust_x, "adjust_y": adjust_y,
            "reason": "检测到穿墙风险，按同名房间锚点保留源房间内相对位置",
        }
    return {
        **common, "status": "axis_mapping_kept_room_conflict",
        "wall_crossing_detected": False,
        "reason": "房间文字/墙线证据冲突且不满足小幅纠偏条件，保留轴网配准位置",
    }
