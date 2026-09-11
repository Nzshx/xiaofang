from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox
from ezdxf.addons.importer import Importer
from ezdxf.math import Matrix44
from shapely.geometry import Point, shape
from shapely.ops import unary_union

from ..common import Detection, Registration, Sheet, floor_sort_key, read_json, write_csv, write_json
from .room_topology import build_room_topologies, entity_bbox, guard_room_mapping


XDATA_APPID = "DUOTUZHI_INSPECTION"
SUPPORTED_ENTITY_TYPES = {"INSERT", "LINE", "LWPOLYLINE", "ARC", "CIRCLE", "POINT"}
CONTINUOUS_INFRASTRUCTURE_CATEGORIES = frozenset(
    {"管网", "管网与喷头", "布线", "风管", "管道"}
)
CONTINUOUS_ENTITY_TYPES = frozenset({"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE"})


def _is_continuous_infrastructure(item: Detection) -> bool:
    """Exclude professional routing lines while preserving device block graphics."""
    return (
        str(item.category or "").strip() in CONTINUOUS_INFRASTRUCTURE_CATEGORIES
        or str(item.entity_type or "").upper() in CONTINUOUS_ENTITY_TYPES
    )


def _continuous_exclusion_row(item: Detection) -> dict[str, object]:
    return {
        "detection_id": item.detection_id,
        "discipline": item.discipline,
        "floor": item.floor,
        "category": item.category,
        "source_drawing": item.source_drawing,
        "source_sheet_id": item.sheet_id,
        "source_handle": item.handle,
        "source_entity_type": item.entity_type,
        "source_layer": item.layer,
        "reason": "连续管网/布线/风管不迁移到建筑底图，仅在排除审计中留痕",
    }


def _local_hvac_symbol_copy(source_doc: Any, entity: Any) -> tuple[Any, int]:
    """Copy the local fan geometry, excluding remotely displaced descendants.

    A source equipment wrapper sometimes includes an auxiliary INSERT whose
    geometry lies hundreds of symbol-widths away. Use the wrapper's own finite
    primitives as the local reference, in block coordinates. Only the in-memory
    import document is extended; the original CAD on disk is never changed.
    """
    block = source_doc.blocks.get(entity.dxf.name)
    if block is None:
        return entity, 0
    direct = [e for e in block if e.dxftype() in {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE"}]
    if len(direct) < 4:
        return entity, 0
    local = bbox.extents(direct, fast=True)
    whole = bbox.extents([entity], fast=True)
    if not local.has_data or not whole.has_data:
        return entity, 0
    diagonal = math.hypot(local.size.x, local.size.y)
    scale = max(abs(entity.dxf.xscale), abs(entity.dxf.yscale), 1e-12)
    if diagonal <= 1e-9 or max(whole.size.x, whole.size.y) <= diagonal * scale * 10:
        return entity, 0
    margin = diagonal * 0.5
    limits = (local.extmin.x - margin, local.extmin.y - margin,
              local.extmax.x + margin, local.extmax.y + margin)
    kept: list[Any] = []
    removed = 0

    def visit(e: Any, inherited: str, depth: int) -> None:
        nonlocal removed
        if depth > 10:
            removed += 1
            return
        layer = str(e.dxf.layer)
        effective = inherited if layer == "0" else layer
        if e.dxftype() == "INSERT":
            for child in e.virtual_entities():
                visit(child, effective, depth + 1)
            return
        box = bbox.extents([e], fast=True)
        if not box.has_data:
            removed += 1
            return
        if (box.extmin.x < limits[0] or box.extmin.y < limits[1]
                or box.extmax.x > limits[2] or box.extmax.y > limits[3]):
            removed += 1
            return
        copied = e.copy()
        copied.dxf.layer = effective or "0"
        kept.append(copied)

    for child in block:
        visit(child, "0", 0)
    if not removed or not kept:
        return entity, 0
    name = f"DUOTUZHI_LOCAL_SYMBOL_{entity.dxf.handle}"
    local_block = source_doc.blocks.new(name, base_point=block.block.dxf.base_point)
    for child in kept:
        local_block.add_entity(child)
    copied_insert = entity.copy()
    copied_insert.dxf.name = name
    return copied_insert, removed


def _save_with_locked_fallback(doc: ezdxf.document.Drawing, output: Path) -> Path:
    """Save beside an output currently locked by CAD instead of overwriting it."""
    try:
        doc.saveas(output)
        return output
    except PermissionError:
        for index in range(1, 1000):
            alternative = output.with_name(f"{output.stem}_更新{index}{output.suffix}")
            try:
                doc.saveas(alternative)
                return alternative
            except PermissionError:
                continue
        raise PermissionError(f"融合DXF及999个更新文件均被占用: {output}")


def _transform_matrix(
    registration: Registration,
    adjust_x: float = 0.0,
    adjust_y: float = 0.0,
) -> Matrix44:
    return Matrix44.chain(
        Matrix44.scale(registration.scale_x, registration.scale_y, 1.0),
        Matrix44.z_rotate(math.radians(registration.rotation_deg)),
        Matrix44.translate(
            registration.translate_x + adjust_x,
            registration.translate_y + adjust_y,
            0.0,
        ),
    )


def _mapped_point(item: Detection, registration: Registration) -> tuple[float, float]:
    rotation = math.radians(registration.rotation_deg)
    cos_r, sin_r = math.cos(rotation), math.sin(rotation)
    return (
        registration.scale_x * (cos_r * item.x - sin_r * item.y) + registration.translate_x,
        registration.scale_y * (sin_r * item.x + cos_r * item.y) + registration.translate_y,
    )


def _load_floor_obstacle_envelopes(stage_dir: Path) -> dict[str, tuple[float, float, float, float]]:
    """Build generous occupied-floor envelopes from architecture obstacles.

    A physical CAD sheet can contain a real floor plan plus system diagrams,
    legends or repeated details inside the same title frame.  Registration can
    map all of them consistently, so the sheet frame alone is not enough to
    prove that a professional symbol belongs to the spatial floor.  The
    architecture-only obstacle output provides a project-independent occupied
    area for that final check.
    """
    path = stage_dir.parent / "02b_building_obstacles" / "building_obstacles.geojson"
    if not path.exists():
        return {}
    payload = read_json(path)
    envelopes: dict[str, tuple[float, float, float, float]] = {}
    for feature in payload.get("features", []):
        properties = feature.get("properties", {})
        floor = str(properties.get("floor", ""))
        bbox = properties.get("bbox", [])
        if not floor or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = map(float, bbox)
        current = envelopes.get(floor)
        envelopes[floor] = (
            min(current[0], x0) if current else x0,
            min(current[1], y0) if current else y0,
            max(current[2], x1) if current else x1,
            max(current[3], y1) if current else y1,
        )
    return envelopes


def _inside_floor_occupancy(
    floor: str,
    x: float,
    y: float,
    envelopes: dict[str, tuple[float, float, float, float]],
    margin_ratio: float = 0.12,
) -> bool:
    envelope = envelopes.get(floor)
    if not envelope:
        return True
    x0, y0, x1, y1 = envelope
    margin_x = max((x1 - x0) * margin_ratio, 1.0)
    margin_y = max((y1 - y0) * margin_ratio, 1.0)
    return x0 - margin_x <= x <= x1 + margin_x and y0 - margin_y <= y <= y1 + margin_y


def _hvac_spatial_reference_status(
    floor: str,
    x: float,
    y: float,
    envelopes: dict[str, tuple[float, float, float, float]],
    hulls: dict[str, Any],
) -> tuple[str, bool | None, bool | None]:
    """Record architecture-space evidence without creating a third decision state.

    Migration is deliberately binary: an object is either copied or rejected by
    a hard prerequisite such as registration, target floor or entity import.
    Obstacle envelopes and convex hulls are incomplete proxies for a building's
    usable floor area, especially at roofs and equipment bands, so they are
    retained only as machine-readable evidence for later route filtering.
    """
    inside_envelope = _inside_floor_occupancy(floor, x, y, envelopes)
    hull = hulls.get(floor)
    inside_hull = hull.covers(Point(x, y)) if hull is not None else None
    outside = (inside_envelope is False) or (inside_hull is False)
    return (
        "outside_architecture_reference_migrated" if outside else "inside_architecture_reference",
        inside_envelope,
        inside_hull,
    )


def _load_floor_occupancy_hulls(stage_dir: Path) -> dict[str, Any]:
    """Use the architecture's occupied shape to exclude blank sheet corners.

    The convex hull permits normal open rooms and roof areas between structural
    elements. A scale-relative buffer allows equipment near exterior walls;
    it does not claim to establish room identity or passability.
    """
    path = stage_dir.parent / "02b_building_obstacles" / "building_obstacles.geojson"
    if not path.exists():
        return {}
    grouped: dict[str, list[Any]] = defaultdict(list)
    for feature in read_json(path).get("features", []):
        floor = str(feature.get("properties", {}).get("floor", ""))
        geometry = feature.get("geometry")
        if floor and geometry:
            polygon = shape(geometry)
            if not polygon.is_empty and polygon.is_valid:
                grouped[floor].append(polygon)
    result = {}
    for floor, polygons in grouped.items():
        hull = unary_union(polygons).convex_hull
        if hull.area <= 0:
            continue
        x0, y0, x1, y1 = hull.bounds
        result[floor] = hull.buffer(math.hypot(x1 - x0, y1 - y0) * 0.02)
    return result


def _set_trace_xdata(entity: Any, item: Detection) -> None:
    entity.set_xdata(
        XDATA_APPID,
        [
            (1000, item.detection_id),
            (1000, item.category),
            (1000, item.floor),
            (1000, item.source_drawing),
            (1000, item.handle),
            (1000, item.layer),
        ],
    )


def _import_entity(
    importer: Importer,
    target_layout: Any,
    source_entity: Any,
    matrix: Matrix44,
    item: Detection,
) -> Any | None:
    before = len(target_layout)
    importer.import_entity(source_entity, target_layout)
    if len(target_layout) <= before:
        return None
    imported = target_layout[-1]
    imported.transform(matrix)
    # Importer normally preserves the layer already; assign explicitly so the
    # migrated object never falls onto a synthetic migration layer.
    imported.dxf.layer = item.layer or "0"
    _set_trace_xdata(imported, item)
    return imported


def _add_name_label(
    doc: ezdxf.document.Drawing,
    item: Detection,
    registration: Registration,
    target_sheet: Sheet,
    target_point: tuple[float, float] | None = None,
) -> str:
    x, y = target_point or _mapped_point(item, registration)
    layer = item.layer or "0"
    if layer not in doc.layers:
        doc.layers.add(layer)
    diagonal = math.hypot(
        target_sheet.max_x - target_sheet.min_x,
        target_sheet.max_y - target_sheet.min_y,
    )
    text_height = max(diagonal * 0.00035, 1e-6)
    label = doc.modelspace().add_text(
        item.category,
        height=text_height,
        dxfattribs={"layer": layer, "color": 256},
    )
    label.set_placement((x + text_height * 2.2, y + text_height * 1.8))
    _set_trace_xdata(label, item)
    return str(label.dxf.handle or "")


def _prepare_direct_open_view(
    doc: ezdxf.document.Drawing,
    target_sheets: dict[str, Sheet],
) -> dict[str, object]:
    """Clean imported dependencies and make CAD open on the building sheets.

    Imported block attributes can reference text styles that are not present in
    the building drawing.  AutoCAD prints one warning for every such attribute
    and eventually stops at ``Press ENTER to continue``.  The ezdxf audit fixes
    these dangling references before the file is written.

    DXF files also keep an independent *Active modelspace viewport.  Keeping the
    source drawing viewport can point the first screen at (0, 0), while this
    project is around x=6,000,000.  Reset it to the union of the recognized
    building-floor frames so the result is visible immediately after opening.
    """
    auditor = doc.audit()
    sheets = list(target_sheets.values())
    if not sheets:
        return {"audit_errors": len(auditor.errors), "audit_fixes": len(auditor.fixes)}

    min_x = min(sheet.min_x for sheet in sheets)
    min_y = min(sheet.min_y for sheet in sheets)
    max_x = max(sheet.max_x for sheet in sheets)
    max_y = max(sheet.max_y for sheet in sheets)
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    aspect_ratio = 16.0 / 9.0
    view_height = max(max_y - min_y, (max_x - min_x) / aspect_ratio, 1.0) * 1.08

    doc.set_modelspace_vport(
        view_height,
        center=(center_x, center_y),
        dxfattribs={"aspect_ratio": aspect_ratio},
    )
    doc.header["$TILEMODE"] = 1
    doc.header["$LIMMIN"] = (min_x, min_y)
    doc.header["$LIMMAX"] = (max_x, max_y)
    return {
        "audit_errors": len(auditor.errors),
        "audit_fixes": len(auditor.fixes),
        "initial_view_center": [center_x, center_y],
        "initial_view_height": view_height,
        "initial_view_bounds": [min_x, min_y, max_x, max_y],
    }


def run_stage(
    prepared_payload: dict[str, object],
    sheets_path: Path,
    detections_path: Path,
    registrations_path: Path,
    stage_dir: Path,
) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    sheets = [Sheet(**item) for item in read_json(sheets_path)]
    all_detections = [Detection(**item) for item in read_json(detections_path)]
    excluded_continuous = [item for item in all_detections if _is_continuous_infrastructure(item)]
    detections = [item for item in all_detections if not _is_continuous_infrastructure(item)]
    excluded_rows = [_continuous_exclusion_row(item) for item in excluded_continuous]
    write_json(stage_dir / "continuous_infrastructure_excluded.json", excluded_rows)
    write_csv(stage_dir / "continuous_infrastructure_excluded.csv", excluded_rows)
    registrations = [Registration(**item) for item in read_json(registrations_path)]
    target_sheets = {sheet.sheet_id: sheet for sheet in sheets if sheet.discipline == "building"}
    floor_obstacle_envelopes = _load_floor_obstacle_envelopes(stage_dir)
    floor_occupancy_hulls = _load_floor_occupancy_hulls(stage_dir)
    registration_map = {item.source_sheet_id: item for item in registrations if item.source_sheet_id}
    building_item = next(item for item in prepared_payload["drawings"] if item["discipline"] == "building")
    building_path = Path(building_item["dxf"]).resolve()
    doc = ezdxf.readfile(building_path)
    target_room_topologies = build_room_topologies(doc, target_sheets.values())
    if XDATA_APPID not in doc.appids:
        doc.appids.add(XDATA_APPID)
    target_layout = doc.modelspace()

    by_source: dict[str, list[Detection]] = defaultdict(list)
    for item in detections:
        by_source[item.source_drawing].append(item)

    migration_by_id: dict[str, dict[str, object]] = {}
    labels_to_add: list[tuple[Detection, Registration, Sheet, tuple[float, float]]] = []
    imported_keys: set[tuple[str, str]] = set()
    for source_name, source_items in by_source.items():
        source_path = Path(source_name)
        source_doc = ezdxf.readfile(source_path)
        source_sheets = [
            sheet for sheet in sheets
            if sheet.discipline != "building" and Path(sheet.drawing).resolve() == source_path.resolve()
        ]
        source_room_topologies = build_room_topologies(source_doc, source_sheets)
        importer = Importer(source_doc, doc)
        for item in source_items:
            registration = registration_map.get(item.sheet_id)
            target_sheet = target_sheets.get(registration.target_sheet_id) if registration else None
            target_x = target_y = None
            migrated = False
            reason = ""
            imported_handle = ""
            excluded_remote_primitives = 0
            spatial_reference_status = "not_applicable"
            inside_obstacle_envelope = None
            inside_occupancy_hull = None
            room_guard = {
                "status": "not_applicable", "source_room_name": "", "target_room_name": "",
                "wall_crossing_detected": False, "adjust_x": 0.0, "adjust_y": 0.0,
                "reason": "非设备INSERT不执行房间拓扑保护",
            }
            if not registration:
                reason = "缺少该专业分图/楼层的配准记录"
            elif not registration.accepted:
                reason = registration.reason
            elif not target_sheet:
                reason = "建筑图缺少对应楼层"
            else:
                target_x, target_y = _mapped_point(item, registration)
                if not target_sheet.contains(target_x, target_y):
                    reason = "迁移图元基点落在目标楼层图框之外，已拒绝"
                else:
                    if item.discipline == "hvac":
                        (
                            spatial_reference_status,
                            inside_obstacle_envelope,
                            inside_occupancy_hull,
                        ) = _hvac_spatial_reference_status(
                            item.floor, target_x, target_y,
                            floor_obstacle_envelopes, floor_occupancy_hulls,
                        )
                if not reason and item.entity_type not in SUPPORTED_ENTITY_TYPES:
                    reason = f"暂不支持复制源图元类型:{item.entity_type}"
                elif not reason:
                    source_entity = source_doc.entitydb.get(item.handle)
                    if source_entity is None:
                        reason = "按源句柄未找到图元"
                    else:
                        if item.entity_type == "INSERT":
                            room_guard = guard_room_mapping(
                                (item.x, item.y), (target_x, target_y),
                                source_room_topologies.get(item.sheet_id),
                                target_room_topologies.get(target_sheet.sheet_id),
                                registration,
                                source_bbox=entity_bbox(source_entity),
                            )
                            if str(room_guard["status"]).startswith("rejected_"):
                                reason = f"房间拓扑保护拒绝:{room_guard['reason']}"
                            elif room_guard["status"] == "adjusted_to_same_room":
                                target_x += float(room_guard["adjust_x"])
                                target_y += float(room_guard["adjust_y"])
                                if not target_sheet.contains(target_x, target_y):
                                    reason = "房间相对位置校正后落在目标楼层图框之外，已拒绝"
                                elif item.discipline == "hvac":
                                    (
                                        spatial_reference_status,
                                        inside_obstacle_envelope,
                                        inside_occupancy_hull,
                                    ) = _hvac_spatial_reference_status(
                                        item.floor, target_x, target_y,
                                        floor_obstacle_envelopes, floor_occupancy_hulls,
                                    )
                        if reason:
                            migration_by_id[item.detection_id] = {
                                "detection_id": item.detection_id,
                                "discipline": item.discipline,
                                "floor": item.floor,
                                "floors": item.floors,
                                "building_ids": item.building_ids,
                                "source_sheet_id": item.sheet_id,
                                "category": item.category,
                                "source_drawing": item.source_drawing,
                                "source_kind": item.sheet_kind,
                                "source_handle": item.handle,
                                "source_entity_type": item.entity_type,
                                "source_layer": item.layer,
                                "source_block_name": item.block_name,
                                "excluded_remote_auxiliary_primitives": excluded_remote_primitives,
                                "source_x": item.x, "source_y": item.y,
                                "target_x": target_x, "target_y": target_y,
                                "target_entity_handle": "", "target_layer": "", "label_handle": "",
                                "migrated": False,
                                "registration_method": registration.method,
                                "registration_acceptance_level": registration.acceptance_level,
                                "registration_fallback_support": registration.fallback_support,
                                "registration_fallback_evidence": registration.fallback_evidence,
                                "registration_scale": registration.scale_x,
                                "registration_rotation_deg": registration.rotation_deg,
                                "registration_median_residual": registration.median_residual,
                                "room_guard_status": room_guard["status"],
                                "source_room_name": room_guard.get("source_room_name", ""),
                                "target_room_name": room_guard.get("target_room_name", ""),
                                "source_room_distance": room_guard.get("source_room_distance"),
                                "target_room_distance": room_guard.get("target_room_distance"),
                                "wall_crossing_detected": room_guard.get("wall_crossing_detected", False),
                                "room_adjust_x": room_guard.get("adjust_x", 0.0),
                                "room_adjust_y": room_guard.get("adjust_y", 0.0),
                                "wall_safety_margin": room_guard.get("wall_safety_margin"),
                                "target_wall_clearance": room_guard.get("target_wall_clearance"),
                                "room_guard_reason": room_guard.get("reason", ""),
                                "spatial_reference_status": spatial_reference_status,
                                "inside_obstacle_envelope": inside_obstacle_envelope,
                                "inside_occupancy_hull": inside_occupancy_hull,
                                "reason": reason,
                            }
                            continue
                        key = (item.source_drawing, item.handle)
                        if key in imported_keys:
                            migrated = True
                            reason = "源图元已由同一识别记录复制，跳过重复实体"
                        else:
                            try:
                                if item.discipline == "hvac" and "风机" in item.category:
                                    source_entity, excluded_remote_primitives = _local_hvac_symbol_copy(
                                        source_doc, source_entity,
                                    )
                                imported = _import_entity(
                                    importer,
                                    target_layout,
                                    source_entity,
                                    _transform_matrix(
                                        registration,
                                        float(room_guard.get("adjust_x", 0.0)),
                                        float(room_guard.get("adjust_y", 0.0)),
                                    ),
                                    item,
                                )
                            except Exception as exc:
                                imported = None
                                reason = f"复制/变换源图元失败:{type(exc).__name__}:{exc}"
                            if imported is not None:
                                imported_keys.add(key)
                                imported_handle = str(imported.dxf.handle or "")
                                migrated = True
                                reason = (
                                    f"{registration.reason}; 房间保护:{room_guard['reason']}; "
                                    f"空间参考:{spatial_reference_status}"
                                )
                                # 水管/电线实体保留原图层即可；暖通只接收设备块，不迁移风管。
                                # 设备 INSERT 额外加一个小型类别文字，且仍使用对象原图层。
                                if item.entity_type == "INSERT" and item.category != "布线":
                                    labels_to_add.append((item, registration, target_sheet, (target_x, target_y)))
                            elif not reason:
                                reason = "Importer未生成目标图元"
            migration_by_id[item.detection_id] = {
                "detection_id": item.detection_id,
                "discipline": item.discipline,
                "floor": item.floor,
                "floors": item.floors,
                "building_ids": item.building_ids,
                "source_sheet_id": item.sheet_id,
                "category": item.category,
                "source_drawing": item.source_drawing,
                "source_kind": item.sheet_kind,
                "source_handle": item.handle,
                "source_entity_type": item.entity_type,
                "source_layer": item.layer,
                "source_block_name": item.block_name,
                "excluded_remote_auxiliary_primitives": excluded_remote_primitives,
                "source_x": item.x,
                "source_y": item.y,
                "target_x": target_x,
                "target_y": target_y,
                "target_entity_handle": imported_handle,
                "target_layer": item.layer if migrated else "",
                "label_handle": "",
                "migrated": migrated,
                "registration_method": registration.method if registration else "",
                "registration_acceptance_level": registration.acceptance_level if registration else "",
                "registration_fallback_support": registration.fallback_support if registration else 0,
                "registration_fallback_evidence": registration.fallback_evidence if registration else "",
                "registration_scale": registration.scale_x if registration else None,
                "registration_rotation_deg": registration.rotation_deg if registration else None,
                "registration_median_residual": registration.median_residual if registration else None,
                "room_guard_status": room_guard["status"],
                "source_room_name": room_guard.get("source_room_name", ""),
                "target_room_name": room_guard.get("target_room_name", ""),
                "source_room_distance": room_guard.get("source_room_distance"),
                "target_room_distance": room_guard.get("target_room_distance"),
                "wall_crossing_detected": room_guard.get("wall_crossing_detected", False),
                "room_adjust_x": room_guard.get("adjust_x", 0.0),
                "room_adjust_y": room_guard.get("adjust_y", 0.0),
                "wall_safety_margin": room_guard.get("wall_safety_margin"),
                "target_wall_clearance": room_guard.get("target_wall_clearance"),
                "room_guard_reason": room_guard.get("reason", ""),
                "spatial_reference_status": spatial_reference_status,
                "inside_obstacle_envelope": inside_obstacle_envelope,
                "inside_occupancy_hull": inside_occupancy_hull,
                "reason": reason,
            }
        importer.finalize()

    for item, registration, target_sheet, target_point in labels_to_add:
        label_handle = _add_name_label(doc, item, registration, target_sheet, target_point)
        migration_by_id[item.detection_id]["label_handle"] = label_handle

    migration_rows = [migration_by_id[item.detection_id] for item in detections]
    direct_open_qa = _prepare_direct_open_view(doc, target_sheets)
    requested_output_dxf = stage_dir / "建筑底图_水电暖巡检对象_融合图.dxf"
    output_dxf = _save_with_locked_fallback(doc, requested_output_dxf)
    write_json(stage_dir / "migration_audit.json", migration_rows)
    write_csv(stage_dir / "migration_audit.csv", migration_rows)
    room_guard_rows = [
        {
            "detection_id": row["detection_id"],
            "discipline": row["discipline"],
            "floor": row["floor"],
            "category": row["category"],
            "source_drawing": row["source_drawing"],
            "source_handle": row["source_handle"],
            "source_x": row["source_x"],
            "source_y": row["source_y"],
            "target_x": row["target_x"],
            "target_y": row["target_y"],
            "migrated": row["migrated"],
            "room_guard_status": row.get("room_guard_status", "not_applicable"),
            "source_room_name": row.get("source_room_name", ""),
            "target_room_name": row.get("target_room_name", ""),
            "source_room_distance": row.get("source_room_distance"),
            "target_room_distance": row.get("target_room_distance"),
            "wall_crossing_detected": row.get("wall_crossing_detected", False),
            "room_adjust_x": row.get("room_adjust_x", 0.0),
            "room_adjust_y": row.get("room_adjust_y", 0.0),
            "wall_safety_margin": row.get("wall_safety_margin"),
            "target_wall_clearance": row.get("target_wall_clearance"),
            "room_guard_reason": row.get("room_guard_reason", ""),
        }
        for row in migration_rows if row.get("source_entity_type") == "INSERT"
    ]
    write_csv(stage_dir / "room_topology_audit.csv", room_guard_rows)
    count_map: Counter[tuple[str, str, str, bool]] = Counter(
        (str(row["discipline"]), str(row["floor"]), str(row["category"]), bool(row["migrated"]))
        for row in migration_rows
    )
    count_rows = [
        {
            "discipline": key[0],
            "floor": key[1],
            "category": key[2],
            "status": "已迁移" if key[3] else "拒绝",
            "count": value,
        }
        for key, value in sorted(
            count_map.items(), key=lambda pair: (pair[0][0], floor_sort_key(pair[0][1]), pair[0][2], not pair[0][3])
        )
    ]
    write_csv(stage_dir / "migration_counts_by_floor.csv", count_rows)
    migrated = sum(bool(row["migrated"]) for row in migration_rows)
    room_guard_counts = dict(Counter(
        str(row.get("room_guard_status", "not_applicable")) for row in migration_rows
        if row.get("source_entity_type") == "INSERT"
    ))
    payload = {
        "stage": "05_migrate_actual_entities",
        "detected_total": len(all_detections),
        "discrete_detection_total": len(detections),
        "continuous_infrastructure_excluded": len(excluded_continuous),
        "migrated": migrated,
        "rejected": len(detections) - migrated,
        "actual_source_entities_imported": len(imported_keys),
        "name_labels_added_on_original_layers": len(labels_to_add),
        "synthetic_migration_boxes_added": 0,
        "room_guard_counts": room_guard_counts,
        "direct_open_qa": direct_open_qa,
        "output_dxf": str(output_dxf.resolve()),
        "migration_audit_csv": str((stage_dir / "migration_audit.csv").resolve()),
        "continuous_infrastructure_excluded_csv": str(
            (stage_dir / "continuous_infrastructure_excluded.csv").resolve()
        ),
        "room_topology_audit_csv": str((stage_dir / "room_topology_audit.csv").resolve()),
        "counts_by_floor": count_rows,
    }
    write_json(stage_dir / "stage_summary.json", payload)
    return payload
