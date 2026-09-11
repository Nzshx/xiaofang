from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common import read_json, write_json


SCHEMA_VERSION = 1
SBM_ALGORITHM_VERSION = "duotuzhi-sbm-v1"
OBJECT_SET_ALGORITHM_VERSION = "duotuzhi-object-set-v1"
ENVIRONMENT_ALGORITHM_VERSION = "duotuzhi-environment-v1"
ROUTE_VERSION_ALGORITHM = "duotuzhi-route-binding-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any, prefix: str) -> str:
    return f"{prefix}_{hashlib.sha256(_json_bytes(value)).hexdigest()[:20]}"


def file_sha256(path: Path | str) -> str:
    source = Path(path).resolve()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path | str) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        return {"path": str(source), "exists": False, "sha256": "", "size": 0}
    return {
        "path": str(source),
        "exists": True,
        "sha256": file_sha256(source),
        "size": source.stat().st_size,
    }


def _copy_if_missing(source: Path | str, destination: Path) -> Path:
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        shutil.copy2(source_path, destination)
    elif file_sha256(destination) != file_sha256(source_path):
        raise RuntimeError(f"版本目录中已存在同名但内容不同的文件: {destination}")
    return destination.resolve()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_-]+", "_", str(value or "UNKNOWN")).strip("_")
    return cleaned or "UNKNOWN"


def _write_per_floor_obstacles(source_path: Path, output_dir: Path) -> list[str]:
    payload = read_json(source_path)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for feature in payload.get("features", []):
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        floor_id = str(properties.get("floor") or properties.get("floor_id") or "UNKNOWN")
        grouped[floor_id].append(feature)
    paths: list[str] = []
    for floor_id, features in sorted(grouped.items()):
        destination = output_dir / f"{_safe_name(floor_id)}.geojson"
        if not destination.is_file():
            write_json(destination, {
                "type": "FeatureCollection",
                "features": features,
                "properties": {
                    "floor_id": floor_id,
                    "source": "building_drawing_only",
                    "authority": "SBM vector obstacle layer",
                },
            })
        paths.append(str(destination.resolve()))
    return paths


def _connect_registry(registry_path: Path) -> sqlite3.Connection:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(registry_path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sbm_versions (
            version TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS object_set_versions (
            version TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            sbm_version TEXT NOT NULL,
            object_count INTEGER NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS environment_versions (
            version TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            sbm_version TEXT NOT NULL,
            object_set_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS route_versions (
            version TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            manifest_path TEXT NOT NULL,
            environment_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        """
    )
    return connection


def _register(
    registry_path: Path,
    table: str,
    values: tuple[Any, ...],
) -> None:
    statements = {
        "sbm_versions": "INSERT OR IGNORE INTO sbm_versions VALUES (?, ?, ?, ?, ?)",
        "object_set_versions": "INSERT OR IGNORE INTO object_set_versions VALUES (?, ?, ?, ?, ?, ?)",
        "environment_versions": "INSERT OR IGNORE INTO environment_versions VALUES (?, ?, ?, ?, ?, ?)",
        "route_versions": "INSERT OR IGNORE INTO route_versions VALUES (?, ?, ?, ?, ?)",
    }
    connection = _connect_registry(registry_path)
    try:
        connection.execute(statements[table], values)
        connection.commit()
    finally:
        connection.close()


def _build_sbm(
    prepared_payload: dict[str, object],
    obstacle_summary: dict[str, object],
    artifact_root: Path,
    registry_path: Path,
) -> dict[str, Any]:
    building = next(
        item for item in prepared_payload["drawings"]
        if item["discipline"] == "building"
    )
    source = Path(str(building["dxf"])).resolve()
    floors = Path(str(obstacle_summary["effective_planning_sheets"])).resolve()
    obstacles = Path(str(obstacle_summary["obstacles_geojson"])).resolve()
    obstacle_result = Path(str(obstacle_summary["reference_obstacle_result"])).resolve()
    identity = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": SBM_ALGORITHM_VERSION,
        "building_source_sha256": file_sha256(source),
        "floors_sha256": file_sha256(floors),
        "obstacles_sha256": file_sha256(obstacles),
        "obstacle_result_sha256": file_sha256(obstacle_result),
        "backend": str(obstacle_summary.get("backend") or ""),
        "scope": "building_drawing_only",
    }
    version = _digest(identity, "sbm")
    root = artifact_root / "sbm" / version
    manifest_path = root / "sbm_manifest.json"
    cache_hit = manifest_path.is_file()
    vector_dir = root / "vector"
    copied_floors = _copy_if_missing(floors, vector_dir / "floors.json")
    copied_obstacles = _copy_if_missing(obstacles, vector_dir / "building_obstacles.geojson")
    copied_result = _copy_if_missing(obstacle_result, vector_dir / "obstacle_result.json")
    per_floor = _write_per_floor_obstacles(copied_obstacles, vector_dir / "per_floor_union")
    review_dxf = ""
    annotated = str(obstacle_summary.get("annotated_dxf") or "").strip()
    if annotated and Path(annotated).is_file():
        review_dxf = str(
            _copy_if_missing(Path(annotated), root / "dxf" / "building_obstacles_review.dxf")
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "spatial_building_model",
        "sbm_version": version,
        "created_at": _now(),
        "authority": {
            "navigation": "vector/building_obstacles.geojson",
            "collision_detection": "vector/building_obstacles.geojson",
            "floor_scopes": "vector/floors.json",
            "cad_review_only": "dxf/building_obstacles_review.dxf",
            "images": "display_only_never_used_for_computation",
        },
        "identity": identity,
        "source_building": _file_record(source),
        "outputs": {
            "floors": str(copied_floors),
            "building_obstacles": str(copied_obstacles),
            "obstacle_result": str(copied_result),
            "per_floor_unions": per_floor,
            "review_dxf": review_dxf,
        },
    }
    if not cache_hit:
        write_json(manifest_path, manifest)
    stored = read_json(manifest_path)
    _register(
        registry_path,
        "sbm_versions",
        (
            version,
            str(stored.get("created_at") or _now()),
            str(manifest_path.resolve()),
            identity["building_source_sha256"],
            json.dumps(identity, ensure_ascii=False, sort_keys=True),
        ),
    )
    return {
        "version": version,
        "manifest": str(manifest_path.resolve()),
        "cache_hit": cache_hit,
        "outputs": stored["outputs"],
    }


def _build_object_set(
    *,
    sbm_version: str,
    prepared_payload: dict[str, object],
    route_targets_path: Path,
    registrations_path: Path,
    scope_policy_path: Path,
    recognition_rule_paths: list[Path],
    artifact_root: Path,
    registry_path: Path,
) -> dict[str, Any]:
    targets = read_json(route_targets_path)
    if not isinstance(targets, list):
        raise ValueError("全专业巡检目标必须是JSON数组")
    by_discipline: dict[str, list[dict[str, Any]]] = {
        discipline: [] for discipline in ("building", "water", "electrical", "hvac")
    }
    for row in targets:
        if not isinstance(row, dict):
            continue
        discipline = str(row.get("discipline") or "")
        if discipline in by_discipline:
            by_discipline[discipline].append(row)
    source_files = [
        _file_record(Path(str(item["dxf"])))
        for item in prepared_payload.get("drawings", [])
        if isinstance(item, dict) and item.get("dxf")
    ]
    recognition_rules = [
        _file_record(path)
        for path in sorted((Path(path).resolve() for path in recognition_rule_paths), key=str)
    ]
    identity = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": OBJECT_SET_ALGORITHM_VERSION,
        "sbm_version": sbm_version,
        "objects_sha256": hashlib.sha256(_json_bytes(targets)).hexdigest(),
        "source_file_hashes": [row["sha256"] for row in source_files],
        "registration_sha256": file_sha256(registrations_path),
        "scope_policy_sha256": file_sha256(scope_policy_path),
        "recognition_rule_hashes": [row["sha256"] for row in recognition_rules],
    }
    version = _digest(identity, "object_set")
    root = artifact_root / "object_sets" / version
    manifest_path = root / "object_set_manifest.json"
    cache_hit = manifest_path.is_file()
    output_paths: dict[str, str] = {}
    for discipline, rows in by_discipline.items():
        path = root / f"{discipline}_objects.json"
        if not path.is_file():
            write_json(path, rows)
        output_paths[discipline] = str(path.resolve())
    merged_path = root / "merged_objects.json"
    if not merged_path.is_file():
        write_json(merged_path, targets)
    output_paths["merged"] = str(merged_path.resolve())
    counts = {discipline: len(rows) for discipline, rows in by_discipline.items()}
    category_counts = Counter(
        (str(row.get("discipline") or ""), str(row.get("target_class") or row.get("category") or ""))
        for row in targets if isinstance(row, dict)
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "multi_discipline_inspection_object_set",
        "object_set_version": version,
        "sbm_version": sbm_version,
        "created_at": _now(),
        "coordinate_space": "building_sbm",
        "discipline_isolation_policy": str(scope_policy_path.resolve()),
        "recognition_rules": recognition_rules,
        "identity": identity,
        "source_files": source_files,
        "counts": {**counts, "total": len(targets)},
        "counts_by_discipline_class": [
            {"discipline": key[0], "category": key[1], "count": value}
            for key, value in sorted(category_counts.items())
        ],
        "outputs": output_paths,
    }
    if not cache_hit:
        write_json(manifest_path, manifest)
    stored = read_json(manifest_path)
    _register(
        registry_path,
        "object_set_versions",
        (
            version,
            str(stored.get("created_at") or _now()),
            str(manifest_path.resolve()),
            sbm_version,
            len(targets),
            json.dumps(identity, ensure_ascii=False, sort_keys=True),
        ),
    )
    return {
        "version": version,
        "manifest": str(manifest_path.resolve()),
        "cache_hit": cache_hit,
        "counts": stored["counts"],
        "outputs": stored["outputs"],
    }


def run_stage(
    *,
    prepared_payload: dict[str, object],
    obstacle_summary: dict[str, object],
    route_targets_path: Path,
    registrations_path: Path,
    scope_policy_path: Path,
    stage_dir: Path,
    artifact_root: Path,
    recognition_rule_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Materialize the immutable SBM, Object Set and environment tuple."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    registry_path = artifact_root / "version_registry.sqlite"
    sbm = _build_sbm(prepared_payload, obstacle_summary, artifact_root, registry_path)
    object_set = _build_object_set(
        sbm_version=sbm["version"],
        prepared_payload=prepared_payload,
        route_targets_path=route_targets_path,
        registrations_path=registrations_path,
        scope_policy_path=scope_policy_path,
        recognition_rule_paths=list(recognition_rule_paths or []),
        artifact_root=artifact_root,
        registry_path=registry_path,
    )
    identity = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ENVIRONMENT_ALGORITHM_VERSION,
        "sbm_version": sbm["version"],
        "object_set_version": object_set["version"],
    }
    version = _digest(identity, "environment")
    environment_dir = artifact_root / "environments" / version
    manifest_path = environment_dir / "environment_snapshot.json"
    cache_hit = manifest_path.is_file()
    environment = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "inspection_environment_snapshot",
        "environment_version": version,
        "created_at": _now(),
        "sbm_version": sbm["version"],
        "object_set_version": object_set["version"],
        "version_tuple": [sbm["version"], object_set["version"]],
        "sbm_manifest": sbm["manifest"],
        "object_set_manifest": object_set["manifest"],
    }
    if not cache_hit:
        write_json(manifest_path, environment)
    stored_environment = read_json(manifest_path)
    _register(
        registry_path,
        "environment_versions",
        (
            version,
            str(stored_environment.get("created_at") or _now()),
            str(manifest_path.resolve()),
            sbm["version"],
            object_set["version"],
            json.dumps(identity, ensure_ascii=False, sort_keys=True),
        ),
    )
    summary = {
        "stage": "06_versioned_environment",
        "schema_version": SCHEMA_VERSION,
        "sbm": sbm,
        "object_set": object_set,
        "environment": {
            "version": version,
            "version_tuple": [sbm["version"], object_set["version"]],
            "manifest": str(manifest_path.resolve()),
            "cache_hit": cache_hit,
        },
        "route": None,
        "registry": str(registry_path.resolve()),
    }
    write_json(stage_dir / "stage_summary.json", summary)
    write_json(stage_dir / "stage_manifest.json", {
        "schema_version": SCHEMA_VERSION,
        "stage": "06_versioned_environment",
        "algorithm_version": ENVIRONMENT_ALGORITHM_VERSION,
        "input_hash": hashlib.sha256(_json_bytes(identity)).hexdigest(),
        "status": "completed",
        "cache_hit": bool(sbm["cache_hit"] and object_set["cache_hit"] and cache_hit),
        "outputs": [sbm["manifest"], object_set["manifest"], str(manifest_path.resolve())],
    })
    return summary


def finalize_route_version(
    *,
    version_summary: dict[str, Any],
    route_summary: dict[str, Any],
    stage_dir: Path,
    artifact_root: Path,
    dataset_manifest_path: Path,
    rgcn_checkpoint_path: Path,
    route_head_checkpoint_path: Path,
) -> dict[str, Any]:
    """Bind a concrete route and its planner/model inputs to one environment."""
    environment_version = str(version_summary["environment"]["version"])
    route_output = str(route_summary.get("path_summary") or "").strip()
    route_output_record = _file_record(route_output) if route_output else {
        "path": "", "exists": False, "sha256": "", "size": 0
    }
    model_inputs = {
        "dataset_manifest": _file_record(dataset_manifest_path),
        "rgcn_checkpoint": _file_record(rgcn_checkpoint_path),
        "route_head_checkpoint": _file_record(route_head_checkpoint_path),
    }
    identity = {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": ROUTE_VERSION_ALGORITHM,
        "environment_version": environment_version,
        "route_output_sha256": route_output_record["sha256"],
        "model_hashes": {key: value["sha256"] for key, value in model_inputs.items()},
        "planner_parameters": {
            "constraints": str(route_summary.get("constraints") or ""),
            "route_start_anchor_policy": str(route_summary.get("route_start_anchor_policy") or ""),
            "continuous_infrastructure_included": bool(
                route_summary.get("continuous_infrastructure_included")
            ),
        },
    }
    version = _digest(identity, "route")
    root = artifact_root / "routes" / version
    manifest_path = root / "route_manifest.json"
    cache_hit = manifest_path.is_file()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "inspection_route_version",
        "route_version": version,
        "created_at": _now(),
        "environment_version": environment_version,
        "sbm_version": version_summary["sbm"]["version"],
        "object_set_version": version_summary["object_set"]["version"],
        "identity": identity,
        "model_inputs": model_inputs,
        "route_output": route_output_record,
        "outputs": {
            "route_dxf": str(route_summary.get("route_dxf") or ""),
            "acceptance_report": str(route_summary.get("acceptance_report") or ""),
            "acceptance_report_index": str(route_summary.get("acceptance_report_index") or ""),
            "final_route_raw_cad_audit": str(route_summary.get("final_route_raw_cad_audit") or ""),
        },
    }
    if not cache_hit:
        write_json(manifest_path, manifest)
    stored = read_json(manifest_path)
    registry_path = Path(version_summary["registry"])
    _register(
        registry_path,
        "route_versions",
        (
            version,
            str(stored.get("created_at") or _now()),
            str(manifest_path.resolve()),
            environment_version,
            json.dumps(identity, ensure_ascii=False, sort_keys=True),
        ),
    )
    route = {
        "version": version,
        "manifest": str(manifest_path.resolve()),
        "environment_version": environment_version,
        "cache_hit": cache_hit,
    }
    version_summary["route"] = route
    write_json(stage_dir / "stage_summary.json", version_summary)
    return route
