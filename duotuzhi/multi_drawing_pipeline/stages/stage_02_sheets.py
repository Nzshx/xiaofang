from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import ezdxf
from ezdxf import bbox as ezbbox

from ..common import (
    Sheet,
    asdict_list,
    clean_text,
    entity_point,
    entity_text,
    floor_sort_key,
    parse_building_ids,
    parse_floor_scope,
    write_csv,
    write_json,
)


PLAN_TITLE_RE = re.compile(r"平面图|平面布置图|平面$|平面", re.I)
REFERENCE_ONLY_TITLE_RE = re.compile(r"(?:详见|参见|见).{0,40}(?:平面图|平面布置图|平面)", re.I)
NON_SPATIAL_TITLE_RE = re.compile(r"系统图|原理图|剖面图|详图|大样图|示意图|计算表", re.I)
FRAME_HINT_RE = re.compile(r"图框|TITLE|FRAME|SHEET|A-EVTR|BORDER|TK", re.I)


def classify_title(text: str, discipline: str, source_name: str = "") -> str | None:
    value = clean_text(text)
    # The positive spatial evidence ("平面") takes precedence over words such
    # as "防雷", "接地" or "剖面".  Combined CAD packages often mention both
    # plan and non-plan deliverables in the same title.  A pure system diagram
    # still fails this gate because it has no plan marker; collect_title_candidates
    # additionally requires an explicit floor scope before creating a sheet.
    if not value or not PLAN_TITLE_RE.search(value) or REFERENCE_ONLY_TITLE_RE.search(value):
        return None
    if discipline == "building":
        if re.search(r"给排水|喷淋|消火栓|消防|电气|动力|照明|报警|暖通|通风|空调", value):
            return None
        return "建筑平面"
    if discipline == "water":
        semantic = f"{value}|{source_name}"
        if re.search(r"喷淋|自动喷水|自喷", semantic):
            return "喷淋"
        if re.search(r"消火栓|消防给水", semantic):
            return "消火栓"
        if re.search(r"给排水|给水|排水|消防", semantic):
            return "给排水"
        return "水专业平面"
    if discipline == "electrical":
        semantic = f"{value}|{source_name}"
        if re.search(r"自动报警|火灾报警|消防报警", semantic):
            return "自动报警"
        if re.search(r"消防照明|应急照明|疏散照明", semantic):
            return "消防照明"
        if re.search(r"动力|配电", semantic):
            return "动力配电"
        if re.search(r"照明", semantic):
            return "照明"
        if re.search(r"弱电|智能化", semantic):
            return "弱电"
        return "电气平面"
    if discipline == "hvac":
        semantic = f"{value}|{source_name}"
        if re.search(r"通风|防排烟|排烟|防烟|送风|暖通|空调", semantic):
            return "暖通平面"
        return "暖通平面"
    return None


def _text_records(doc: ezdxf.document.Drawing) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind in {"TEXT", "MTEXT"}:
            text = entity_text(entity)
            point = entity_point(entity)
            if text and point:
                height = (
                    float(entity.dxf.get("height", 0.0) or 0.0)
                    if kind == "TEXT"
                    else float(entity.dxf.get("char_height", 0.0) or 0.0)
                )
                records.append(
                    {"text": text, "x": point[0], "y": point[1], "height": height,
                     "layer": str(entity.dxf.get("layer", "0")), "source": kind}
                )
        elif kind == "INSERT":
            insert_point = entity_point(entity)
            for attrib in entity.attribs:
                text = entity_text(attrib)
                point = entity_point(attrib) or insert_point
                if text and point:
                    records.append(
                        {"text": text, "x": point[0], "y": point[1],
                         "height": float(attrib.dxf.get("height", 0.0) or 0.0),
                         "layer": str(attrib.dxf.get("layer", entity.dxf.get("layer", "0"))),
                         "source": "ATTRIB"}
                    )
    return records


def _entity_bbox(
    entity: Any,
    doc: ezdxf.document.Drawing,
    block_bbox_cache: dict[str, tuple[float, float, float, float] | None],
) -> tuple[float, float, float, float] | None:
    try:
        kind = entity.dxftype()
        if kind == "INSERT":
            name = str(entity.dxf.get("name", ""))
            if name not in block_bbox_cache:
                try:
                    block = doc.blocks.get(name)
                    local = ezbbox.extents(block, fast=True)
                    block_bbox_cache[name] = (
                        (float(local.extmin.x), float(local.extmin.y), float(local.extmax.x), float(local.extmax.y))
                        if local.has_data else None
                    )
                except Exception:
                    block_bbox_cache[name] = None
            local_bounds = block_bbox_cache[name]
            if not local_bounds:
                return None
            min_x, min_y, max_x, max_y = local_bounds
            matrix = entity.matrix44()
            points = [matrix.transform((x, y, 0.0)) for x, y in (
                (min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y),
            )]
            return (
                min(float(point.x) for point in points), min(float(point.y) for point in points),
                max(float(point.x) for point in points), max(float(point.y) for point in points),
            )
        if kind == "LWPOLYLINE":
            points = list(entity.get_points("xy"))
            if not points:
                return None
            return (
                min(float(point[0]) for point in points), min(float(point[1]) for point in points),
                max(float(point[0]) for point in points), max(float(point[1]) for point in points),
            )
        if kind == "POLYLINE":
            points = [vertex.dxf.location for vertex in entity.vertices]
            if not points:
                return None
            return (
                min(float(point.x) for point in points), min(float(point.y) for point in points),
                max(float(point.x) for point in points), max(float(point.y) for point in points),
            )
        return None
    except Exception:
        return None


def _frame_candidates(
    doc: ezdxf.document.Drawing,
    title_points: list[tuple[float, float]],
    title_height: float,
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    if not title_points:
        return frames
    min_tx = min(point[0] for point in title_points)
    max_tx = max(point[0] for point in title_points)
    min_ty = min(point[1] for point in title_points)
    max_ty = max(point[1] for point in title_points)
    title_span = max(max_tx - min_tx, max_ty - min_ty, 1.0)
    block_bbox_cache: dict[str, tuple[float, float, float, float] | None] = {}

    for entity in doc.modelspace():
        kind = entity.dxftype()
        candidate = False
        if kind == "INSERT":
            signature = f"{entity.dxf.get('layer', '')}|{entity.dxf.get('name', '')}"
            attribute_has_plan_title = any(
                PLAN_TITLE_RE.search(entity_text(attrib) or "")
                and bool(parse_floor_scope(entity_text(attrib)))
                for attrib in entity.attribs
            )
            candidate = bool(FRAME_HINT_RE.search(signature) or attribute_has_plan_title)
        elif kind == "LWPOLYLINE" and bool(entity.closed):
            candidate = len(entity) <= 24
        elif kind == "POLYLINE" and bool(entity.is_closed):
            candidate = True
        if not candidate:
            continue
        bounds = _entity_bbox(entity, doc, block_bbox_cache)
        if not bounds:
            continue
        min_x, min_y, max_x, max_y = bounds
        width, height = max_x - min_x, max_y - min_y
        if width <= 0 or height <= 0 or width / height < 1.05 or width / height > 8.0:
            continue
        if width < title_height * 80.0 or height < title_height * 40.0:
            continue
        if max(width, height) < title_span * 0.03:
            continue
        contained = sum(min_x <= x <= max_x and min_y <= y <= max_y for x, y in title_points)
        if not contained:
            continue
        frames.append(
            {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y,
             "area": width * height, "method": f"{kind.lower()}_frame", "contained_titles": contained}
        )
    return frames


def _global_bounds(doc: ezdxf.document.Drawing) -> tuple[float, float, float, float]:
    extmin = doc.header.get("$EXTMIN")
    extmax = doc.header.get("$EXTMAX")
    try:
        if extmin and extmax and float(extmax.x) > float(extmin.x) and float(extmax.y) > float(extmin.y):
            return float(extmin.x), float(extmin.y), float(extmax.x), float(extmax.y)
    except Exception:
        pass
    box = ezbbox.extents(doc.modelspace(), fast=True)
    if box.has_data:
        return float(box.extmin.x), float(box.extmin.y), float(box.extmax.x), float(box.extmax.y)
    return -1.0, -1.0, 1.0, 1.0


def _fallback_bounds(
    candidate: dict[str, Any],
    candidates: list[dict[str, Any]],
    global_bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    x, y = float(candidate["x"]), float(candidate["y"])
    gmin_x, gmin_y, gmax_x, gmax_y = global_bounds
    width_all, height_all = max(gmax_x - gmin_x, 1.0), max(gmax_y - gmin_y, 1.0)
    height_hint = max(float(candidate.get("height", 0.0) or 0.0), min(width_all, height_all) / 5000.0, 1.0)
    dx_values = sorted(abs(x - float(item["x"])) for item in candidates if abs(x - float(item["x"])) > height_hint * 20)
    dy_values = sorted(abs(y - float(item["y"])) for item in candidates if abs(y - float(item["y"])) > height_hint * 20)
    width = min(width_all, (dx_values[0] * 0.90 if dx_values else width_all))
    height = min(height_all, (dy_values[0] * 0.90 if dy_values else height_all / max(len(candidates), 1)))
    width = max(width, height_hint * 300.0)
    height = max(height, height_hint * 180.0)
    # Plan titles are usually close to the lower edge of the drawing region.
    return x - width * 0.5, y - height * 0.15, x + width * 0.5, y + height * 0.85


def _select_bounds(
    candidate: dict[str, Any],
    frames: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    global_bounds: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float, float], str, float]:
    x, y = float(candidate["x"]), float(candidate["y"])
    containing = [
        frame for frame in frames
        if frame["min_x"] <= x <= frame["max_x"] and frame["min_y"] <= y <= frame["max_y"]
    ]
    if containing:
        frame = min(containing, key=lambda item: float(item["area"]))
        return (
            (float(frame["min_x"]), float(frame["min_y"]), float(frame["max_x"]), float(frame["max_y"])),
            str(frame["method"]),
            0.95,
        )
    return _fallback_bounds(candidate, candidates, global_bounds), "adaptive_title_cell", 0.65


def collect_title_candidates(dxf: Path, discipline: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    doc = ezdxf.readfile(dxf)
    records = _text_records(doc)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record in records:
        text = str(record["text"])
        cleaned = clean_text(text)
        if PLAN_TITLE_RE.search(cleaned) and REFERENCE_ONLY_TITLE_RE.search(cleaned):
            rejected.append({
                "drawing": str(dxf.resolve()), "discipline": discipline,
                "title": text, "x": record["x"], "y": record["y"],
                "layer": record["layer"], "height": record["height"],
                "text_source": record["source"],
                "rejection_reason": "文字仅引用另一张平面图，不是本图标题",
            })
            continue
        kind = classify_title(text, discipline, dxf.stem)
        if not kind:
            continue
        floors = parse_floor_scope(text)
        buildings = parse_building_ids(text)
        row = {
            "drawing": str(dxf.resolve()), "discipline": discipline, "kind": kind,
            "floor": floors[0] if floors else "", "floors": floors, "building_ids": buildings,
            "title": text, "x": record["x"], "y": record["y"], "layer": record["layer"],
            "height": record["height"], "text_source": record["source"],
        }
        if floors:
            accepted.append(row)
        else:
            row["rejection_reason"] = "平面标题未解析出楼层"
            rejected.append(row)

    points = [(float(item["x"]), float(item["y"])) for item in accepted]
    positive_heights = sorted(float(item["height"]) for item in accepted if float(item["height"]) > 0)
    title_height = positive_heights[len(positive_heights) // 2] if positive_heights else 1.0
    frames = _frame_candidates(doc, points, title_height)
    bounds_all = _global_bounds(doc)
    for item in accepted:
        bounds, method, confidence = _select_bounds(item, frames, accepted, bounds_all)
        item["min_x"], item["min_y"], item["max_x"], item["max_y"] = bounds
        item["detection_method"] = method
        item["confidence"] = confidence
    return accepted, rejected


def _same_physical_sheet(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_box = (left["min_x"], left["min_y"], left["max_x"], left["max_y"])
    right_box = (right["min_x"], right["min_y"], right["max_x"], right["max_y"])
    diagonal = math.hypot(float(left["max_x"] - left["min_x"]), float(left["max_y"] - left["min_y"]))
    same_box = max(abs(float(a) - float(b)) for a, b in zip(left_box, right_box)) <= max(diagonal * 0.01, 1.0)
    return same_box and left["kind"] == right["kind"]


def _suppress_multi_sheet_containers(sheets: list[Sheet]) -> list[Sheet]:
    """Remove outer frames which enclose a whole multi-floor drawing set.

    A drawing-set wrapper can accidentally inherit the title of just one inner
    sheet (for example ``ROOF``), so the wrapper's own floor scope is not a
    reliable discriminator.  Suppression instead requires at least three much
    smaller physical sheets from the same drawing, covering at least three
    distinct floors and lying spatially inside the candidate wrapper.

    This deliberately keeps legitimate repeated-standard-floor plans: they do
    not spatially contain several independently detected floor sheets.
    """
    kept: list[Sheet] = []
    for sheet in sheets:
        area = max((sheet.max_x - sheet.min_x) * (sheet.max_y - sheet.min_y), 1e-9)
        nested = []
        for other in sheets:
            if other is sheet or other.drawing != sheet.drawing:
                continue
            other_area = max((other.max_x - other.min_x) * (other.max_y - other.min_y), 0.0)
            if other_area >= area * 0.35:
                continue
            if not (sheet.min_x <= other.center_x <= sheet.max_x and sheet.min_y <= other.center_y <= sheet.max_y):
                continue
            nested.append(other)
        nested_floors = {floor for other in nested for floor in other.floor_ids}
        if len(nested) >= 3 and len(nested_floors) >= 3:
            continue
        kept.append(sheet)
    return kept


def build_sheets(candidates: list[dict[str, Any]]) -> list[Sheet]:
    building_drawings_with_frames = {
        str(item["drawing"]) for item in candidates
        if item["discipline"] == "building" and float(item["confidence"]) >= 0.90
    }
    candidates = [
        item for item in candidates
        if not (
            item["discipline"] == "building"
            and str(item["drawing"]) in building_drawings_with_frames
            and float(item["confidence"]) < 0.90
        )
    ]
    # When a real frame contains the title, duplicated internal block text can
    # also create several adaptive cells for the same logical plan. Prefer the
    # framed records. Keep a frameless record only when it explicitly names a
    # building not covered by any framed record in the same floor/kind scope.
    scoped: dict[tuple[str, str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        scoped[(str(item["drawing"]), str(item["kind"]), tuple(item["floors"]))].append(item)
    filtered: list[dict[str, Any]] = []
    for group in scoped.values():
        framed = [item for item in group if float(item["confidence"]) >= 0.90]
        if not framed:
            filtered.extend(group)
            continue
        filtered.extend(framed)
        covered_buildings = {building for item in framed for building in item["building_ids"]}
        filtered.extend(
            item for item in group
            if float(item["confidence"]) < 0.90
            and item["building_ids"]
            and not set(item["building_ids"]).issubset(covered_buildings)
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in filtered:
        grouped[str(item["drawing"])].append(item)
    sheets: list[Sheet] = []
    for drawing, items in grouped.items():
        clusters: list[list[dict[str, Any]]] = []
        for item in sorted(items, key=lambda row: (-float(row["confidence"]), -float(row["height"]))):
            cluster = next((group for group in clusters if _same_physical_sheet(item, group[0])), None)
            if cluster is None:
                clusters.append([item])
            else:
                cluster.append(item)
        for index, cluster in enumerate(clusters, start=1):
            best = max(cluster, key=lambda row: (float(row["confidence"]), float(row["height"]), len(str(row["title"]))))
            floors = sorted({floor for row in cluster for floor in row["floors"]}, key=floor_sort_key)
            buildings = sorted({building for row in cluster for building in row["building_ids"]}, key=lambda value: int(value))
            signature = (
                f"{drawing}|{best['kind']}|{best['min_x']:.3f}|{best['min_y']:.3f}|"
                f"{best['max_x']:.3f}|{best['max_y']:.3f}"
            )
            sheet_id = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:14]
            center_x = (float(best["min_x"]) + float(best["max_x"])) / 2.0
            center_y = (float(best["min_y"]) + float(best["max_y"])) / 2.0
            sheets.append(
                Sheet(
                    drawing=drawing, discipline=str(best["discipline"]), kind=str(best["kind"]),
                    floor=floors[0] if floors else "", title=str(best["title"]),
                    title_x=float(best["x"]), title_y=float(best["y"]), center_x=center_x, center_y=center_y,
                    min_x=float(best["min_x"]), min_y=float(best["min_y"]),
                    max_x=float(best["max_x"]), max_y=float(best["max_y"]),
                    sheet_id=sheet_id, building_ids=buildings, floors=floors,
                    role="floor_plan", detection_method=str(best["detection_method"]),
                    confidence=float(best["confidence"]),
                )
            )
    sheets = _suppress_multi_sheet_containers(sheets)
    return sorted(
        sheets,
        key=lambda sheet: (sheet.discipline, sheet.kind, floor_sort_key(sheet.floor), sheet.center_y, sheet.center_x),
    )


def run_stage(prepared_payload: dict[str, object], stage_dir: Path) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    all_candidates: list[dict[str, Any]] = []
    rejected_candidates: list[dict[str, Any]] = []
    for drawing in prepared_payload["drawings"]:
        accepted, rejected = collect_title_candidates(Path(str(drawing["dxf"])), str(drawing["discipline"]))
        all_candidates.extend(accepted)
        rejected_candidates.extend(rejected)
    sheets = build_sheets(all_candidates)
    rows = asdict_list(sheets)
    write_json(stage_dir / "sheet_floor_candidates.json", all_candidates)
    write_csv(stage_dir / "sheet_floor_candidates.csv", all_candidates)
    write_json(stage_dir / "rejected_sheet_titles.json", rejected_candidates)
    write_json(stage_dir / "sheets.json", rows)
    write_csv(stage_dir / "sheets.csv", rows)
    counts: dict[str, int] = defaultdict(int)
    for sheet in sheets:
        counts[f"{sheet.discipline}/{sheet.kind}"] += 1
    payload = {
        "stage": "02_sheets",
        "counts": dict(sorted(counts.items())),
        "sheet_count": len(sheets),
        "accepted_title_count": len(all_candidates),
        "rejected_title_count": len(rejected_candidates),
        "sheets_json": str((stage_dir / "sheets.json").resolve()),
    }
    write_json(stage_dir / "stage_summary.json", payload)
    return payload
