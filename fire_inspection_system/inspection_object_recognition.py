from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_region_inspection_inventory import normalize_value, run_pipeline  # type: ignore
from mark_inspection_objects_dxf import (  # type: ignore
    bbox_from_inventory,
    dedupe_annotations,
    timestamped_output_path,
    write_marked_dxf,
)

DEFAULT_LLM_SCRIPT = PROJECT_ROOT / "scripts" / "llm-deepseekv4.py"


@dataclass(frozen=True)
class InspectionRecognitionResult:
    result_json: Path
    regions_manifest: Path
    marked_dxf: Path | None
    marked_report_json: Path | None
    region_count: int
    inspection_type_count: int
    inspection_instance_count: int
    llm_candidate_count: int
    llm_model: str


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def candidate_term(row: dict[str, str]) -> tuple[str, str]:
    entity_type = str(row.get("entity_type", "") or "").upper()
    norm_text = str(row.get("norm_text", "") or "").strip()
    if norm_text and entity_type in {"TEXT", "MTEXT", "ATTRIB"}:
        return norm_text, "text"
    block = str(row.get("parent_block_name", "") or "").strip()
    if block and entity_type == "INSERT":
        return block, "block"
    return "", "none"


def decision_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        normalize_value(str(item.get("term", "") or "")),
        str(item.get("layer", "") or ""),
        str(item.get("parent_block_name", "") or ""),
        str(item.get("entity_type", "") or ""),
        str(item.get("geometry_kind", "") or ""),
    )


def inventory_row_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    term, _source_type = candidate_term(row)
    return (
        normalize_value(term),
        str(row.get("layer", "") or ""),
        str(row.get("parent_block_name", "") or ""),
        str(row.get("entity_type", "") or ""),
        str(row.get("geometry_kind", "") or ""),
    )


def collect_region_annotations(region_output_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Map region-level inspection decisions back to instance bboxes.

    The region pipeline groups candidates semantically. For DXF review we need
    physical positions, so this function joins each floor's decisions back to
    its `cad_semantic_inventory.csv` rows.
    """
    result_path = region_output_dir / "region_inspection_results.json"
    result = read_json(result_path)
    annotations: list[dict[str, Any]] = []
    matched_by_sheet: Counter[str] = Counter()
    decision_count_by_sheet: dict[str, int] = {}

    for floor in result.get("floors", []) or []:
        if not isinstance(floor, dict):
            continue
        sheet_id = str(floor.get("sheet_id", "") or "")
        floor_id = str(floor.get("floor_id", "") or "")
        floor_name = str(floor.get("floor_name", "") or floor_id)
        sheet_dir = region_output_dir / sheet_id
        payload_path = sheet_dir / "inspection_objects.json"
        inventory_path = sheet_dir / "cad_semantic_inventory.csv"
        if not payload_path.exists() or not inventory_path.exists():
            continue

        payload = read_json(payload_path)
        decision_map: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for decision in payload.get("decisions", []) or []:
            if not isinstance(decision, dict) or decision.get("role") != "inspection_object":
                continue
            key = decision_key(decision)
            if key[0]:
                decision_map[key] = decision
        decision_count_by_sheet[sheet_id] = len(decision_map)

        with inventory_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                decision = decision_map.get(inventory_row_key(row))
                if not decision:
                    continue
                bbox = bbox_from_inventory(row)
                if not bbox:
                    continue
                annotations.append(
                    {
                        "object_id": row.get("object_id", ""),
                        "handle": row.get("handle", ""),
                        "source": row.get("source", ""),
                        "class_name": str(
                            decision.get("display_class_name")
                            or decision.get("class_name")
                            or decision.get("term")
                            or "巡检对象"
                        ),
                        "standard_class_name": str(decision.get("class_name") or ""),
                        "reason": str(decision.get("reason") or "region_inspection_result"),
                        "confidence": float(decision.get("confidence") or 0.0),
                        "sheet_id": sheet_id,
                        "floor_id": floor_id,
                        "floor_name": floor_name,
                        "source_type": decision.get("source_type", ""),
                        "term": decision.get("term", ""),
                        "layer": row.get("layer", ""),
                        "entity_type": row.get("entity_type", ""),
                        "geometry_kind": row.get("geometry_kind", ""),
                        "parent_block_name": row.get("parent_block_name", ""),
                        "norm_text": row.get("norm_text", ""),
                        "raw_text": row.get("raw_text", ""),
                        "bbox": bbox,
                    }
                )
                matched_by_sheet[sheet_id] += 1

    return annotations, {
        "result_json": str(result_path.resolve()),
        "region_count": result.get("region_count", 0),
        "llm_model": result.get("llm_model", ""),
        "matched_by_sheet": dict(matched_by_sheet),
        "decision_count_by_sheet": decision_count_by_sheet,
    }


def write_inspection_review_dxf(
    input_dxf: Path | str,
    region_output_dir: Path | str,
    output_dxf: Path | str,
) -> tuple[Path, Path, dict[str, Any]]:
    input_path = Path(input_dxf).expanduser().resolve()
    region_dir = Path(region_output_dir).resolve()
    output_path = Path(output_dxf).resolve()

    annotations, summary = collect_region_annotations(region_dir)
    deduped = dedupe_annotations(annotations)
    if not deduped:
        raise RuntimeError("没有找到可标注的区域级巡检对象实例。")

    try:
        write_marked_dxf(input_path, output_path, deduped)
    except PermissionError:
        output_path = timestamped_output_path(output_path)
        write_marked_dxf(input_path, output_path, deduped)

    class_counts = Counter(str(item["class_name"]) for item in deduped)
    floor_counts = Counter(str(item.get("floor_name") or item.get("floor_id") or "") for item in deduped)
    report = {
        "input_dxf": str(input_path),
        "output_dxf": str(output_path),
        "source": "region_inspection_results + per-sheet inspection_objects.json",
        "total_annotations_before_dedupe": len(annotations),
        "total_annotations": len(deduped),
        "deduped_annotations": len(annotations) - len(deduped),
        "class_counts": dict(class_counts.most_common()),
        "floor_counts": dict(floor_counts.most_common()),
        **summary,
        "sample_annotations": deduped[:30],
    }
    report_path = output_path.with_name("region_inspection_marked_report.json")
    write_json(report_path, report)
    return output_path, report_path, report


def recognize_inspection_objects(
    input_dxf: Path | str,
    inventory_dir: Path | str,
    sheets_json: Path | str,
    output_dir: Path | str,
    *,
    llm_script: Path | str = DEFAULT_LLM_SCRIPT,
    no_llm: bool = False,
    write_review_dxf: bool = True,
) -> InspectionRecognitionResult:

    input_path = Path(input_dxf).expanduser().resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    result = run_pipeline(
        Path(inventory_dir).resolve(),
        Path(sheets_json).resolve(),
        out_dir,
        Path(llm_script).resolve(),
        no_llm=no_llm,
    )

    marked_dxf: Path | None = None
    marked_report_json: Path | None = None
    if write_review_dxf:
        review_dir = out_dir.parent / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        marked_dxf, marked_report_json, _report = write_inspection_review_dxf(
            input_path,
            out_dir,
            review_dir / f"{input_path.stem}_inspection_marked.dxf",
        )

    catalog_rows = result.get("catalog_rows", []) if isinstance(result.get("catalog_rows"), list) else []
    return InspectionRecognitionResult(
        result_json=Path(result["artifacts"]["result_json"]),
        regions_manifest=out_dir / "regions_manifest.json",
        marked_dxf=marked_dxf,
        marked_report_json=marked_report_json,
        region_count=int(result.get("region_count") or 0),
        inspection_type_count=len(catalog_rows),
        inspection_instance_count=sum(int(row.get("count") or 0) for row in catalog_rows if isinstance(row, dict)),
        llm_candidate_count=int(result.get("llm_candidate_count") or 0),
        llm_model=str(result.get("llm_model") or ""),
    )
