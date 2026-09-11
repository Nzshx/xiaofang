from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import ezdxf

from ..common import (
    Detection,
    Sheet,
    add_marker,
    asdict_list,
    clean_text,
    entity_point,
    entity_text,
    floor_sort_key,
    read_json,
    write_csv,
    write_json,
)


Rule = tuple[re.Pattern[str], str, float]

RULE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "inspection_object_rules.json"


def _load_rules(path: Path = RULE_CONFIG_PATH) -> tuple[list[Rule], list[Rule], list[Rule], set[str]]:
    """Load the closed inspection taxonomy from a project-independent config.

    This follows the inventory/rule-library split used by the original fire
    inspection pipeline, while keeping this multi-drawing pipeline deterministic
    and limited to the requested water/electrical/HVAC categories.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))

    def compile_group(name: str) -> list[Rule]:
        return [
            (re.compile(str(item["pattern"]), re.I), str(item["category"]), float(item["confidence"]))
            for item in payload.get(name, [])
        ]

    return compile_group("water"), compile_group("electrical"), compile_group("hvac"), {
        str(value) for value in payload.get("room_categories", [])
    }


WATER_RULES, ELECTRICAL_RULES, HVAC_RULES, ROOM_CATEGORIES = _load_rules()


def _rules_for(discipline: str) -> list[Rule]:
    return {
        "water": WATER_RULES,
        "electrical": ELECTRICAL_RULES,
        "hvac": HVAC_RULES,
    }.get(discipline, [])

WATER_PIPE_RE = re.compile(r"消防|喷淋|消火栓|给水|FIRE|HYDRANT|SPRINK", re.I)
PIPE_GEOMETRY_RE = re.compile(r"管|PIPE|PIPING|LINE", re.I)
ELECTRICAL_WIRE_RE = re.compile(
    r"消防|应急|报警|广播|电话|通讯|通信|FIRE|ALARM|EMERGENCY|BROADCAST|PHONE|COMM",
    re.I,
)
WIRE_GEOMETRY_RE = re.compile(r"线|WIRE|CABLE|布线", re.I)
EQUIPMENT_LAYER_RE = re.compile(r"EQUIP|设备|器具|符号|图例", re.I)
HVAC_SYMBOL_HINT_RE = re.compile(
    r"ACS[_-]|暖通|通风|风口|百叶|风机|风阀|防火阀|排烟|防烟|送风|补风|轴流|FAN|DAMPER|LOUVER|GRILLE",
    re.I,
)
HVAC_NON_SPATIAL_ENTITY_RE = re.compile(
    r"系统(?:原理)?图|原理图|剖面图|详图|示意图|计算(?:书|表)|图例|大样图",
    re.I,
)
# Common HVAC CAD authoring tools separate duct fittings by structural layer
# role.  ``FM_F`` is a fire/smoke damper symbol layer; the neighbouring duct
# itself is still linework and is deliberately ignored below.
HVAC_FIRE_DAMPER_LAYER_RE = re.compile(r"ACS_[A-Z]*FG_FM_F(?:$|[_-])", re.I)
HVAC_FIRE_DAMPER_SYSTEM_RE = re.compile(r"ACS_([YJSP])FG_FM_F(?:$|[_-])", re.I)
HVAC_FAN_SYMBOL_RE = re.compile(
    r"排烟(?:风)?机|加压风机|加压送风机|正压送风机|补风机|送风机|轴流风机|离心风机|"
    r"风机.{0,8}(?:排烟|加压|补风)|ZL[_-]?FJ|ZLFJ|FAN",
    re.I,
)
HVAC_OUTLET_SYMBOL_RE = re.compile(
    r"风口|百叶|GRILLE|LOUVER|H-DTDFFS|SFFH|ACS_[A-Z]*FG_FK",
    re.I,
)
HVAC_ACS_SYSTEM_GEOMETRY_RE = re.compile(r"ACS_([YJSP])FG_(?:F2|FRZX)(?:$|[_-])", re.I)
HVAC_EXACT_FAN_RULES: list[Rule] = [
    (
        re.compile(
            r"排风兼排烟风机|排烟风机|排烟机|消防排烟风机|SMOKE.?EXHAUST.?FAN|"
            r"(?:^|[|_\-])(?:SEF|PYF)(?=$|[|_\-\d])",
            re.I,
        ),
        "排烟风机", 0.98,
    ),
    (
        re.compile(
            r"加压风机|加压送风机|正压送风机|消防加压风机|消防送风风机|PRESSURI[ZS].*FAN|"
            r"(?:^|[|_\-])JYF(?=$|[|_\-\d])",
            re.I,
        ),
        "加压风机", 0.98,
    ),
    (
        re.compile(
            r"补风机|消防补风机|事故补风机|MAKE.?UP.?AIR.?FAN|"
            r"(?:^|[|_\-])MAF(?=$|[|_\-\d])",
            re.I,
        ),
        "补风机", 0.98,
    ),
]
HVAC_EXACT_OUTLET_RULES: list[Rule] = [
    (re.compile(r"多叶排烟口|排烟口|SMOKE.?OUTLET", re.I), "排烟口", 0.98),
    (re.compile(r"加压送风口|正压送风口|多叶正压送风阀", re.I), "加压送风口", 0.98),
    (re.compile(r"补风口|MAKE.?UP.?AIR.?OUTLET", re.I), "补风口", 0.98),
]
ROOM_WORD_RE = re.compile(r"房|室|间|池|水箱|控制中心", re.I)
ROOM_BOUNDARY_NOISE_RE = re.compile(
    r"TITLE|FRAME|BORDER|AXIS|GRID|DIM|ANNO|TEXT|LEGEND|TABLE|TABL|图框|图签|轴|尺寸|标注|文字|图例|表格",
    re.I,
)
NON_EQUIPMENT_CONTAINER_RE = re.compile(r"(?:^|[-_$])XREF|MODEL[_ -]?SPACE|PAPER[_ -]?SPACE", re.I)
NON_GRAPHIC_BLOCK_TYPES = {
    "TEXT", "MTEXT", "ATTDEF", "ATTRIB", "SEQEND",
    "ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT",
}


def _block_profiles(doc: ezdxf.document.Drawing, max_depth: int = 10) -> tuple[dict[str, str], dict[str, bool]]:
    """Build recursive block semantic and geometry signatures.

    CAD equipment is often wrapped by anonymous blocks several levels deep.
    The former implementation inspected only direct text.  This selective port
    from the original inventory stage follows child INSERTs with cycle and depth
    guards, but still returns the top-level INSERT so migration preserves the
    original visible entity and layer.
    """
    direct: dict[str, tuple[list[str], list[str], bool, int]] = {}
    for block in doc.blocks:
        texts: list[str] = []
        children: list[str] = []
        has_geometry = False
        for entity in block:
            kind = entity.dxftype()
            if kind in {"TEXT", "MTEXT", "ATTDEF", "ATTRIB"}:
                value = entity_text(entity)
                if value:
                    texts.append(value)
            elif kind == "INSERT":
                child = str(entity.dxf.get("name", ""))
                if child:
                    children.append(child)
            elif kind not in NON_GRAPHIC_BLOCK_TYPES:
                has_geometry = True
        direct[block.name] = (texts, children, has_geometry, len(block))

    semantic_cache: dict[str, str] = {}
    graphic_cache: dict[str, bool] = {}

    def visit(name: str, visiting: set[str], depth: int) -> tuple[str, bool]:
        if name in semantic_cache:
            return semantic_cache[name], graphic_cache[name]
        if name in visiting or depth > max_depth:
            return "", False
        texts, children, has_geometry, entity_count = direct.get(name, ([], [], False, 0))
        # An XREF/model-space wrapper or a very large block represents a whole
        # drawing region, not one piece of equipment.  Descendant words such as
        # "安全出口" must not classify the container itself as a device.
        if NON_EQUIPMENT_CONTAINER_RE.search(name) or entity_count > 80:
            recursive_graphics = has_geometry or bool(children)
            semantic_cache[name] = ""
            graphic_cache[name] = recursive_graphics
            return "", recursive_graphics
        semantic_parts = [*texts]
        recursive_graphics = has_geometry
        for child in children:
            semantic_parts.append(child)
            child_semantics, child_graphics = visit(child, {*visiting, name}, depth + 1)
            if child_semantics:
                semantic_parts.append(child_semantics)
            recursive_graphics = recursive_graphics or child_graphics
        semantic = "|".join(dict.fromkeys(part for part in semantic_parts if part))[:4000]
        semantic_cache[name] = semantic
        graphic_cache[name] = recursive_graphics
        return semantic, recursive_graphics

    for block_name in direct:
        visit(block_name, set(), 0)
    return semantic_cache, graphic_cache


def _block_semantics(doc: ezdxf.document.Drawing) -> dict[str, str]:
    return _block_profiles(doc)[0]


def _block_graphics(doc: ezdxf.document.Drawing) -> dict[str, bool]:
    return _block_profiles(doc)[1]


def _find_sheet(sheets: list[Sheet], x: float, y: float) -> Sheet | None:
    containing = [sheet for sheet in sheets if sheet.contains(x, y)]
    if not containing:
        return None
    return min(
        containing,
        key=lambda sheet: (
            (sheet.max_x - sheet.min_x) * (sheet.max_y - sheet.min_y),
            -sheet.confidence,
            abs(x - sheet.center_x) + abs(y - sheet.center_y),
        ),
    )


def _polyline_points(entity: Any) -> list[tuple[float, float]]:
    try:
        if entity.dxftype() == "LWPOLYLINE" and bool(entity.closed):
            return [(float(x), float(y)) for x, y, *_ in entity.get_points("xy")]
        if entity.dxftype() == "POLYLINE" and bool(entity.is_closed):
            return [(float(vertex.dxf.location.x), float(vertex.dxf.location.y)) for vertex in entity.vertices]
    except Exception:
        return []
    return []


def _polygon_area(points: list[tuple[float, float]]) -> float:
    return abs(sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
    )) / 2.0 if len(points) >= 3 else 0.0


def _point_in_polygon(x: float, y: float, points: list[tuple[float, float]]) -> bool:
    inside = False
    previous = points[-1]
    for current in points:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1
            if x < crossing_x:
                inside = not inside
        previous = current
    return inside


def _room_boundaries(doc: ezdxf.document.Drawing, sheets: list[Sheet]) -> dict[str, list[tuple[float, Any, list[tuple[float, float]]]]]:
    """Index plausible closed room regions per physical sheet.

    A room name is accepted as an inspection target only when this index can
    bind it to real closed CAD geometry.  This preserves the user's rule that a
    standalone label is evidence, not an object.
    """
    result: dict[str, list[tuple[float, Any, list[tuple[float, float]]]]] = defaultdict(list)
    for entity in doc.modelspace():
        points = _polyline_points(entity)
        if len(points) < 3 or ROOM_BOUNDARY_NOISE_RE.search(str(entity.dxf.get("layer", ""))):
            continue
        center_x = sum(point[0] for point in points) / len(points)
        center_y = sum(point[1] for point in points) / len(points)
        sheet = _find_sheet(sheets, center_x, center_y)
        if not sheet:
            continue
        area = _polygon_area(points)
        sheet_area = max((sheet.max_x - sheet.min_x) * (sheet.max_y - sheet.min_y), 1.0)
        if sheet_area * 1e-7 <= area <= sheet_area * 0.25:
            result[sheet.sheet_id].append((area, entity, points))
    for values in result.values():
        values.sort(key=lambda item: item[0])
    return result


def _containing_room_boundary(
    boundaries: dict[str, list[tuple[float, Any, list[tuple[float, float]]]]],
    sheet: Sheet,
    x: float,
    y: float,
) -> Any | None:
    return next(
        (entity for _area, entity, points in boundaries.get(sheet.sheet_id, []) if _point_in_polygon(x, y, points)),
        None,
    )


def _match_rule(value: str, rules: list[Rule]) -> tuple[str, float, str] | None:
    for pattern, category, confidence in rules:
        match = pattern.search(value)
        if match:
            return category, confidence, f"语义关键词:{match.group(0)}"
    return None


def _semantic_value(entity: Any, block_semantics: dict[str, str]) -> str:
    kind = entity.dxftype()
    layer = str(entity.dxf.get("layer", ""))
    if kind == "INSERT":
        block_name = str(entity.dxf.get("name", ""))
        attributes = "|".join(entity_text(item) for item in entity.attribs if entity_text(item))
        return clean_text(f"{layer}|{block_name}|{block_semantics.get(block_name, '')}|{attributes}")
    if kind in {"TEXT", "MTEXT"}:
        return clean_text(f"{layer}|{entity_text(entity)}")
    return clean_text(layer)


def _nearby_text_index(doc: ezdxf.document.Drawing, sheets: list[Sheet]) -> tuple[float, dict[tuple[int, int], list[tuple[float, float, str]]]]:
    diagonals = [math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y) for sheet in sheets]
    cell_size = max((sorted(diagonals)[len(diagonals) // 2] * 0.015 if diagonals else 1000.0), 1.0)
    grid: dict[tuple[int, int], list[tuple[float, float, str]]] = defaultdict(list)
    for entity in doc.modelspace():
        if entity.dxftype() not in {"TEXT", "MTEXT"}:
            continue
        text = entity_text(entity)
        point = entity_point(entity)
        if not text or not point or len(text) > 40:
            continue
        key = math.floor(point[0] / cell_size), math.floor(point[1] / cell_size)
        grid[key].append((point[0], point[1], text))
    return cell_size, grid


def _nearby_text(x: float, y: float, cell_size: float, grid: dict[tuple[int, int], list[tuple[float, float, str]]]) -> str:
    cx, cy = math.floor(x / cell_size), math.floor(y / cell_size)
    candidates: list[tuple[float, str]] = []
    radius2 = cell_size * cell_size
    for gx in range(cx - 1, cx + 2):
        for gy in range(cy - 1, cy + 2):
            for tx, ty, text in grid.get((gx, gy), []):
                distance2 = (tx - x) ** 2 + (ty - y) ** 2
                if distance2 <= radius2:
                    candidates.append((distance2, text))
    return "|".join(text for _, text in sorted(candidates)[:4])


def _point_segment_distance(
    x: float, y: float, x1: float, y1: float, x2: float, y2: float,
) -> float:
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 <= 1e-12:
        return math.hypot(x - x1, y - y1)
    ratio = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length2))
    return math.hypot(x - (x1 + ratio * dx), y - (y1 + ratio * dy))


def _hvac_context_index(
    doc: ezdxf.document.Drawing,
    sheets: list[Sheet],
) -> tuple[dict[str, list[tuple[str, float, float, float, float]]], dict[str, float]]:
    """Index smoke-exhaust/pressurisation duct geometry as local context.

    YFG/JFG/SFG/PFG are structural CAD layer roles (smoke exhaust, pressure
    supply, supply and exhaust), not project names.  Chinese/English semantic
    system-layer names are supported as well.  They are used only to decide what a nearby
    *symbol block* means; these duct segments are never returned as inspection
    objects or copied to the building drawing.
    """
    result: dict[str, list[tuple[str, float, float, float, float]]] = defaultdict(list)
    limits = {
        sheet.sheet_id: max(math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y) * 0.008, 1.0)
        for sheet in sheets
    }
    for entity in doc.modelspace():
        layer = clean_text(str(entity.dxf.get("layer", "")))
        matched = HVAC_ACS_SYSTEM_GEOMETRY_RE.search(layer)
        if matched:
            system_code = matched.group(1).upper()
        elif re.search(r"补风|MAKE.?UP.?AIR", layer, re.I):
            system_code = "B"
        elif re.search(r"加压|正压|PRESSURI[ZS]", layer, re.I):
            system_code = "J"
        elif re.search(r"排烟|SMOKE.?EXHAUST", layer, re.I):
            system_code = "Y"
        elif re.search(r"送风|SUPPLY.?AIR", layer, re.I):
            system_code = "S"
        elif re.search(r"排风|EXHAUST.?AIR", layer, re.I):
            system_code = "P"
        else:
            continue
        segments: list[tuple[float, float, float, float]] = []
        try:
            if entity.dxftype() == "LINE":
                start, end = entity.dxf.start, entity.dxf.end
                segments.append((float(start.x), float(start.y), float(end.x), float(end.y)))
            elif entity.dxftype() == "LWPOLYLINE":
                points = [(float(px), float(py)) for px, py in entity.get_points("xy")]
                segments.extend((x1, y1, x2, y2) for (x1, y1), (x2, y2) in zip(points, points[1:]))
        except Exception:
            continue
        for x1, y1, x2, y2 in segments:
            sheet = _find_sheet(sheets, (x1 + x2) / 2.0, (y1 + y2) / 2.0)
            if sheet:
                result[sheet.sheet_id].append((system_code, x1, y1, x2, y2))
    return result, limits


def _nearest_hvac_context(
    sheet: Sheet,
    x: float,
    y: float,
    context: dict[str, list[tuple[str, float, float, float, float]]],
    limits: dict[str, float],
) -> tuple[str, float] | None:
    values = context.get(sheet.sheet_id, [])
    if not values:
        return None
    distance, code = min(
        (_point_segment_distance(x, y, x1, y1, x2, y2), code)
        for code, x1, y1, x2, y2 in values
    )
    return (code, distance) if distance <= limits.get(sheet.sheet_id, 0.0) else None


def _classify_hvac_structural_symbol(
    entity: Any,
    sheet: Sheet,
    semantic: str,
    context: dict[str, list[tuple[str, float, float, float, float]]],
    context_limits: dict[str, float],
    block_graphics: dict[str, bool] | None = None,
    nearby_text: str = "",
) -> tuple[str, float, str] | None:
    """Classify a real HVAC INSERT into one physical-object category.

    Evidence is evaluated in descending reliability: the entity's own
    semantics, an explicit nearby equipment label, and finally the structural
    system attached/adjacent to the symbol.  Generic abbreviations such as
    ``SF`` and ``PDF`` are intentionally not hard-coded because their meaning
    changes between projects.
    """
    if entity.dxftype() != "INSERT" or HVAC_NON_SPATIAL_ENTITY_RE.search(semantic):
        return None
    layer = clean_text(str(entity.dxf.get("layer", "")))
    block_name = str(entity.dxf.get("name", ""))
    symbol_identity = clean_text(f"{layer}|{block_name}")
    if block_graphics is not None and not block_graphics.get(block_name, False):
        return None
    damper_system = HVAC_FIRE_DAMPER_SYSTEM_RE.search(layer)
    if damper_system:
        code = damper_system.group(1).upper()
        if code == "Y":
            return "排烟防火阀", 0.98, f"排烟系统防火阀符号图层:{layer}"
        return "防火阀", 0.97, f"{code}FG系统防火阀符号图层:{layer}"
    if HVAC_FIRE_DAMPER_LAYER_RE.search(layer):
        direct_damper = _match_rule(semantic, HVAC_RULES)
        if direct_damper and direct_damper[0] in {"防火阀", "排烟防火阀"}:
            return direct_damper
        return None

    is_fan = bool(HVAC_FAN_SYMBOL_RE.search(semantic))
    is_outlet = bool(HVAC_OUTLET_SYMBOL_RE.search(symbol_identity))
    if is_fan:
        direct_fan = _match_rule(semantic, HVAC_EXACT_FAN_RULES)
        if direct_fan:
            return direct_fan[0], direct_fan[1], f"风机图元自身语义:{direct_fan[2]}"
    if is_outlet:
        direct_outlet = _match_rule(semantic, HVAC_EXACT_OUTLET_RULES)
        if direct_outlet:
            return direct_outlet[0], direct_outlet[1], f"风口图元自身语义:{direct_outlet[2]}"
    point = entity_point(entity)
    if not point:
        return None
    local = _nearest_hvac_context(sheet, point[0], point[1], context, context_limits)
    if not local:
        return None
    code, distance = local
    normalized = distance / max(context_limits.get(sheet.sheet_id, 1.0), 1.0)
    if is_fan:
        nearby_fan = _match_rule(clean_text(nearby_text), HVAC_EXACT_FAN_RULES)
        if nearby_fan:
            expected_code = {"排烟风机": "Y", "加压风机": "J", "补风机": "B"}.get(nearby_fan[0])
            if expected_code is None or expected_code == code:
                return (
                    nearby_fan[0], 0.96,
                    f"附近明确设备标签:{nearby_fan[2]};{code}FG系统一致;归一距离={normalized:.3f}",
                )
        if code == "Y":
            return "排烟风机", 0.94, f"真实风机块+YFG排烟系统邻接:归一距离={normalized:.3f}"
        if code == "J":
            return "加压风机", 0.94, f"真实风机块+JFG加压系统邻接:归一距离={normalized:.3f}"
        if code == "B":
            return "补风机", 0.94, f"真实风机块+补风系统邻接:归一距离={normalized:.3f}"
        # SFG/PFG alone only means supply/exhaust ventilation; it does not
        # prove that the fire-inspection object is a make-up-air fan.
        return None
    # For outlets, require the actual layer/block identity to describe a
    # symbol.  A large note block may contain wording such as "预留通风口" and
    # must not be promoted merely because that text occurs inside the block.
    if is_outlet:
        nearby_outlet = _match_rule(clean_text(nearby_text), HVAC_EXACT_OUTLET_RULES)
        if nearby_outlet:
            expected_code = {"排烟口": "Y", "加压送风口": "J", "补风口": "B"}.get(nearby_outlet[0])
            if expected_code is None or expected_code == code:
                return (
                    nearby_outlet[0], 0.96,
                    f"附近明确风口标签:{nearby_outlet[2]};{code}FG系统一致;归一距离={normalized:.3f}",
                )
        if code == "Y":
            return "排烟口", 0.93, f"真实风口块+YFG排烟系统邻接:归一距离={normalized:.3f}"
        if code == "J":
            return "加压送风口", 0.93, f"真实风口块+JFG加压系统邻接:归一距离={normalized:.3f}"
        if code == "B":
            return "补风口", 0.93, f"真实风口块+补风系统邻接:归一距离={normalized:.3f}"
    return None


def _legend_block_map(
    doc: ezdxf.document.Drawing,
    sheets: list[Sheet],
    block_semantics: dict[str, str],
    block_graphics: dict[str, bool],
    discipline: str,
    cell_size: float,
    text_grid: dict[tuple[int, int], list[tuple[float, float, str]]],
) -> dict[str, tuple[str, float, str]]:
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    evidence: dict[tuple[str, str], str] = {}
    rules = _rules_for(discipline)
    for entity in doc.modelspace():
        if entity.dxftype() != "INSERT":
            continue
        point = entity_point(entity)
        if not point or not _find_sheet(sheets, *point):
            continue
        block_name = str(entity.dxf.get("name", ""))
        if not block_graphics.get(block_name, False):
            continue
        direct = _match_rule(_semantic_value(entity, block_semantics), rules)
        if direct:
            votes[block_name][direct[0]] += 3
            evidence[(block_name, direct[0])] = direct[2]
            continue
        nearby = _nearby_text(point[0], point[1], cell_size, text_grid)
        inferred = _match_rule(clean_text(nearby), rules)
        if inferred:
            votes[block_name][inferred[0]] += 1
            evidence[(block_name, inferred[0])] = f"同块图例邻近文字:{nearby[:80]}"

    result: dict[str, tuple[str, float, str]] = {}
    for block_name, categories in votes.items():
        category, count = categories.most_common(1)[0]
        total = sum(categories.values())
        if count < 2 or count / max(total, 1) < 0.60:
            continue
        confidence = 0.91 if count >= 3 else 0.84
        result[block_name] = category, confidence, evidence[(block_name, category)]
    return result


def _classify_entity(
    entity: Any,
    discipline: str,
    sheet: Sheet,
    semantic: str,
    legend_map: dict[str, tuple[str, float, str]],
    block_graphics: dict[str, bool] | None = None,
) -> tuple[str, float, str] | None:
    kind = entity.dxftype()
    rules = _rules_for(discipline)
    layer = clean_text(str(entity.dxf.get("layer", "")))

    # Continuous linework is infrastructure, not a point inspection target.
    # The current workflow plans routes only to concrete devices/components;
    # therefore water pipes, electrical wiring and HVAC ducts are excluded at
    # recognition time instead of becoming migration failures later.
    if kind in {"LINE", "LWPOLYLINE", "ARC"}:
        return None

    # Standalone annotation text is evidence only. It may describe a symbol
    # above/below it and must never become a migrated inspection object itself.
    if kind in {"TEXT", "MTEXT"}:
        return None
    if kind != "INSERT":
        return None

    block_name = str(entity.dxf.get("name", ""))
    if block_graphics is not None and not block_graphics.get(block_name, False):
        return None
    direct = _match_rule(semantic, rules)
    if direct:
        return direct
    if block_name in legend_map:
        category, confidence, evidence = legend_map[block_name]
        return category, confidence, f"同块图例归纳:{evidence}"
    return None


def recognize_drawing(source: Path, discipline: str, sheets: list[Sheet]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    doc = ezdxf.readfile(source)
    block_semantics, block_graphics = _block_profiles(doc)
    room_boundaries = _room_boundaries(doc, sheets)
    cell_size, text_grid = _nearby_text_index(doc, sheets)
    hvac_context, hvac_context_limits = (
        _hvac_context_index(doc, sheets) if discipline == "hvac" else ({}, {})
    )
    legend_map = _legend_block_map(
        doc, sheets, block_semantics, block_graphics,
        discipline, cell_size, text_grid,
    )
    accepted: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind not in {"INSERT", "TEXT", "MTEXT", "LINE", "LWPOLYLINE", "ARC"}:
            continue
        point = entity_point(entity)
        if not point:
            continue
        sheet = _find_sheet(sheets, *point)
        if not sheet:
            continue
        semantic = _semantic_value(entity, block_semantics)
        rules = _rules_for(discipline)
        # A system/section/detail wrapper can geometrically overlap a nearby
        # plan frame.  Its title is a hard structural exclusion, even when the
        # wrapper contains words such as "排烟口".
        if discipline == "hvac" and HVAC_NON_SPATIAL_ENTITY_RE.search(semantic):
            continue
        if kind in {"TEXT", "MTEXT"}:
            # Keep short matched text in the review trail, but never promote it
            # to Detection or migrate the text as an inspection object.
            text_value = entity_text(entity)
            text_rule = _match_rule(semantic, rules)
            if text_rule and len(text_value) <= 32:
                category, confidence, evidence = text_rule
                boundary = (
                    _containing_room_boundary(room_boundaries, sheet, point[0], point[1])
                    if (
                        category in ROOM_CATEGORIES
                        and ROOM_WORD_RE.search(text_value)
                        and not ROOM_BOUNDARY_NOISE_RE.search(str(entity.dxf.get("layer", "")))
                    )
                    else None
                )
                if boundary is not None:
                    accepted.append({
                        "source_drawing": str(source.resolve()), "discipline": discipline,
                        "sheet_kind": sheet.kind, "floor": sheet.floor, "category": category,
                        "x": point[0], "y": point[1], "entity_type": boundary.dxftype(),
                        "handle": str(boundary.dxf.get("handle", "")),
                        "layer": str(boundary.dxf.get("layer", "")), "block_name": "",
                        "semantic": semantic[:500], "confidence": min(confidence, 0.93),
                        "evidence": f"房间文字+闭合空间边界:{text_value};{evidence}",
                        "sheet_id": sheet.sheet_id, "building_ids": list(sheet.building_ids),
                        "floors": list(sheet.floors), "review_status": "auto_accepted_room_boundary",
                    })
                    continue
                review.append({
                    "source_drawing": str(source.resolve()), "discipline": discipline,
                    "sheet_kind": sheet.kind, "floor": sheet.floor, "category": category,
                    "x": point[0], "y": point[1], "entity_type": kind,
                    "handle": str(entity.dxf.get("handle", "")),
                    "layer": str(entity.dxf.get("layer", "")), "block_name": "",
                    "semantic": semantic[:500], "confidence": confidence, "evidence": evidence,
                    "sheet_id": sheet.sheet_id, "building_ids": list(sheet.building_ids),
                    "floors": list(sheet.floors), "review_status": "text_evidence_only",
                    "rejection_reason": "独立文字仅作为附近图元的语义线索，不作为巡检对象",
                })
            continue
        nearby_hvac_text = (
            _nearby_text(point[0], point[1], cell_size, text_grid)
            if discipline == "hvac" and kind == "INSERT"
            else ""
        )
        rule = (
            _classify_hvac_structural_symbol(
                entity, sheet, semantic, hvac_context, hvac_context_limits, block_graphics,
                nearby_hvac_text,
            )
            if discipline == "hvac"
            else None
        )
        if not rule:
            rule = _classify_entity(
                entity, discipline, sheet, semantic, legend_map, block_graphics,
            )
        if not rule and discipline == "hvac" and kind == "INSERT" and HVAC_SYMBOL_HINT_RE.search(semantic):
            nearby = nearby_hvac_text
            inferred = _match_rule(clean_text(nearby), rules)
            if inferred:
                category, confidence, evidence = inferred
                rule = category, min(confidence, 0.91), f"暖通实体邻近文字:{nearby[:100]};{evidence}"
        if not rule:
            if (
                discipline == "hvac" and kind == "INSERT"
                and block_graphics.get(str(entity.dxf.get("name", "")), False)
                and HVAC_FAN_SYMBOL_RE.search(semantic)
            ):
                review.append({
                    "source_drawing": str(source.resolve()), "discipline": discipline,
                    "sheet_kind": sheet.kind, "floor": sheet.floor,
                    "category": "风机类型无法确定", "x": point[0], "y": point[1],
                    "entity_type": kind, "handle": str(entity.dxf.get("handle", "")),
                    "layer": str(entity.dxf.get("layer", "")),
                    "block_name": str(entity.dxf.get("name", "")),
                    "semantic": semantic[:500], "confidence": 0.50,
                    "evidence": f"存在真实风机图元，但无明确设备名或可靠YFG/JFG系统连接;附近文字:{nearby_hvac_text[:100]}",
                    "sheet_id": sheet.sheet_id, "building_ids": list(sheet.building_ids),
                    "floors": list(sheet.floors), "review_status": "rejected_hvac_subtype_unresolved",
                    "rejection_reason": "无法唯一判定为排烟风机、加压风机或补风机，未强制迁移",
                })
            continue
        category, confidence, evidence = rule
        # Long notes can contain equipment names but do not represent devices.
        if kind in {"TEXT", "MTEXT"} and len(entity_text(entity)) > 32:
            continue
        row = {
            "source_drawing": str(source.resolve()), "discipline": discipline,
            "sheet_kind": sheet.kind, "floor": sheet.floor, "category": category,
            "x": point[0], "y": point[1], "entity_type": kind,
            "handle": str(entity.dxf.get("handle", "")), "layer": str(entity.dxf.get("layer", "")),
            "block_name": str(entity.dxf.get("name", "")) if kind == "INSERT" else "",
            "semantic": semantic[:500], "confidence": confidence, "evidence": evidence,
            "sheet_id": sheet.sheet_id, "building_ids": list(sheet.building_ids),
            "floors": list(sheet.floors),
            "review_status": "auto_accepted" if confidence >= 0.88 else "rejected_low_confidence",
        }
        if confidence >= 0.88:
            accepted.append(row)
        else:
            review.append(row)
    return accepted, review


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = row["source_drawing"], row["handle"], row["category"]
        current = best.get(key)
        if current is None or float(row["confidence"]) > float(current["confidence"]):
            best[key] = row
    return list(best.values())


def _make_detections(rows: list[dict[str, Any]]) -> list[Detection]:
    counters: Counter[tuple[str, str]] = Counter()
    detections: list[Detection] = []
    ordered = sorted(
        rows,
        key=lambda row: (
            row["discipline"], floor_sort_key(row["floor"]), row["sheet_id"], row["category"], row["x"], row["y"],
        ),
    )
    for row in ordered:
        key = row["discipline"], row["floor"]
        counters[key] += 1
        prefix = {"water": "W", "electrical": "E", "hvac": "H"}.get(row["discipline"], "X")
        row["detection_id"] = f"{prefix}-{row['floor']}-{counters[key]:05d}"
        detections.append(Detection(**row))
    return detections


def annotate_source(
    source: Path,
    detections: list[Detection],
    sheets: list[Sheet],
    output: Path,
) -> Path:
    doc = ezdxf.readfile(source)
    sheet_map = {sheet.sheet_id: sheet for sheet in sheets}
    for item in detections:
        color = {"water": 5, "electrical": 1, "hvac": 3}.get(item.discipline, 7)
        prefix = {
            "water": "通用融合_水_原图识别",
            "electrical": "通用融合_电_原图识别",
            "hvac": "通用融合_暖通_原图识别",
        }.get(item.discipline, "通用融合_原图识别")
        draw_label = item.category not in {"布线", "管网"}
        sheet = sheet_map.get(item.sheet_id)
        diagonal = (
            math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
            if sheet else 1.0
        )
        text_height = max(diagonal * 0.0005, 1e-6)
        radius_factor = 2.1 if item.category in {"喷头", "布线", "管网"} else 2.75
        add_marker(
            doc, item.x, item.y, f"{item.detection_id} {item.category}",
            layer_prefix=prefix, color=color,
            radius=text_height * radius_factor,
            text_height=text_height, draw_label=draw_label,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.saveas(output)
        return output
    except PermissionError:
        # CAD commonly locks the annotation currently open for review. Preserve
        # that file and write the refreshed result beside it.
        for index in range(1, 1000):
            alternative = output.with_name(f"{output.stem}_更新{index}{output.suffix}")
            try:
                doc.saveas(alternative)
                return alternative
            except PermissionError:
                continue
        raise PermissionError(f"标注DXF及999个更新文件均被占用: {output}")


def run_stage(prepared_payload: dict[str, object], sheets_path: Path, stage_dir: Path) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    sheets = [Sheet(**item) for item in read_json(sheets_path)]
    scope_path = Path(__file__).resolve().parents[1] / "configs" / "discipline_object_scope.json"
    scope = read_json(scope_path)
    professional = [
        item for item in prepared_payload["drawings"]
        if item["discipline"] in {"water", "electrical", "hvac"}
    ]
    all_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    for drawing in professional:
        source = Path(str(drawing["dxf"])).resolve()
        source_sheets = [sheet for sheet in sheets if Path(sheet.drawing) == source]
        discipline = str(drawing["discipline"])
        accepted, review = recognize_drawing(source, discipline, source_sheets)
        allowed = set((scope.get(discipline) or {}).get("allowed_classes") or [])
        scoped: list[dict[str, Any]] = []
        for row in accepted:
            if str(row.get("category") or "") in allowed:
                scoped.append(row)
            else:
                review.append({
                    **row,
                    "review_reason": "类别不属于来源图纸专业的巡检对象同名库",
                    "discipline_scope_rejected": True,
                })
        all_rows.extend(scoped)
        review_rows.extend(review)
    detections = _make_detections(_deduplicate(all_rows))
    rows = asdict_list(detections)
    write_json(stage_dir / "recognized_objects.json", rows)
    write_csv(stage_dir / "recognized_objects.csv", rows)
    for row in review_rows:
        row["decision"] = "rejected"
    write_json(stage_dir / "recognition_rejections.json", review_rows)
    write_csv(stage_dir / "recognition_rejections.csv", review_rows)

    annotation_files: list[str] = []
    for drawing in professional:
        source = Path(str(drawing["dxf"])).resolve()
        selected = [item for item in detections if Path(item.source_drawing) == source]
        output = stage_dir / "source_annotations" / str(drawing["discipline"]) / f"原图识别标注_{source.stem}.dxf"
        saved_output = annotate_source(source, selected, sheets, output)
        annotation_files.append(str(saved_output.resolve()))

    by_floor_category: Counter[tuple[str, str, str]] = Counter(
        (item.discipline, item.floor, item.category) for item in detections
    )
    count_rows = [
        {"discipline": key[0], "floor": key[1], "category": key[2], "count": value}
        for key, value in sorted(by_floor_category.items(), key=lambda pair: (pair[0][0], floor_sort_key(pair[0][1]), pair[0][2]))
    ]
    write_csv(stage_dir / "recognition_counts_by_floor.csv", count_rows)
    payload = {
        "stage": "03_recognize", "recognized_total": len(detections),
        "recognized_water": sum(item.discipline == "water" for item in detections),
        "recognized_electrical": sum(item.discipline == "electrical" for item in detections),
        "recognized_hvac": sum(item.discipline == "hvac" for item in detections),
        "recognition_rejected_count": len(review_rows),
        "counts_by_floor_category": count_rows,
        "source_annotation_files": annotation_files,
        "recognized_objects_json": str((stage_dir / "recognized_objects.json").resolve()),
        "discipline_scope_policy": str(scope_path.resolve()),
        "discipline_scope_rejected_count": sum(
            bool(row.get("discipline_scope_rejected")) for row in review_rows
        ),
    }
    write_json(stage_dir / "stage_summary.json", payload)
    return payload
