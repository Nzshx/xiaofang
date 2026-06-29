# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import ezdxf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import web.server as review_server

JOBS_ROOT = PROJECT_ROOT / "web" / "runtime" / "jobs"
BOX_LAYER = "CHECK_INSPECTION_OBJECT_BOX"
TEXT_LAYER = "CHECK_INSPECTION_OBJECT_TEXT"
POINT_LAYER = "CHECK_INSPECTION_OBJECT_POINT"
EXCLUSION_LAYER_TERMS = [
    "\u4e0d\u51fa\u56fe\u8303\u56f4",  # 不出图范围
    "\u975e\u5de1\u68c0\u8303\u56f4",  # 非巡检范围
    "\u4e0d\u5de1\u68c0\u8303\u56f4",  # 不巡检范围
    "\u4e0d\u6807\u6ce8\u8303\u56f4",  # 不标注范围
]
EXCLUSION_NOTE_TERMS = [
    "\u9634\u5f71\u90e8\u5206\u8be6\u89c1\u5355\u4f53\u56fe\u7eb8",  # 阴影部分详见单体图纸
]


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def as_int(value: Any) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def safe_layer_suffix(text: Any) -> str:
    clean = str(text or "").strip()
    clean = re.sub(r'[<>/\\":;?*|=,`\[\]]+', "_", clean)
    return clean[:80] or "UNKNOWN"


def latest_job_dir() -> Path:
    jobs = [path for path in JOBS_ROOT.iterdir() if path.is_dir() and (path / "inventory").exists()]
    if not jobs:
        raise FileNotFoundError(f"No jobs found under {JOBS_ROOT}")
    return max(jobs, key=lambda item: item.stat().st_mtime)


def catalog_key_from_catalog(row: dict[str, str]) -> tuple[str, ...]:
    geometry_kind = str(row.get("geometry_kind", "") or "")
    text_key = str(row.get("norm_text_sample", "") or "") if geometry_kind == "text" else ""
    return tuple(str(row.get(key, "") or "") for key in [
        "layer",
        "entity_type",
        "geometry_kind",
        "color",
        "linetype",
        "is_closed",
        "parent_block_name",
    ]) + (text_key,)


def catalog_key_from_inventory(row: dict[str, str]) -> tuple[str, ...]:
    geometry_kind = str(row.get("geometry_kind", "") or "")
    text_key = str(row.get("norm_text", "") or "") if geometry_kind == "text" else ""
    return tuple(str(row.get(key, "") or "") for key in [
        "layer",
        "entity_type",
        "geometry_kind",
        "color",
        "linetype",
        "is_closed",
        "parent_block_name",
    ]) + (text_key,)


def pseudo_catalog_row_from_inventory(row: dict[str, str]) -> dict[str, str]:
    source = row.get("source", "")
    raw_text = row.get("raw_text", "")
    return {
        "count": "1",
        "layer": row.get("layer", ""),
        "entity_type": row.get("entity_type", ""),
        "geometry_kind": row.get("geometry_kind", ""),
        "color": row.get("color", ""),
        "linetype": row.get("linetype", ""),
        "is_closed": row.get("is_closed", ""),
        "parent_block_name": row.get("parent_block_name", ""),
        "norm_text_sample": row.get("norm_text", ""),
        "raw_text_sample": json.dumps([raw_text], ensure_ascii=False) if raw_text else "[]",
        "source_counter": json.dumps({source: 1}, ensure_ascii=False) if source else "{}",
        "bbox_minx": row.get("bbox_minx", ""),
        "bbox_miny": row.get("bbox_miny", ""),
        "bbox_maxx": row.get("bbox_maxx", ""),
        "bbox_maxy": row.get("bbox_maxy", ""),
    }


def bbox_from_inventory(row: dict[str, str]) -> tuple[float, float, float, float] | None:
    minx = safe_float(row.get("bbox_minx"))
    miny = safe_float(row.get("bbox_miny"))
    maxx = safe_float(row.get("bbox_maxx"))
    maxy = safe_float(row.get("bbox_maxy"))
    x = safe_float(row.get("x"))
    y = safe_float(row.get("y"))
    if None in (minx, miny, maxx, maxy) or maxx <= minx or maxy <= miny:
        if x is None or y is None:
            return None
        minx, miny, maxx, maxy = x - 250.0, y - 250.0, x + 250.0, y + 250.0

    width = maxx - minx
    height = maxy - miny
    pad = max(180.0, min(800.0, max(width, height) * 0.18))
    if width < 600.0:
        extra = (600.0 - width) / 2.0
        minx -= extra
        maxx += extra
    if height < 360.0:
        extra = (360.0 - height) / 2.0
        miny -= extra
        maxy += extra
    return minx - pad, miny - pad, maxx + pad, maxy + pad


def bbox_from_row(row: dict[str, str]) -> tuple[float, float, float, float] | None:
    minx = safe_float(row.get("bbox_minx"))
    miny = safe_float(row.get("bbox_miny"))
    maxx = safe_float(row.get("bbox_maxx"))
    maxy = safe_float(row.get("bbox_maxy"))
    if None in (minx, miny, maxx, maxy) or maxx <= minx or maxy <= miny:
        return None
    return minx, miny, maxx, maxy


def bbox_area(bbox: tuple[float, float, float, float]) -> float:
    minx, miny, maxx, maxy = bbox
    return max(0.0, maxx - minx) * max(0.0, maxy - miny)


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = bbox_area(a) + bbox_area(b) - intersection
    return intersection / union if union > 0 else 0.0


def center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    minx, miny, maxx, maxy = bbox
    return (minx + maxx) / 2.0, (miny + maxy) / 2.0


def center_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay = center(a)
    bx, by = center(b)
    return math.hypot(ax - bx, ay - by)


def bbox_contains_point(bbox: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    minx, miny, maxx, maxy = bbox
    x, y = point
    return minx <= x <= maxx and miny <= y <= maxy


def text_contains_any(value: Any, terms: list[str]) -> bool:
    text = str(value or "").upper()
    return any(str(term).upper() in text for term in terms)


def annotation_priority(item: dict[str, Any]) -> tuple[int, float]:
    entity_type = str(item.get("entity_type", "")).upper()
    geometry_kind = str(item.get("geometry_kind", "")).lower()
    source = str(item.get("source", ""))
    score = 0
    if entity_type == "INSERT" or geometry_kind == "block_insert":
        score += 300
    elif entity_type in {"TEXT", "MTEXT", "ATTRIB"} or geometry_kind == "text":
        score += 200
    if source == "direct_entity":
        score += 30
    elif source == "insert_container":
        score += 20
    elif source == "virtual_entity_in_insert":
        score -= 10
    if item.get("reason", "").startswith("inspection_library"):
        score += 10
    return score, -bbox_area(item["bbox"])


def is_duplicate_annotation(item: dict[str, Any], kept: dict[str, Any]) -> bool:
    if item["class_name"] != kept["class_name"]:
        return False
    if bbox_iou(item["bbox"], kept["bbox"]) >= 0.55:
        return True
    return center_distance(item["bbox"], kept["bbox"]) <= 700.0


def dedupe_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for item in sorted(annotations, key=annotation_priority, reverse=True):
        if any(is_duplicate_annotation(item, existing) for existing in kept):
            continue
        kept.append(item)
    return sorted(kept, key=lambda item: (item["class_name"], center(item["bbox"])[0], center(item["bbox"])[1]))


def collect_exclusion_regions(inventory_dir: Path, *, include_polylines: bool = False) -> list[dict[str, Any]]:
    """Collect CAD regions where review overlays should not be added.

    The main production signal is a HATCH on layers such as "不出图范围".
    Closed polylines are optional because drawings often use very large frame
    rectangles on these layers that are not actual masked areas.
    """
    catalog_path = inventory_dir / "cad_object_catalog.csv"
    if not catalog_path.exists():
        return []

    regions: list[dict[str, Any]] = []
    with catalog_path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            layer = str(row.get("layer", "") or "")
            text = str(row.get("norm_text_sample", "") or "")
            block = str(row.get("parent_block_name", "") or "")
            combined = " ".join([layer, text, block])
            if not (
                text_contains_any(combined, EXCLUSION_LAYER_TERMS)
                or text_contains_any(text, EXCLUSION_NOTE_TERMS)
            ):
                continue

            entity_type = str(row.get("entity_type", "") or "").upper()
            geometry_kind = str(row.get("geometry_kind", "") or "").lower()
            is_region_entity = entity_type == "HATCH" or geometry_kind == "hatch_area"
            if include_polylines:
                is_region_entity = is_region_entity or geometry_kind == "polyline_closed"
            if not is_region_entity:
                continue

            bbox = bbox_from_row(row)
            if not bbox or bbox_area(bbox) < 1_000_000:
                continue
            regions.append(
                {
                    "signature_id": row.get("signature_id", ""),
                    "layer": layer,
                    "entity_type": entity_type,
                    "geometry_kind": geometry_kind,
                    "bbox": bbox,
                    "reason": "auto_exclusion_region",
                    "source_text": text,
                }
            )
    return regions


def filter_annotations_by_exclusion_regions(
    annotations: list[dict[str, Any]],
    regions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not regions:
        return annotations, []
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for item in annotations:
        point = center(item["bbox"])
        matched_region = next(
            (region for region in regions if bbox_contains_point(region["bbox"], point)),
            None,
        )
        if matched_region:
            copy = dict(item)
            copy["excluded_by_region"] = {
                "signature_id": matched_region.get("signature_id", ""),
                "layer": matched_region.get("layer", ""),
                "bbox": matched_region.get("bbox", ()),
            }
            excluded.append(copy)
            continue
        kept.append(item)
    return kept, excluded


def add_layer(doc: Any, name: str, color: int) -> None:
    if name not in doc.layers:
        doc.layers.add(name=name, color=color)
        return
    try:
        doc.layers.get(name).dxf.color = color
    except Exception:
        pass


def add_label(msp: Any, text: str, point: tuple[float, float], height: float, layer: str, color: int) -> None:
    entity = msp.add_text(text, dxfattribs={"layer": layer, "color": color, "height": height})
    try:
        entity.set_placement(point)
    except Exception:
        entity.dxf.insert = point


def find_input_dxf(job_dir: Path) -> Path:
    upload_dir = job_dir / "upload"
    candidates = sorted(upload_dir.glob("*.dxf"))
    if not candidates:
        raise FileNotFoundError(f"No DXF found in {upload_dir}")
    return candidates[0]


def collect_inspection_annotations(inventory_dir: Path) -> tuple[list[dict[str, Any]], int]:
    classified = review_server.read_json(review_server.classified_result_path(inventory_dir), fallback={}) or {}
    decisions = review_server.classified_text_decision_map(classified)
    inspection_keys: dict[tuple[str, ...], dict[str, str]] = {}

    with (inventory_dir / "cad_object_catalog.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            role, name, reason = review_server.classify_catalog_semantics(row, decisions)
            if role != "inspection_object":
                continue
            raw_count = max(as_int(row.get("count")), 0)
            if review_server.inspection_instance_count(row, raw_count, name) <= 0:
                continue
            inspection_keys[catalog_key_from_catalog(row)] = {"class_name": name, "reason": reason}

    annotations: list[dict[str, Any]] = []
    with (inventory_dir / "cad_object_inventory.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            info = inspection_keys.get(catalog_key_from_inventory(row))
            if not info:
                continue
            pseudo = pseudo_catalog_row_from_inventory(row)
            if review_server.inspection_instance_count(pseudo, 1, info["class_name"]) <= 0:
                continue
            bbox = bbox_from_inventory(row)
            if not bbox:
                continue
            annotations.append({
                "object_id": row.get("object_id", ""),
                "handle": row.get("handle", ""),
                "source": row.get("source", ""),
                "class_name": info["class_name"],
                "reason": info["reason"],
                "layer": row.get("layer", ""),
                "entity_type": row.get("entity_type", ""),
                "geometry_kind": row.get("geometry_kind", ""),
                "parent_block_name": row.get("parent_block_name", ""),
                "norm_text": row.get("norm_text", ""),
                "raw_text": row.get("raw_text", ""),
                "bbox": bbox,
            })
    return annotations, len(inspection_keys)


def write_marked_dxf(input_dxf: Path, output_dxf: Path, annotations: list[dict[str, Any]]) -> None:
    doc = ezdxf.readfile(input_dxf)
    msp = doc.modelspace()
    add_layer(doc, BOX_LAYER, 1)
    add_layer(doc, TEXT_LAYER, 1)
    add_layer(doc, POINT_LAYER, 1)
    for class_name in sorted({item["class_name"] for item in annotations}):
        add_layer(doc, "CHECK_INSP_" + safe_layer_suffix(class_name), 1)

    for index, item in enumerate(annotations, start=1):
        minx, miny, maxx, maxy = item["bbox"]
        cx, cy = center(item["bbox"])
        width = maxx - minx
        height = maxy - miny
        marker_radius = max(120.0, min(500.0, max(width, height) * 0.08))
        text_height = max(260.0, min(700.0, max(width, height) * 0.12))
        class_layer = "CHECK_INSP_" + safe_layer_suffix(item["class_name"])
        polyline = msp.add_lwpolyline(
            [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)],
            close=True,
            dxfattribs={"layer": class_layer, "color": 1},
        )
        try:
            polyline.dxf.const_width = max(20.0, min(80.0, marker_radius * 0.12))
        except Exception:
            pass
        msp.add_circle((cx, cy), radius=marker_radius, dxfattribs={"layer": POINT_LAYER, "color": 1})
        add_label(
            msp,
            f"{index:03d} {item['class_name']}",
            (minx, maxy + text_height * 0.4),
            text_height,
            TEXT_LAYER,
            1,
        )
    output_dxf.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(output_dxf)


def timestamped_output_path(output_dxf: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return output_dxf.with_name(f"{output_dxf.stem}_{stamp}{output_dxf.suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Add inspection-object review overlays to a DXF file.")
    parser.add_argument("--job-id", default="", help="web/runtime/jobs job id. Defaults to latest job.")
    parser.add_argument("--input-dxf", default="", help="Override input DXF path.")
    parser.add_argument("--inventory-dir", default="", help="Override inventory directory.")
    parser.add_argument("--output", default="", help="Output marked DXF path.")
    parser.add_argument("--no-auto-exclusion", action="store_true", help="Do not filter annotations by CAD no-output/no-inspection regions.")
    parser.add_argument("--include-polyline-exclusions", action="store_true", help="Also treat closed polylines on exclusion layers as exclusion regions.")
    args = parser.parse_args()

    job_dir = JOBS_ROOT / args.job_id if args.job_id else latest_job_dir()
    inventory_dir = Path(args.inventory_dir) if args.inventory_dir else job_dir / "inventory"
    input_dxf = Path(args.input_dxf) if args.input_dxf else find_input_dxf(job_dir)
    output_dxf = Path(args.output) if args.output else job_dir / "review" / f"{input_dxf.stem}_inspection_marked.dxf"

    annotations, catalog_signature_count = collect_inspection_annotations(inventory_dir)
    exclusion_regions = [] if args.no_auto_exclusion else collect_exclusion_regions(
        inventory_dir,
        include_polylines=args.include_polyline_exclusions,
    )
    scoped_annotations, excluded_annotations = filter_annotations_by_exclusion_regions(annotations, exclusion_regions)
    deduped = dedupe_annotations(scoped_annotations)
    if not deduped:
        raise RuntimeError("No inspection annotations found.")

    try:
        write_marked_dxf(input_dxf, output_dxf, deduped)
    except PermissionError:
        locked_output = output_dxf
        output_dxf = timestamped_output_path(output_dxf)
        print(f"[WARN] Output DXF is locked, writing fallback file: {locked_output} -> {output_dxf}", file=sys.stderr)
        write_marked_dxf(input_dxf, output_dxf, deduped)

    report_json = output_dxf.with_name("inspection_marked_report.json")
    class_counts = Counter(item["class_name"] for item in deduped)
    report = {
        "job_id": job_dir.name,
        "input_dxf": str(input_dxf.resolve()),
        "output_dxf": str(output_dxf.resolve()),
        "annotation_layers": [BOX_LAYER, TEXT_LAYER, POINT_LAYER]
        + ["CHECK_INSP_" + safe_layer_suffix(name) for name in class_counts],
        "total_annotations_before_dedupe": len(annotations),
        "total_annotations_after_scope_filter": len(scoped_annotations),
        "total_annotations": len(deduped),
        "excluded_by_scope_regions": len(excluded_annotations),
        "deduped_annotations": len(scoped_annotations) - len(deduped),
        "class_counts": dict(class_counts.most_common()),
        "catalog_signature_count": catalog_signature_count,
        "exclusion_regions": [
            {
                "signature_id": region.get("signature_id", ""),
                "layer": region.get("layer", ""),
                "entity_type": region.get("entity_type", ""),
                "geometry_kind": region.get("geometry_kind", ""),
                "bbox": region.get("bbox", ()),
                "source_text": region.get("source_text", ""),
            }
            for region in exclusion_regions[:50]
        ],
        "sample_excluded_annotations": excluded_annotations[:30],
        "note": "Annotations are review overlays only. Original drawing entities are not modified except for adding CHECK_* layers/entities.",
        "sample_annotations": deduped[:30],
    }
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output_dxf": str(output_dxf.resolve()),
        "report_json": str(report_json.resolve()),
        "total_annotations_before_dedupe": len(annotations),
        "total_annotations_after_scope_filter": len(scoped_annotations),
        "total_annotations": len(deduped),
        "excluded_by_scope_regions": len(excluded_annotations),
        "deduped_annotations": len(scoped_annotations) - len(deduped),
        "exclusion_region_count": len(exclusion_regions),
        "class_counts": dict(class_counts.most_common()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
