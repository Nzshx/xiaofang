from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import ezdxf


CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

SPECIAL_FLOORS = {
    "屋顶": "ROOF",
    "屋面": "ROOF",
    "设备层": "EQUIPMENT",
    "机房层": "EQUIPMENT",
}


@dataclass
class Sheet:
    drawing: str
    discipline: str
    kind: str
    floor: str
    title: str
    title_x: float
    title_y: float
    center_x: float
    center_y: float
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    sheet_id: str = ""
    building_ids: list[str] = field(default_factory=list)
    floors: list[str] = field(default_factory=list)
    role: str = "floor_plan"
    detection_method: str = ""
    confidence: float = 0.0

    def contains(self, x: float, y: float) -> bool:
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    @property
    def floor_ids(self) -> set[str]:
        return set(self.floors or ([self.floor] if self.floor else []))

    @property
    def building_set(self) -> set[str]:
        return set(self.building_ids)


@dataclass
class Detection:
    detection_id: str
    source_drawing: str
    discipline: str
    sheet_kind: str
    floor: str
    category: str
    x: float
    y: float
    entity_type: str
    handle: str
    layer: str
    block_name: str
    semantic: str
    confidence: float
    evidence: str
    sheet_id: str = ""
    building_ids: list[str] = field(default_factory=list)
    floors: list[str] = field(default_factory=list)
    review_status: str = "auto_accepted"


@dataclass
class Registration:
    source_drawing: str
    source_kind: str
    floor: str
    target_drawing: str
    method: str
    accepted: bool
    scale_x: float
    scale_y: float
    translate_x: float
    translate_y: float
    title_translate_x: float
    title_translate_y: float
    axis_matches: int
    common_matches: int
    inlier_matches: int
    median_residual: float
    p95_residual: float
    reason: str
    rotation_deg: float = 0.0
    source_sheet_id: str = ""
    target_sheet_id: str = ""
    source_building_ids: list[str] = field(default_factory=list)
    target_building_ids: list[str] = field(default_factory=list)
    source_floors: list[str] = field(default_factory=list)
    target_floors: list[str] = field(default_factory=list)
    fit_tolerance: float = 0.0
    confidence: float = 0.0
    acceptance_level: str = "rejected"
    fallback_support: int = 0
    fallback_evidence: str = ""


def clean_text(value: object) -> str:
    text = str(value or "")
    text = text.replace("\\P", " ").replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\\[A-Za-z][^;]*;", "", text)
    return re.sub(r"\s+", "", text).strip()


def entity_text(entity: Any) -> str:
    kind = entity.dxftype()
    if kind == "TEXT":
        return clean_text(entity.dxf.get("text", ""))
    if kind == "MTEXT":
        try:
            return clean_text(entity.plain_text())
        except Exception:
            return clean_text(entity.dxf.get("text", ""))
    if kind in {"ATTRIB", "ATTDEF"}:
        return clean_text(entity.dxf.get("text", ""))
    return ""


def entity_point(entity: Any) -> tuple[float, float] | None:
    kind = entity.dxftype()
    try:
        if kind in {"TEXT", "MTEXT", "INSERT", "ATTRIB", "ATTDEF", "POINT"}:
            point = entity.dxf.insert if kind != "POINT" else entity.dxf.location
            return float(point.x), float(point.y)
        if kind == "LINE":
            a, b = entity.dxf.start, entity.dxf.end
            return (float(a.x + b.x) / 2.0, float(a.y + b.y) / 2.0)
        if kind == "CIRCLE" or kind == "ARC":
            point = entity.dxf.center
            return float(point.x), float(point.y)
        if kind == "LWPOLYLINE":
            points = list(entity.get_points("xy"))
            if points:
                return (
                    sum(float(item[0]) for item in points) / len(points),
                    sum(float(item[1]) for item in points) / len(points),
                )
    except Exception:
        return None
    return None


def chinese_number(token: str) -> int | None:
    """Parse common Chinese or Arabic floor numbers without project limits."""
    value = clean_text(token)
    if not value:
        return None
    if value.isdigit():
        return int(value)
    value = value.replace("两", "二")
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if all(char in digits for char in value):
        number = 0
        for char in value:
            number = number * 10 + digits[char]
        return number
    return CHINESE_NUMBERS.get(value)


def _floor_id(token: str, basement: bool = False) -> str | None:
    number = chinese_number(token)
    if not number or number < 1:
        return None
    return f"B{number}" if basement else f"F{number}"


def parse_floor_scope(text: str) -> list[str]:
    """Return every logical floor represented by a physical plan title."""
    value = clean_text(text).upper()
    if not value:
        return []
    result: list[str] = []
    for marker, floor_id in SPECIAL_FLOORS.items():
        if marker in value and floor_id not in result:
            result.append(floor_id)

    token_re = r"[一二两三四五六七八九十零\d]+"
    range_re = re.compile(
        rf"(?P<basement>地下|负|B)?(?P<start>{token_re})\s*(?:层|F)?\s*(?:至|到|~|～|-)\s*"
        rf"(?P<end>{token_re})\s*(?:层|F)",
        re.I,
    )
    occupied: list[tuple[int, int]] = []
    for match in range_re.finditer(value):
        start = chinese_number(match.group("start"))
        end = chinese_number(match.group("end"))
        basement = bool(match.group("basement"))
        if start and end and 0 < start <= end <= 99:
            for number in range(start, end + 1):
                floor_id = f"B{number}" if basement else f"F{number}"
                if floor_id not in result:
                    result.append(floor_id)
            occupied.append(match.span())

    def in_range(position: int) -> bool:
        return any(start <= position < end for start, end in occupied)

    single_re = re.compile(rf"(?P<basement>地下|负|B|-)?\s*(?P<number>{token_re})\s*(?:层|F)", re.I)
    for match in single_re.finditer(value):
        if in_range(match.start()):
            continue
        floor_id = _floor_id(match.group("number"), bool(match.group("basement")))
        if floor_id and floor_id not in result:
            result.append(floor_id)
    return sorted(result, key=floor_sort_key)


def parse_building_ids(text: str) -> list[str]:
    value = clean_text(text).upper()
    result: list[str] = []
    # Handles "1、2、3#楼" and "1#、2#、3#楼".
    for group in re.findall(r"((?:\d{1,3}\s*#?\s*(?:[、,，/]\s*)?)+)楼", value):
        for token in re.findall(r"\d{1,3}", group):
            normalized = str(int(token))
            if normalized not in result:
                result.append(normalized)
    for token in re.findall(r"(\d{1,3})\s*#\s*楼", value):
        normalized = str(int(token))
        if normalized not in result:
            result.append(normalized)
    return result


def normalize_floor(text: str) -> str | None:
    floors = parse_floor_scope(text)
    return floors[0] if floors else None


def floor_sort_key(floor: str) -> tuple[int, int]:
    if not floor:
        return 9, 0
    if floor.startswith("B"):
        try:
            return 0, -int(floor[1:])
        except ValueError:
            return 9, 0
    if floor.startswith("F"):
        try:
            return 1, int(floor[1:])
        except ValueError:
            return 9, 0
    if floor == "EQUIPMENT":
        return 2, 900
    if floor == "ROOF":
        return 2, 999
    return 3, 0


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(materialized[0]) if materialized else []
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        if not fieldnames:
            return
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)


def asdict_list(items: Iterable[Any]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return float(ordered[index])


def median(values: list[float]) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float(ordered[middle - 1] + ordered[middle]) / 2.0


def ensure_layer(doc: ezdxf.document.Drawing, name: str, color: int, lineweight: int = 35) -> None:
    if name not in doc.layers:
        doc.layers.add(name, color=color, lineweight=lineweight)


CATEGORY_COLORS = {
    "water": 5,
    "electrical": 1,
    "hvac": 3,
    "migrated_water": 4,
    "migrated_electrical": 2,
    "migrated_hvac": 3,
}


def add_marker(
    doc: ezdxf.document.Drawing,
    x: float,
    y: float,
    label: str,
    *,
    layer_prefix: str,
    color: int,
    radius: float = 330.0,
    text_height: float = 160.0,
    draw_label: bool = True,
) -> None:
    marker_layer = f"{layer_prefix}_框"
    text_layer = f"{layer_prefix}_文字"
    ensure_layer(doc, marker_layer, color, 50)
    ensure_layer(doc, text_layer, color, 25)
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [
            (x - radius, y - radius),
            (x + radius, y - radius),
            (x + radius, y + radius),
            (x - radius, y + radius),
        ],
        close=True,
        dxfattribs={"layer": marker_layer, "color": color, "lineweight": 50},
    )
    msp.add_circle((x, y), radius * 0.52, dxfattribs={"layer": marker_layer, "color": color})
    if draw_label:
        msp.add_line(
            (x + radius, y + radius * 0.2),
            (x + radius * 1.7, y + radius * 0.8),
            dxfattribs={"layer": marker_layer, "color": color},
        )
        msp.add_text(
            label,
            height=text_height,
            dxfattribs={"layer": text_layer, "color": color},
        ).set_placement((x + radius * 1.8, y + radius * 0.75))
