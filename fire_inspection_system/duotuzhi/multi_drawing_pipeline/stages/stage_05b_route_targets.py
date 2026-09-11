from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from ..common import floor_sort_key, read_json, write_csv, write_json


# Explicit safety boundary for resumed legacy runs: continuous linework is not
# a point inspection target and must never be sent to the route planner.
CONTINUOUS_INFRASTRUCTURE_CATEGORIES = frozenset(
    {"管网", "管网与喷头", "布线", "风管", "管道"}
)


ROUTE_CLASS_ALIASES = {
    "室内消火栓": "室外（内）消火栓",
    "室外消火栓": "室外（内）消火栓",
    "排烟风机": "加压风机（管道）/排烟风机（管道）/补风机（管道）",
    "加压风机": "加压风机（管道）/排烟风机（管道）/补风机（管道）",
    "补风机": "加压风机（管道）/排烟风机（管道）/补风机（管道）",
    "排烟口": "机械排烟",
    "加压送风口": "机械防烟/机械加压送风",
    "补风口": "机械防烟/机械加压送风",
    "防火阀": "防火阀/排烟防火阀",
    "排烟防火阀": "防火阀/排烟防火阀",
}


def canonical_route_class(category: str) -> str:
    """Map detailed recognition labels onto the route constraint vocabulary."""
    value = str(category or "").strip() or "巡检对象"
    return ROUTE_CLASS_ALIASES.get(value, value)


def _is_continuous_infrastructure(row: dict[str, Any]) -> bool:
    category = str(row.get("category") or "").strip()
    entity_type = str(row.get("source_entity_type") or "").upper()
    if category in CONTINUOUS_INFRASTRUCTURE_CATEGORIES:
        return True
    return entity_type in {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE"}


def build_discrete_route_targets(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    targets: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not row.get("migrated"):
            continue
        if _is_continuous_infrastructure(row):
            excluded.append({
                "detection_id": row.get("detection_id", ""),
                "discipline": row.get("discipline", ""),
                "floor": row.get("floor", ""),
                "category": row.get("category", ""),
                "source_drawing": row.get("source_drawing", ""),
                "source_handle": row.get("source_handle", ""),
                "reason": "连续管线不属于当前点式巡检对象范围",
            })
            continue
        detection_id = str(row.get("detection_id") or "")
        if not detection_id or detection_id in seen:
            continue
        seen.add(detection_id)
        try:
            x, y = float(row["target_x"]), float(row["target_y"])
        except (KeyError, TypeError, ValueError):
            continue
        category = str(row.get("category") or "巡检对象")
        targets.append({
            "target_id": detection_id,
            "detection_id": detection_id,
            "target_class": canonical_route_class(category),
            "category": category,
            "discipline": str(row.get("discipline") or ""),
            "floor_id": str(row.get("floor") or ""),
            "floor": str(row.get("floor") or ""),
            "x": x,
            "y": y,
            "source_drawing": str(row.get("source_drawing") or ""),
            "source_handle": str(row.get("source_handle") or ""),
            "target_entity_handle": str(row.get("target_entity_handle") or ""),
            "source_layer": str(row.get("source_layer") or ""),
            "source_block_name": str(row.get("source_block_name") or ""),
            "confidence": float(row.get("confidence") or 1.0),
        })
    targets.sort(
        key=lambda row: (
            floor_sort_key(row["floor_id"]), row["target_class"], row["target_id"]
        )
    )
    return targets, excluded


def merge_building_and_professional_targets(
    building_objects: list[dict[str, Any]],
    professional_targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create one target layer while preserving each object's source discipline."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [*building_objects, *professional_targets]:
        if not isinstance(row, dict):
            continue
        target_id = str(row.get("target_id") or row.get("detection_id") or "").strip()
        if not target_id or target_id in seen:
            continue
        discipline = str(row.get("discipline") or "").strip()
        if discipline not in {"building", "water", "electrical", "hvac"}:
            continue
        try:
            x, y = float(row["x"]), float(row["y"])
        except (KeyError, TypeError, ValueError):
            continue
        category = str(row.get("category") or row.get("target_class") or "巡检对象")
        item = dict(row)
        item.update({
            "target_id": target_id,
            "detection_id": str(row.get("detection_id") or target_id),
            "target_class": canonical_route_class(
                str(row.get("target_class") or category)
            ),
            "category": category,
            "discipline": discipline,
            "floor_id": str(row.get("floor_id") or row.get("floor") or ""),
            "floor": str(row.get("floor") or row.get("floor_id") or ""),
            "x": x,
            "y": y,
        })
        if discipline == "building":
            item.setdefault("coordinate_space", "building_sbm")
            item.setdefault("registration_method", "identity_building_source")
        else:
            item.setdefault("coordinate_space", "building_sbm")
            item.setdefault("registration_method", "professional_to_building_registration")
        seen.add(target_id)
        merged.append(item)
    merged.sort(
        key=lambda row: (
            floor_sort_key(str(row["floor_id"])),
            str(row["discipline"]),
            str(row["target_class"]),
            str(row["target_id"]),
        )
    )
    return merged


def run_stage(
    prepared_payload: dict[str, object],
    sheets_path: Path,
    migration_audit_path: Path,
    fused_dxf_path: Path,
    obstacles_path: Path,
    stage_dir: Path,
    building_objects_path: Path | None = None,
) -> dict[str, object]:
    """Export architectural and registered professional objects as route targets."""
    del prepared_payload, sheets_path, obstacles_path
    stage_dir.mkdir(parents=True, exist_ok=True)
    professional_targets, excluded = build_discrete_route_targets(read_json(migration_audit_path))
    building_objects = (
        read_json(building_objects_path)
        if building_objects_path is not None and Path(building_objects_path).is_file()
        else []
    )
    targets = merge_building_and_professional_targets(
        building_objects,
        professional_targets,
    )
    migration_exclusion_path = (
        Path(migration_audit_path).resolve().parent
        / "continuous_infrastructure_excluded.json"
    )
    if migration_exclusion_path.is_file():
        for row in read_json(migration_exclusion_path):
            excluded.append({
                **row,
                "reason": "迁移前排除：连续管线不写入建筑融合DXF",
            })
    write_json(stage_dir / "route_targets.json", targets)
    write_csv(stage_dir / "route_targets.csv", targets)
    write_csv(stage_dir / "continuous_infrastructure_excluded.csv", excluded)

    counts: Counter[tuple[str, str, str]] = Counter(
        (row["discipline"], row["floor_id"], row["target_class"])
        for row in targets
    )
    count_rows = [
        {"discipline": key[0], "floor": key[1], "category": key[2], "count": value}
        for key, value in sorted(
            counts.items(),
            key=lambda item: (
                item[0][0], floor_sort_key(item[0][1]), item[0][2]
            ),
        )
    ]
    write_csv(stage_dir / "route_target_counts_by_floor.csv", count_rows)
    payload = {
        "stage": "05b_discrete_route_targets",
        "route_planning_performed": False,
        "inspection_center_count": 0,
        "retained_discrete_object_count": len(targets),
        "route_target_count": len(targets),
        "building_object_count": sum(row["discipline"] == "building" for row in targets),
        "professional_object_count": sum(row["discipline"] != "building" for row in targets),
        "continuous_infrastructure_excluded": len(excluded),
        "output_dxf": str(Path(fused_dxf_path).resolve()),
        "route_targets_json": str((stage_dir / "route_targets.json").resolve()),
        "route_targets_csv": str((stage_dir / "route_targets.csv").resolve()),
        "continuous_infrastructure_excluded_csv": str(
            (stage_dir / "continuous_infrastructure_excluded.csv").resolve()
        ),
        "counts_by_floor": count_rows,
    }
    write_json(stage_dir / "stage_summary.json", payload)
    return payload
