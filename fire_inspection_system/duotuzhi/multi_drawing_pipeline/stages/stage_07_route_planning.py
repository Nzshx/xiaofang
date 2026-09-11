from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from ..common import floor_sort_key, read_json, write_json


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
LOCAL_ROUTE_CORE = Path(__file__).resolve().parent / "fire_route_core"
LOCAL_CONSTRAINTS_ROOT = Path(__file__).resolve().parents[1] / "configs"
DEFAULT_DATASET_MANIFEST = (
    WORKSPACE_ROOT
    / "datasets"
    / "semantic_navigation_reachability80_v1"
    / "dataset_manifest.json"
)
DEFAULT_RGCN_CHECKPOINT = (
    WORKSPACE_ROOT
    / "outputs"
    / "semantic_pretraining"
    / "reachability80_v1_run1"
    / "edge_gated_rgcn_pretrained.pt"
)
DEFAULT_ROUTE_HEAD_CHECKPOINT = (
    WORKSPACE_ROOT
    / "outputs"
    / "semantic_pseudo_labels"
    / "reachability80_v1"
    / "model"
    / "pseudo_label_selection_transition_heads.pt"
)


INVENTORY_COLUMNS = (
    "object_id", "layout", "source", "depth", "insert_depth", "entity_type",
    "handle", "layer", "color", "true_color", "linetype", "lineweight",
    "parent_block_name", "block_path", "insert_path", "raw_text", "norm_text",
    "geometry_kind", "is_closed", "x", "y", "bbox_minx", "bbox_miny",
    "bbox_maxx", "bbox_maxy", "bbox_area", "sheet_id", "floor_id", "floor_name",
)


def _require(path: Path, label: str) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label}不存在: {resolved}")
    return resolved


def _load_local_route_stages() -> dict[str, Any]:
    """Load only the self-contained algorithms shipped inside ``duotuzhi``."""
    if not LOCAL_ROUTE_CORE.is_dir():
        raise FileNotFoundError(f"多图纸本地路线核心不存在: {LOCAL_ROUTE_CORE}")
    from .fire_route_core import (
        route_beautification,
        stage_05A_obstacles,
        stage_05B_raw_cad_collision,
        stage_04_inspection_objects,
        stage_06_navigation_graph,
        stage_07_connector_metric_closure,
        stage_08_semantic_rgcn,
        stage_09_dual_graph_planning,
        stage_10_route_outputs,
        stage_11_acceptance_reports,
    )

    return {
        "inspection": stage_04_inspection_objects,
        "building_vision": stage_05A_obstacles,
        "raw_collision": stage_05B_raw_cad_collision,
        "navigation": stage_06_navigation_graph,
        "physical": stage_07_connector_metric_closure,
        "semantic": stage_08_semantic_rgcn,
        "planning": stage_09_dual_graph_planning,
        "outputs": stage_10_route_outputs,
        "acceptance": stage_11_acceptance_reports,
        "beautification": route_beautification,
    }


def prepare_original_obstacle_runtime_contract(
    building_obstacles_path: Path,
    runtime: Path,
) -> dict[str, Any]:
    """Expose the architecture-only Stage 05 contract to copied safety stages."""
    source = (
        building_obstacles_path.resolve().parent
        / "local_route_core"
        / "obstacles"
        / "floor_obstacle_recognition_result.json"
    )
    source = _require(source, "建筑底图原系统障碍识别契约")
    payload = read_json(source)
    required = ("input_dxf", "inventory_dir", "sheets_json", "obstacle_csv")
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise RuntimeError(f"建筑障碍识别契约缺少字段: {', '.join(missing)}")
    destination = runtime / "obstacles" / "floor_obstacle_recognition_result.json"
    write_json(destination, payload)
    return {
        "source_contract": str(source),
        "runtime_contract": str(destination.resolve()),
        "building_input_dxf": str(Path(str(payload["input_dxf"])).resolve()),
        "inventory_dir": str(Path(str(payload["inventory_dir"])).resolve()),
        "sheets_json": str(Path(str(payload["sheets_json"])).resolve()),
    }


def _bbox(sheet: dict[str, Any]) -> tuple[float, float, float, float] | None:
    value = sheet.get("inspection_region_bbox") or sheet.get("bbox")
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(result) != 4 or not all(math.isfinite(item) for item in result):
        return None
    minx, miny, maxx, maxy = result
    return result if maxx > minx and maxy > miny else None


def _choose_sheet(
    target: dict[str, Any],
    sheets_by_floor: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    floor_id = str(target.get("floor_id") or "")
    candidates = sheets_by_floor.get(floor_id, [])
    if not candidates:
        return None
    x, y = float(target["x"]), float(target["y"])
    containing: list[tuple[float, dict[str, Any]]] = []
    for sheet in candidates:
        bounds = _bbox(sheet)
        if bounds is None:
            continue
        minx, miny, maxx, maxy = bounds
        if minx <= x <= maxx and miny <= y <= maxy:
            containing.append(((maxx - minx) * (maxy - miny), sheet))
    if containing:
        return min(containing, key=lambda item: item[0])[1]
    # Keep an exact-floor target in the audit even if it lands outside the floor
    # envelope.  Stage 06 is the authority that rejects it from navigation.
    def distance(item: dict[str, Any]) -> float:
        bounds = _bbox(item)
        if bounds is None:
            return float("inf")
        minx, miny, maxx, maxy = bounds
        return math.hypot(x - (minx + maxx) / 2.0, y - (miny + maxy) / 2.0)

    return min(candidates, key=distance)


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


ROUTE_START_CLASSES = frozenset({"安全出口", "消防电梯"})


def build_architectural_route_starts(
    source_targets_path: Path,
    building_sheets_path: Path,
    output_path: Path,
    inspection_stage: Any,
) -> dict[str, Any]:
    """Select starts exclusively from architectural Object Set members.

    The building C-line has already applied its dedicated alias library. Water,
    electrical and HVAC objects can therefore never become route starts. When a
    floor lacks an explicit architectural start, a verified architectural start
    may be transferred between building floor frames and is recorded as inferred.
    """
    del inspection_stage
    source = read_json(source_targets_path)
    if not isinstance(source, list):
        raise ValueError("离散巡检目标必须是JSON数组")
    floors = sorted(
        {str(row.get("floor_id") or "") for row in source if isinstance(row, dict)} - {""},
        key=floor_sort_key,
    )
    sheets_payload = read_json(building_sheets_path)
    usable_sheets = [
        row for row in sheets_payload.get("sheets", [])
        if isinstance(row, dict) and row.get("path_planning_usable")
    ]
    sheet_by_id = {str(row.get("sheet_id") or ""): row for row in usable_sheets}
    sheets_by_floor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usable_sheets:
        sheets_by_floor[str(row.get("floor_id") or "")].append(row)

    starts = [
        row for row in source
        if isinstance(row, dict)
        and str(row.get("discipline") or "") == "building"
        and str(row.get("target_class") or row.get("category") or "") in ROUTE_START_CLASSES
    ]
    if not starts:
        raise RuntimeError(
            "建筑专业Object Set中没有安全出口或消防电梯，拒绝由水电暖对象代替路线起点"
        )
    starts.sort(
        key=lambda row: (
            0 if str(row.get("target_class") or row.get("category")) == "安全出口" else 1,
            -float(row.get("confidence") or 0.0),
            str(row.get("target_id") or row.get("object_id") or ""),
        )
    )
    explicit_by_floor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in starts:
        explicit_by_floor[str(row.get("floor_id") or "")].append(row)

    route_starts: list[dict[str, Any]] = []
    inferred_starts: list[dict[str, Any]] = []
    source_with_anchor_flags = [dict(row) for row in source if isinstance(row, dict)]
    source_by_target_id = {
        str(row.get("target_id") or ""): row
        for row in source_with_anchor_flags if row.get("target_id")
    }
    for floor_id in floors:
        explicit = explicit_by_floor.get(floor_id, [])
        donor = explicit[0] if explicit else starts[0]
        class_name = str(donor.get("target_class") or donor.get("category"))
        try:
            source_x = float(donor["x"])
            source_y = float(donor["y"])
        except (KeyError, TypeError, ValueError):
            continue
        provenance = "explicit_architectural_symbol"
        source_floor = str(donor.get("floor_id") or "")
        anchor_target_id = str(donor.get("target_id") or "")
        if not explicit:
            source_sheet = sheet_by_id.get(
                str(donor.get("source_sheet_id") or donor.get("sheet_id") or "")
            )
            target_sheets = sheets_by_floor.get(floor_id, [])
            if source_sheet is None or not target_sheets:
                continue
            source_bounds = _bbox(source_sheet)
            target_sheet = min(
                target_sheets,
                key=lambda row: abs(
                    ((_bbox(row) or (0, 0, 0, 0))[2] - (_bbox(row) or (0, 0, 0, 0))[0])
                    - (source_bounds[2] - source_bounds[0] if source_bounds else 0.0)
                ),
            )
            target_bounds = _bbox(target_sheet)
            if source_bounds is None or target_bounds is None:
                continue
            sx1, sy1, sx2, sy2 = source_bounds
            tx1, ty1, tx2, ty2 = target_bounds
            u = min(1.0, max(0.0, (source_x - sx1) / (sx2 - sx1)))
            v = min(1.0, max(0.0, (source_y - sy1) / (sy2 - sy1)))
            source_x = tx1 + u * (tx2 - tx1)
            source_y = ty1 + v * (ty2 - ty1)
            provenance = "normalized_architectural_floor_frame_transfer"
            anchor_target_id = f"BUILDING-START-{floor_id}"
        anchor = {
            "target_id": anchor_target_id,
            "detection_id": anchor_target_id,
            "target_class": class_name,
            "category": class_name,
            "discipline": "building",
            "floor_id": floor_id,
            "floor": floor_id,
            "x": source_x,
            "y": source_y,
            "source_drawing": str(sheets_payload.get("input_dxf") or ""),
            "source_handle": str(donor.get("source_handle") or donor.get("handle") or ""),
            "target_entity_handle": "",
            "source_layer": str(donor.get("source_layer") or donor.get("layer") or ""),
            "source_block_name": str(donor.get("source_block_name") or donor.get("parent_block_name") or ""),
            "confidence": float(donor.get("confidence") or 0.0),
            "route_anchor": True,
            "route_anchor_provenance": provenance,
            "route_anchor_source_floor": source_floor,
            "route_anchor_source_object_id": str(donor.get("object_id") or donor.get("target_id") or ""),
        }
        route_starts.append(anchor)
        if explicit and anchor_target_id in source_by_target_id:
            source_by_target_id[anchor_target_id].update({
                "route_anchor": True,
                "route_anchor_provenance": provenance,
                "route_anchor_source_floor": source_floor,
                "route_anchor_source_object_id": anchor["route_anchor_source_object_id"],
            })
        else:
            inferred_starts.append(anchor)
    missing = sorted(set(floors) - {row["floor_id"] for row in route_starts}, key=floor_sort_key)
    if missing:
        raise RuntimeError("以下楼层无法从建筑安全出口/消防电梯确定起点: " + ", ".join(missing))
    combined = [*source_with_anchor_flags, *inferred_starts]
    write_json(output_path, combined)
    _write_csv(
        output_path.with_name("route_start_anchors.csv"),
        route_starts,
        tuple(sorted({key for row in route_starts for key in row})) or ("target_id",),
    )
    return {
        "path": str(output_path.resolve()),
        "inspection_target_count": len(source),
        "route_anchor_count": len(route_starts),
        "adapter_target_count": len(combined),
        "anchors": route_starts,
        "architectural_recognition_result": "object_set_building_objects",
    }


def build_route_inspection_adapter(
    route_targets_path: Path,
    building_sheets_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Write the exact per-sheet contract consumed by the single-DXF route stages."""
    targets = read_json(route_targets_path)
    if not isinstance(targets, list) or not targets:
        raise RuntimeError("没有可供路线规划的离散巡检对象")
    sheets_payload = read_json(building_sheets_path)
    sheets = [
        row for row in sheets_payload.get("sheets", [])
        if isinstance(row, dict)
        and row.get("path_planning_usable")
        and str(row.get("floor_id") or "")
    ]
    if not sheets:
        raise RuntimeError("建筑底图没有可供路线规划的楼层区域")
    sheets_by_floor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sheet in sheets:
        sheets_by_floor[str(sheet["floor_id"])].append(sheet)

    assigned: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    unassigned: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        sheet = _choose_sheet(target, sheets_by_floor)
        if sheet is None:
            unassigned.append({**target, "reason": "建筑底图不存在同楼层可规划图框"})
            continue
        assigned[str(sheet["sheet_id"])].append((target, sheet))

    output_dir.mkdir(parents=True, exist_ok=True)
    floor_rows: list[dict[str, Any]] = []
    all_catalog: list[dict[str, Any]] = []
    adapter_target_count = 0
    for sheet_id, items in sorted(assigned.items()):
        sheet = items[0][1]
        floor_id = str(sheet["floor_id"])
        floor_name = str(sheet.get("floor_name") or floor_id)
        sheet_dir = output_dir / sheet_id
        inventory_rows: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        class_counts: Counter[tuple[str, str]] = Counter()
        for index, (target, _sheet) in enumerate(items, start=1):
            target_id = str(target.get("target_id") or f"{sheet_id}_{index:07d}")
            class_name = str(target.get("target_class") or target.get("category") or "巡检对象")
            raw_name = str(target.get("category") or class_name)
            x, y = float(target["x"]), float(target["y"])
            discipline = str(target.get("discipline") or "")
            is_building_object = discipline == "building"
            parent_name = (
                f"BUILDING_TARGET::{target_id}"
                if is_building_object
                else f"MIGRATED_TARGET::{target_id}"
            )
            object_id = target_id
            layer = str(target.get("source_layer") or "MIGRATED_INSPECTION_OBJECT")
            handle = str(target.get("target_entity_handle") or target.get("source_handle") or "")
            inventory_rows.append({
                "object_id": object_id,
                "layout": "Model",
                "source": (
                    "building_object_identity"
                    if is_building_object
                    else "migrated_professional_entity"
                ),
                "depth": 0,
                "insert_depth": 0,
                "entity_type": "INSERT",
                "handle": handle,
                "layer": layer,
                "color": "",
                "true_color": "",
                "linetype": "BYLAYER",
                "lineweight": "-1",
                "parent_block_name": parent_name,
                "block_path": json.dumps([parent_name], ensure_ascii=False),
                "insert_path": json.dumps([handle] if handle else [], ensure_ascii=False),
                "raw_text": raw_name,
                "norm_text": "",
                "geometry_kind": "block_insert",
                "is_closed": 0,
                "x": x,
                "y": y,
                "bbox_minx": x - 1.0,
                "bbox_miny": y - 1.0,
                "bbox_maxx": x + 1.0,
                "bbox_maxy": y + 1.0,
                "bbox_area": 4.0,
                "sheet_id": sheet_id,
                "floor_id": floor_id,
                "floor_name": floor_name,
            })
            decisions.append({
                "term": parent_name,
                "source_type": "block",
                "layer": layer,
                "parent_block_name": parent_name,
                "entity_type": "INSERT",
                "geometry_kind": "block_insert",
                "count": 1,
                "sample_object_ids": [object_id],
                "role": "inspection_object",
                "class_name": class_name,
                "standard_class_name": class_name,
                "original_object_name": raw_name,
                "confidence": float(target.get("confidence") or 1.0),
                "reason": (
                    "building_object_identity_adapter"
                    if is_building_object
                    else "multi_drawing_axis_registered_migration"
                ),
                "stage": "multi_drawing_adapter",
            })
            class_counts[(discipline, class_name)] += 1
            adapter_target_count += 1
        catalog_rows = [
            {
                "signature_id": (
                    f"{sheet_id}:BLD_{index:04d}"
                    if discipline == "building"
                    else f"{sheet_id}:MIG_{index:04d}"
                ),
                "sheet_id": sheet_id,
                "semantic_name": class_name,
                "display_name": f"{class_name}({count})",
                "count": count,
                "discipline": discipline,
                "source": (
                    "building_object_identity"
                    if discipline == "building"
                    else "migrated_professional_entity"
                ),
                "layer": (
                    "建筑原生巡检对象"
                    if discipline == "building"
                    else "专业迁移巡检对象"
                ),
                "parent_block_name": "",
                "entity_type": "INSERT",
                "geometry_kind": "block_insert",
                "role": "inspection_object",
                "proposed_role": "inspection_object",
                "confidence": 1.0,
                "reason": (
                    "building_object_identity_adapter"
                    if discipline == "building"
                    else "multi_drawing_axis_registered_migration"
                ),
            }
            for index, ((discipline, class_name), count) in enumerate(
                sorted(class_counts.items()), start=1
            )
        ]
        _write_csv(sheet_dir / "cad_semantic_inventory.csv", inventory_rows, INVENTORY_COLUMNS)
        write_json(sheet_dir / "inspection_objects.json", {
            "catalog_rows": catalog_rows,
            "decisions": decisions,
        })
        all_catalog.extend(catalog_rows)
        floor_rows.append({
            "sheet_id": sheet_id,
            "floor_id": floor_id,
            "source_floor_id": floor_id,
            "floor_name": floor_name,
            "display_name": floor_name,
            "sheet_title": str(sheet.get("sheet_title") or ""),
            "building_scope_id": floor_id,
            "building_id": str(sheet.get("building_id") or ""),
            "building_name": str(sheet.get("building_name") or ""),
            "bbox": list(_bbox(sheet) or []),
            "inspection_instance_count": len(items),
            "inspection_type_count": len(class_counts),
            "catalog_rows": catalog_rows,
        })

    has_professional_objects = any(
        str(row.get("discipline") or "") != "building"
        for row in targets if isinstance(row, dict)
    )
    result = {
        "schema_version": 1,
        "pipeline": [
            "building_object_identity",
            *(["professional_drawing_migration"] if has_professional_objects else []),
            "discrete_target_filter",
            "route_adapter",
        ],
        "region_count": len(floor_rows),
        "inspection_instance_count": adapter_target_count,
        "inspection_type_count": len({row["semantic_name"] for row in all_catalog}),
        "catalog_rows": all_catalog,
        "floors": sorted(floor_rows, key=lambda row: (floor_sort_key(row["floor_id"]), row["sheet_id"])),
        "unassigned_target_count": len(unassigned),
        "artifacts": {},
    }
    result_path = output_dir / "region_inspection_results.json"
    result["artifacts"]["result_json"] = str(result_path.resolve())
    write_json(result_path, result)
    _write_csv(
        output_dir / "route_target_adapter_unassigned.csv",
        unassigned,
        tuple(sorted({key for row in unassigned for key in row})) or ("reason",),
    )
    return {
        "result_json": str(result_path.resolve()),
        "source_target_count": len(targets),
        "adapter_target_count": adapter_target_count,
        "unassigned_target_count": len(unassigned),
        "unassigned_csv": str((output_dir / "route_target_adapter_unassigned.csv").resolve()),
    }


def build_navigation_obstacle_unions(
    building_obstacles_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Dissolve building-only obstacles by floor, retaining closed openings."""
    payload = read_json(building_obstacles_path)
    grouped: dict[str, list[Any]] = defaultdict(list)
    for feature in payload.get("features", []):
        if not isinstance(feature, dict) or not feature.get("geometry"):
            continue
        floor_id = str((feature.get("properties") or {}).get("floor") or "").strip()
        if not floor_id:
            continue
        try:
            geometry = shape(feature["geometry"])
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
        except Exception:
            continue
        if not geometry.is_empty:
            grouped[floor_id].append(geometry)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    declared_count = 0
    for floor_id in sorted(grouped, key=floor_sort_key):
        parts = grouped[floor_id]
        union = unary_union(parts)
        if not union.is_valid:
            union = union.buffer(0)
        if union.is_empty:
            continue
        path = output_dir / f"building_obstacle_union_{floor_id}.geojson"
        write_json(path, {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {
                    "floor_id": floor_id,
                    "floor_name": floor_id,
                    "kind": "building_obstacle_union",
                    "obstacle_count": len(parts),
                    "scope": "building_drawing_only_including_non_passable_openings",
                },
                "geometry": mapping(union),
            }],
        })
        paths.append(str(path.resolve()))
        declared_count += len(parts)
    if not paths:
        raise RuntimeError("建筑底图障碍物为空，拒绝按无障碍空间生成路线")
    return {
        "union_geojsons": paths,
        "obstacle_count": declared_count,
        "floor_count": len(paths),
    }


def build_discrete_constraints(output_path: Path) -> Path:
    source = _require(
        LOCAL_CONSTRAINTS_ROOT / "semantic_inspection_constraints.json",
        "多图纸本地路线约束配置",
    )
    payload = read_json(source)
    removed = {"管网与喷头", "布线", "管网", "风管", "管道"}
    for rule in payload.get("rules", []):
        if isinstance(rule, dict) and isinstance(rule.get("classes"), list):
            rule["classes"] = [value for value in rule["classes"] if value not in removed]
    payload["description"] = (
        "多专业融合后的离散巡检对象约束；连续管网、布线、风管不参与路径目标。"
    )
    payload["excluded_continuous_classes"] = sorted(removed)
    write_json(output_path, payload)
    return output_path.resolve()


def write_multi_drawing_acceptance_report(
    output_path: Path,
    *,
    route_inputs: Any,
    path_result: dict[str, Any],
    acceptance: Any,
    navigation: Any,
) -> Path:
    counts = path_result.get("counts") or {}
    floor_count = len(path_result.get("selected_floor_ids") or [])
    feasible = int(counts.get("dual_graph_feasible_floor_count") or 0)
    virtual = int(counts.get("virtual_continu_entry_count") or counts.get("virtual_entry_count") or 0)
    shortfalls = int(counts.get("inspection_constraint_shortfall_count") or 0)
    verdict = "通过" if feasible == floor_count and virtual == 0 and shortfalls == 0 else "有条件通过"
    anchors = route_inputs.get("anchors") or []
    explicit = sum(row.get("route_anchor_provenance") == "explicit_architectural_symbol" for row in anchors)
    inferred = len(anchors) - explicit
    nav_inputs = getattr(navigation, "inputs", None)
    nav_count = int(getattr(nav_inputs, "target_count", 0))
    nav_skipped = int(getattr(nav_inputs, "skipped_target_count", 0))
    lines = [
        "# 多图纸融合消防巡检路线验收总报告",
        "",
        f"- 自动验收结论：**{verdict}**",
        f"- 规划楼层：{floor_count}；生成可行路线楼层：{feasible}",
        f"- 全专业离散巡检对象：{int(route_inputs['inspection_target_count'])}",
        f"- 实际进入导航图的目标（含建筑起点）：{nav_count}；因建筑楼层区域外跳过：{nav_skipped}",
        f"- 路线选中目标：{int(counts.get('selected_target_count') or 0)}；实际访问次数（含必要重复访问）：{int(counts.get('planned_target_visit_count') or 0)}",
        f"- 路线分段：{int(counts.get('open_route_segment_count') or 0)}；虚拟续接：{virtual}",
        f"- 巡检配额短缺项：{shortfalls}",
        "",
        "## 验收口径",
        "",
        "1. 管网、布线、风管及其他连续走线不属于本次路径目标，不计作迁移失败。",
        "2. 喷头、消火栓、探测器、风机、风口、阀门等具有明确位置的离散图元参与规划。",
        "3. 路线障碍物只取建筑底图；墙体、柱及洞口按不可通行处理。",
        "4. 每层路线必须从建筑底图中的安全出口或消防电梯起步，不允许用水电暖设备代替。",
        "5. 虚拟续接不是可步行路径；若其数量大于0，须在现场确认建筑图未表达的连通关系。",
        "",
        "## 建筑路线起点",
        "",
        f"共 {len(anchors)} 个：建筑图直接识别 {explicit} 个，按同一建筑楼层图框归一位置迁移 {inferred} 个。",
        "",
        "| 楼层 | 起点类别 | 来源方式 | 来源楼层 | 来源对象 |",
        "|---|---|---|---|---|",
    ]
    for row in sorted(anchors, key=lambda item: floor_sort_key(str(item.get("floor_id") or ""))):
        lines.append(
            f"| {row.get('floor_id', '')} | {row.get('target_class', '')} | "
            f"{row.get('route_anchor_provenance', '')} | "
            f"{row.get('route_anchor_source_floor', '')} | "
            f"{row.get('route_anchor_source_object_id', '')} |"
        )
    lines.extend([
        "",
        "## 分楼层验收报告",
        "",
        f"本地路线核心生成的报告索引：`{Path(acceptance.index_markdown).resolve()}`",
        "",
    ])
    for floor_id, report_path in sorted(
        acceptance.floor_reports.items(), key=lambda item: floor_sort_key(item[0])
    ):
        lines.append(f"- {floor_id}: `{Path(report_path).resolve()}`")
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path.resolve()


def run_stage(
    *,
    run_dir: Path,
    fused_dxf_path: Path,
    route_targets_path: Path,
    building_sheets_path: Path,
    building_obstacles_path: Path,
    stage_dir: Path,
    dataset_manifest_path: Path = DEFAULT_DATASET_MANIFEST,
    rgcn_checkpoint_path: Path = DEFAULT_RGCN_CHECKPOINT,
    route_head_checkpoint_path: Path = DEFAULT_ROUTE_HEAD_CHECKPOINT,
    transition_top_k: int = 20,
    device_name: str = "auto",
    dmax_ratio: float = 0.8,
    area_graph_pixel_size: float = 240.0,
    area_graph_max_raster_side: int = 1600,
    area_graph_max_raster_pixels: int = 1_500_000,
    write_dxf: bool = True,
) -> dict[str, Any]:
    fused_dxf = _require(fused_dxf_path, "迁移融合DXF")
    route_targets = _require(route_targets_path, "离散巡检目标")
    building_sheets = _require(building_sheets_path, "建筑楼层图框")
    building_obstacles = _require(building_obstacles_path, "建筑障碍物")
    dataset_manifest = _require(dataset_manifest_path, "R-GCN数据清单")
    rgcn_checkpoint = _require(rgcn_checkpoint_path, "R-GCN权重")
    route_head_checkpoint = _require(route_head_checkpoint_path, "路线头权重")

    stage_dir.mkdir(parents=True, exist_ok=True)
    runtime = stage_dir / "route_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    stages = _load_local_route_stages()
    obstacle_runtime_contract = prepare_original_obstacle_runtime_contract(
        building_obstacles, runtime
    )
    route_inputs = build_architectural_route_starts(
        route_targets,
        building_sheets,
        stage_dir / "route_targets_with_start_anchors.json",
        stages["inspection"],
    )
    adapter = build_route_inspection_adapter(
        Path(route_inputs["path"]), building_sheets, runtime / "inspection_objects"
    )
    obstacles = build_navigation_obstacle_unions(
        building_obstacles, stage_dir / "building_obstacle_unions"
    )
    constraints = build_discrete_constraints(stage_dir / "discrete_route_constraints.json")

    navigation = stages["navigation"].run_stage(
        run_dir=runtime,
        input_dxf=fused_dxf,
        sheets_json=building_sheets,
        obstacle_union_geojsons=[Path(value) for value in obstacles["union_geojsons"]],
        expected_obstacle_count=int(obstacles["obstacle_count"]),
        expected_target_count=int(adapter["adapter_target_count"]),
        area_graph_pixel_size=area_graph_pixel_size,
        area_graph_max_raster_side=area_graph_max_raster_side,
        area_graph_max_raster_pixels=area_graph_max_raster_pixels,
        area_graph_minimum_bottleneck_score=0.06,
        area_graph_maximum_portals_per_floor=600,
        include_area_anchors=True,
    )
    rule_graph = Path(
        str((navigation.area_graph_navigation.get("outputs") or {}).get("graph_json") or "")
    )
    rule_graph = _require(rule_graph, "建筑障碍约束后的规则导航图")
    raw_collision = stages["raw_collision"].build_and_certify_physical_graph(
        runtime,
        rule_graph,
        None,
        source_dxf=Path(obstacle_runtime_contract["building_input_dxf"]),
    )
    physical = stages["physical"].run_stage(
        runtime,
        path_planning_enabled=True,
        force_refinement=False,
        rule_navigation_graph=raw_collision.safe_graph,
    )
    semantic = stages["semantic"].run_stage(
        runtime,
        physical,
        dataset_manifest_path=dataset_manifest,
        rgcn_checkpoint_path=rgcn_checkpoint,
        route_head_checkpoint_path=route_head_checkpoint,
        constraints_path=constraints,
        source_run_id=run_dir.name,
        transition_top_k=transition_top_k,
        device_name=device_name,
    )
    planning = stages["planning"].run_stage(
        runtime, physical, semantic, dmax_ratio=dmax_ratio
    )
    pipeline_summary: dict[str, Any] = {
        "schema_version": 1,
        "pipeline_type": "multi_drawing_discrete_inspection_route",
        "source_run_dir": str(run_dir.resolve()),
        "input_dxf": str(fused_dxf),
        "cad_preprocess": {"sheets_json": str(building_sheets)},
        "inspection_recognition": {
            "inspection_instance_count": int(adapter["adapter_target_count"]),
            "continuous_infrastructure_included": False,
        },
        "obstacle_recognition": obstacles,
        "original_obstacle_runtime_contract": obstacle_runtime_contract,
        "raw_cad_collision_certification": {
            "safe_graph": str(raw_collision.safe_graph),
            "barriers_geojson": str(raw_collision.barriers_geojson),
            "door_evidence_geojson": str(raw_collision.door_evidence_geojson),
            "rejected_edges_geojson": str(raw_collision.rejected_edges_geojson),
            "audit_json": str(raw_collision.audit_json),
            "counts": dict((raw_collision.audit.get("counts") or {})),
        },
        "navigation_graph": navigation.to_dict(),
        "physical_preparation": physical.to_summary(),
    }
    path_result, summary_path = stages["outputs"].run_stage(
        run_dir=runtime,
        input_dxf=fused_dxf,
        pipeline_summary=pipeline_summary,
        physical=physical,
        semantic=semantic,
        planning=planning,
        write_dxf=write_dxf,
    )
    if path_result is None:
        raise RuntimeError("路线输出阶段没有生成路径")
    raw_route_audit = stages["raw_collision"].audit_forwarding_route(
        runtime / "path_planning" / "dual_graph" / "physical_walk" / "forwarding_route.json",
        raw_collision.safe_graph,
        runtime / "raw_cad_safety" / "final_route_raw_cad_audit.json",
    )
    if not raw_route_audit.get("accepted"):
        raise RuntimeError(
            f"最终路线未通过建筑原始CAD障碍碰撞审查: "
            f"{raw_route_audit.get('failure_count', 0)}项"
        )
    acceptance, summary_path = stages["acceptance"].run_stage(
        runtime, pipeline_summary=pipeline_summary, path_result=path_result
    )
    # Beautification is allowed only as a display transformation.  It must pass
    # both the effective-free-space check in the beautifier and an independent
    # raw-CAD recertification; otherwise the already-certified clean route DXF
    # remains the published result.
    beautification = stages["beautification"].run_main_route_beautification(
        runtime,
        fused_dxf,
        physical.effective_free_areas,
        write_dxf=False,
    )
    beauty_outputs = beautification.get("outputs") or {}
    beauty_geojson = Path(str(beauty_outputs.get("beautified_route_geojson") or ""))
    beauty_raw_audit = {
        "accepted": False,
        "failure_count": 1,
        "reason": "beautified_route_not_generated",
    }
    if beauty_geojson.is_file() and beautification.get("status") == "safe":
        beauty_raw_audit = stages["raw_collision"].audit_beautified_route_against_existing_raw_cad(
            runtime,
            runtime / "path_planning" / "dual_graph" / "physical_walk" / "forwarding_route.geojson",
            beauty_geojson,
            raw_collision.barriers_geojson,
            runtime / "raw_cad_safety" / "beautified_route_raw_cad_audit.json",
        )
    if beauty_raw_audit.get("accepted"):
        beautified_dxf = stages["beautification"].write_beautified_route_dxf(
            runtime,
            fused_dxf,
            beauty_geojson,
        )
        beauty_outputs["annotated_route_dxf"] = str(beautified_dxf)
        path_result.setdefault("outputs", {})["annotated_route_dxf"] = str(beautified_dxf)
        beautification["published"] = True
        beautification["fallback_to_original_route"] = False
    else:
        beautification["published"] = False
        beautification["fallback_to_original_route"] = True
    beautification["raw_cad_recertification"] = beauty_raw_audit
    path_result["route_beautification"] = beautification
    path_result["raw_cad_route_audit"] = raw_route_audit
    path_result.setdefault("route_safety_policy", {})[
        "independent_raw_cad_collision_certified"
    ] = True
    path_result["route_safety_policy"][
        "beautified_display_route_requires_raw_cad_recertification"
    ] = True
    path_summary_path = runtime / "path_planning" / "path_planning_summary.json"
    path_result["summary_path"] = str(path_summary_path.resolve())
    write_json(path_summary_path, path_result)
    pipeline_summary["path_planning"] = path_result
    pipeline_summary["acceptance_reporting"] = acceptance.to_summary()
    pipeline_summary["route_beautification"] = beautification
    write_json(Path(summary_path), pipeline_summary)

    acceptance_report = write_multi_drawing_acceptance_report(
        stage_dir / "多图纸融合消防巡检路线_验收总报告.md",
        route_inputs=route_inputs,
        path_result=path_result,
        acceptance=acceptance,
        navigation=navigation,
    )

    output_dxf = str((path_result.get("outputs") or {}).get("annotated_route_dxf") or "")
    payload = {
        "stage": "07_route_planning_and_acceptance",
        "route_core": str(LOCAL_ROUTE_CORE.resolve()),
        "route_core_runtime_dependency": "duotuzhi_local_only",
        "continuous_infrastructure_included": False,
        "source_route_target_count": int(adapter["source_target_count"]),
        "inspection_route_target_count": int(route_inputs["inspection_target_count"]),
        "route_start_anchor_count": int(route_inputs["route_anchor_count"]),
        "route_start_anchor_policy": "仅使用建筑专业Object Set中的安全出口/消防电梯；缺层时按建筑楼层图框归一坐标迁移同一建筑核心位置",
        "adapter_route_target_count": int(adapter["adapter_target_count"]),
        "unassigned_before_navigation": int(adapter["unassigned_target_count"]),
        "navigation_target_count": int(getattr(navigation.inputs, "target_count", 0)),
        "navigation_skipped_target_count": int(getattr(navigation.inputs, "skipped_target_count", 0)),
        "selected_floor_ids": list(semantic.selected_floor_ids),
        "route_dxf": output_dxf,
        "path_summary": str(path_summary_path.resolve()),
        "acceptance_report_index": str(acceptance.index_markdown.resolve()),
        "acceptance_report_summary": str(acceptance.summary_json.resolve()),
        "acceptance_report": str(acceptance_report),
        "floor_acceptance_reports": {
            floor_id: str(path.resolve()) for floor_id, path in acceptance.floor_reports.items()
        },
        "route_runtime_dir": str(runtime.resolve()),
        "constraints": str(constraints),
        "raw_cad_collision_audit": str(raw_collision.audit_json),
        "final_route_raw_cad_audit": str(
            (runtime / "raw_cad_safety" / "final_route_raw_cad_audit.json").resolve()
        ),
        "route_beautification": {
            "status": beautification.get("status"),
            "published": bool(beautification.get("published")),
            "fallback_to_original_route": bool(
                beautification.get("fallback_to_original_route")
            ),
            "raw_cad_recertification": beauty_raw_audit,
        },
        "original_obstacle_runtime_contract": obstacle_runtime_contract,
    }
    write_json(stage_dir / "stage_summary.json", payload)
    return payload
