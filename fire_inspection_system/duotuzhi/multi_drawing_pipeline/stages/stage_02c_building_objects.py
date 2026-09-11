from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

from ..common import floor_sort_key, read_json, write_csv, write_json


CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
SCOPE_CONFIG = CONFIG_DIR / "discipline_object_scope.json"
BASE_ALIAS_LIBRARY = CONFIG_DIR / "inspection_object_aliases.json"
BASE_KEYWORD_LIBRARY = CONFIG_DIR / "inspection_object_keyword_patterns.json"


def _center(bbox: Any) -> tuple[float, float] | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        min_x, min_y, max_x, max_y = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if max_x < min_x or max_y < min_y:
        return None
    return (min_x + max_x) / 2.0, (min_y + max_y) / 2.0


def filter_building_annotations(
    annotations: list[dict[str, Any]],
    *,
    source_drawing: Path,
    allowed_classes: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only architectural inspection classes and normalize their coordinates."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, float, float]] = set()
    for row in annotations:
        category = str(row.get("standard_class_name") or row.get("class_name") or "").strip()
        point = _center(row.get("bbox"))
        if category not in allowed_classes:
            rejected.append({
                "object_id": str(row.get("object_id") or ""),
                "floor_id": str(row.get("floor_id") or ""),
                "category": category,
                "reason": "来源为建筑图，但类别不在建筑专业巡检对象白名单",
            })
            continue
        if point is None:
            rejected.append({
                "object_id": str(row.get("object_id") or ""),
                "floor_id": str(row.get("floor_id") or ""),
                "category": category,
                "reason": "建筑对象缺少有效矢量包围盒，无法生成巡检点",
            })
            continue
        floor_id = str(row.get("floor_id") or row.get("source_floor_id") or "").strip()
        source_object_id = str(row.get("object_id") or row.get("handle") or "").strip()
        key = (floor_id, source_object_id, category, round(point[0], 6), round(point[1], 6))
        if key in seen:
            continue
        seen.add(key)
        digest = hashlib.sha256(
            "|".join((str(source_drawing.resolve()), *map(str, key))).encode("utf-8")
        ).hexdigest()[:16]
        accepted.append({
            "target_id": f"B-{digest}",
            "detection_id": f"B-{digest}",
            "object_id": f"B-{digest}",
            "discipline": "building",
            "category": category,
            "target_class": category,
            "floor_id": floor_id,
            "floor": floor_id,
            "x": point[0],
            "y": point[1],
            "bbox": [float(value) for value in row["bbox"]],
            "source_drawing": str(source_drawing.resolve()),
            "source_handle": str(row.get("handle") or ""),
            "target_entity_handle": str(row.get("handle") or ""),
            "source_layer": str(row.get("layer") or ""),
            "source_block_name": str(row.get("parent_block_name") or ""),
            "source_object_id": source_object_id,
            "source_sheet_id": str(row.get("sheet_id") or ""),
            "source_floor_id": str(row.get("source_floor_id") or floor_id),
            "building_scope_id": str(row.get("building_scope_id") or floor_id),
            "building_id": str(row.get("building_id") or ""),
            "building_name": str(row.get("building_name") or ""),
            "original_object_name": str(row.get("original_object_name") or row.get("term") or category),
            "confidence": float(row.get("confidence") or 0.0),
            "coordinate_space": "building_sbm",
            "registration_method": "identity_building_source",
            "recognition_reason": str(row.get("reason") or "building_object_recognition"),
        })
    accepted.sort(
        key=lambda item: (
            floor_sort_key(str(item["floor_id"])),
            str(item["category"]),
            str(item["target_id"]),
        )
    )
    return accepted, rejected


def materialize_building_libraries(
    *,
    allowed_classes: set[str],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Create the effective architecture-specific alias and pattern libraries."""
    aliases = read_json(BASE_ALIAS_LIBRARY)
    patterns = read_json(BASE_KEYWORD_LIBRARY)
    alias_path = output_dir / "building_inspection_object_aliases.json"
    pattern_path = output_dir / "building_inspection_object_keyword_patterns.json"
    write_json(alias_path, {
        "description": "建筑专业专用巡检对象同名库；不含水、电、暖通对象。",
        "source_library": str(BASE_ALIAS_LIBRARY.resolve()),
        "objects": [
            row for row in aliases.get("objects", [])
            if isinstance(row, dict) and str(row.get("canonical") or "") in allowed_classes
        ],
    })
    write_json(pattern_path, {
        "description": "建筑专业专用巡检对象关键词规则；不含水、电、暖通对象。",
        "source_library": str(BASE_KEYWORD_LIBRARY.resolve()),
        "rules": [
            row for row in patterns.get("rules", [])
            if isinstance(row, dict) and str(row.get("canonical") or "") in allowed_classes
        ],
    })
    return alias_path.resolve(), pattern_path.resolve()


def _activate_building_libraries(
    inspection_stage: Any,
    alias_path: Path,
    pattern_path: Path,
) -> None:
    """Point the copied architectural recognizer at the building-only libraries."""
    library = getattr(inspection_stage, "_s04_library", None)
    if library is None:
        raise RuntimeError("建筑巡检对象识别模块缺少可配置同名库接口")
    library.DEFAULT_LIBRARY_PATH = alias_path
    library.DEFAULT_KEYWORD_PATTERN_PATH = pattern_path
    library.cached_library.cache_clear()
    library.cached_keyword_patterns.cache_clear()


def run_stage(
    prepared_payload: dict[str, object],
    obstacle_summary: dict[str, object],
    stage_dir: Path,
    *,
    no_llm: bool = False,
) -> dict[str, object]:
    """Run the existing architectural recognizer and enforce architectural scope."""
    from .fire_route_core import stage_04_inspection_objects

    building = next(
        item for item in prepared_payload["drawings"]
        if item["discipline"] == "building"
    )
    source = Path(str(building["dxf"])).resolve()
    inventory_dir = Path(str(obstacle_summary["reference_inventory"])).resolve()
    sheets_path = Path(str(obstacle_summary["effective_planning_sheets"])).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"建筑底图不存在: {source}")
    if not inventory_dir.is_dir():
        raise FileNotFoundError(f"建筑图元清单不存在: {inventory_dir}")
    if not sheets_path.is_file():
        raise FileNotFoundError(f"建筑楼层清单不存在: {sheets_path}")

    stage_dir.mkdir(parents=True, exist_ok=True)
    runtime = stage_dir / "recognition_runtime"
    scope = read_json(SCOPE_CONFIG)
    allowed_classes = set(scope["building"]["allowed_classes"])
    alias_path, pattern_path = materialize_building_libraries(
        allowed_classes=allowed_classes,
        output_dir=stage_dir / "effective_rules",
    )
    _activate_building_libraries(stage_04_inspection_objects, alias_path, pattern_path)
    recognition = stage_04_inspection_objects.run_stage(
        source,
        inventory_dir,
        sheets_path,
        runtime,
        no_llm=no_llm,
    )
    collector = getattr(
        getattr(stage_04_inspection_objects, "_s04_api", None),
        "collect_region_annotations",
        None,
    )
    if collector is None:
        raise RuntimeError("建筑巡检对象识别模块缺少实例回填接口")
    annotations, collection_summary = collector(runtime / "inspection_objects")
    objects, rejected = filter_building_annotations(
        annotations,
        source_drawing=source,
        allowed_classes=allowed_classes,
    )
    object_path = stage_dir / "building_objects.json"
    write_json(object_path, objects)
    write_csv(stage_dir / "building_objects.csv", objects)
    write_json(stage_dir / "discipline_scope_rejections.json", rejected)

    counts = Counter((str(row["floor_id"]), str(row["category"])) for row in objects)
    count_rows = [
        {"discipline": "building", "floor": key[0], "category": key[1], "count": value}
        for key, value in sorted(
            counts.items(), key=lambda item: (floor_sort_key(item[0][0]), item[0][1])
        )
    ]
    write_csv(stage_dir / "building_object_counts_by_floor.csv", count_rows)
    summary = {
        "stage": "02c_building_objects",
        "source_building": str(source),
        "scope_policy": str(SCOPE_CONFIG.resolve()),
        "alias_library": str(alias_path),
        "keyword_pattern_library": str(pattern_path),
        "coordinate_space": "building_sbm",
        "registration_required": False,
        "raw_recognition_instance_count": len(annotations),
        "building_object_count": len(objects),
        "discipline_scope_rejected_count": len(rejected),
        "recognized_safety_exit_count": sum(row["category"] == "安全出口" for row in objects),
        "recognized_fire_elevator_count": sum(row["category"] == "消防电梯" for row in objects),
        "counts_by_floor_category": count_rows,
        "building_objects_json": str(object_path.resolve()),
        "recognition_result_json": str(Path(recognition.result_json).resolve()),
        "recognition_instances_report": (
            str(Path(recognition.marked_report_json).resolve())
            if recognition.marked_report_json else ""
        ),
        "collection_summary": collection_summary,
        "no_llm": bool(no_llm),
    }
    write_json(stage_dir / "stage_summary.json", summary)
    return summary
