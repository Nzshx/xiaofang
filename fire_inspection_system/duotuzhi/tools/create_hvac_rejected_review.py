from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import ezdxf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multi_drawing_pipeline.common import (  # noqa: E402
    Registration,
    Sheet,
    add_marker,
    ensure_layer,
    read_json,
    write_csv,
    write_json,
)


REASON_RULES = (
    ("占用区包络", "R1", "建筑楼层占用区外", 1),
    ("实体空间范围之外", "R2", "建筑实体范围外", 6),
    ("图框之外", "R3", "目标楼层图框外", 30),
    ("房间拓扑", "R4", "房间拓扑保护拒绝", 2),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="在原暖通图和建筑底图中标出全部暖通迁移拒绝对象")
    parser.add_argument("--run", type=Path, required=True, help="已完成第5阶段的运行目录")
    return parser.parse_args()


def _reason_code(reason: str) -> tuple[str, str, int]:
    for keyword, code, short_name, color in REASON_RULES:
        if keyword in reason:
            return code, short_name, color
    return "R9", "其他拒绝原因", 1


def _sheet_style(sheet: Sheet) -> tuple[float, float]:
    diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
    radius = max(diagonal * 0.00115, 1.0)
    text_height = max(diagonal * 0.00038, 1.0)
    return radius, text_height


def _set_view(doc: ezdxf.document.Drawing, points: list[tuple[float, float]]) -> None:
    if not points:
        return
    min_x = min(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_x = max(point[0] for point in points)
    max_y = max(point[1] for point in points)
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    margin = max(width, height) * 0.04
    view_height = max(height + margin * 2.0, (width + margin * 2.0) / (16.0 / 9.0))
    doc.set_modelspace_vport(
        view_height,
        center=((min_x + max_x) / 2.0, (min_y + max_y) / 2.0),
        dxfattribs={"aspect_ratio": 16.0 / 9.0},
    )
    doc.header["$TILEMODE"] = 1


def _add_legend(
    doc: ezdxf.document.Drawing,
    points: list[tuple[float, float]],
    text_height: float,
    title: str,
) -> None:
    if not points:
        return
    ensure_layer(doc, "暖通拒绝_图例", 7, 25)
    x = min(point[0] for point in points)
    y = max(point[1] for point in points) + text_height * 8.0
    lines = [
        title,
        "R1=建筑楼层占用区外  R2=建筑实体范围外  R3=目标楼层图框外  R9=其他",
        "红/紫/橙框仅用于拒绝对象审批；正式融合图中未迁移这些对象",
    ]
    for index, value in enumerate(lines):
        doc.modelspace().add_text(
            value,
            height=text_height * (1.15 if index == 0 else 0.82),
            dxfattribs={"layer": "暖通拒绝_图例", "color": 7},
        ).set_placement((x, y - index * text_height * 1.6))


def _mark(
    doc: ezdxf.document.Drawing,
    row: dict[str, Any],
    x: float,
    y: float,
    sheet: Sheet,
) -> tuple[str, str]:
    code, short_reason, color = _reason_code(str(row.get("reason", "")))
    radius, text_height = _sheet_style(sheet)
    add_marker(
        doc,
        x,
        y,
        f"{row['detection_id']} {row['category']} {code}",
        layer_prefix=f"暖通拒绝_{code}",
        color=color,
        radius=radius,
        text_height=text_height,
        draw_label=True,
    )
    return code, short_reason


def generate(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    sheets = [Sheet(**row) for row in read_json(run_dir / "02_sheets" / "sheets.json")]
    sheet_by_id = {sheet.sheet_id: sheet for sheet in sheets}
    registrations = [
        Registration(**row)
        for row in read_json(run_dir / "04_registration" / "registrations.json")
    ]
    target_sheet_by_source = {
        registration.source_sheet_id: sheet_by_id[registration.target_sheet_id]
        for registration in registrations
        if registration.target_sheet_id in sheet_by_id
    }
    rejected = [
        row
        for row in read_json(run_dir / "05_migration" / "migration_audit.json")
        if row.get("discipline") == "hvac" and not bool(row.get("migrated"))
    ]
    if not rejected:
        raise RuntimeError("本次运行没有暖通迁移拒绝对象")

    output_dir = run_dir / "07_hvac_rejected_review"
    output_dir.mkdir(parents=True, exist_ok=True)

    source_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rejected:
        source_groups[str(row["source_drawing"])].append(row)

    source_outputs: list[str] = []
    detail_rows: list[dict[str, Any]] = []
    for source_name, rows in source_groups.items():
        source_path = Path(source_name)
        doc = ezdxf.readfile(source_path)
        points: list[tuple[float, float]] = []
        heights: list[float] = []
        for row in rows:
            sheet = sheet_by_id[str(row["source_sheet_id"])]
            x, y = float(row["source_x"]), float(row["source_y"])
            code, short_reason = _mark(doc, row, x, y, sheet)
            points.append((x, y))
            heights.append(_sheet_style(sheet)[1])
            detail_rows.append({
                "detection_id": row["detection_id"],
                "floor": row["floor"],
                "category": row["category"],
                "reason_code": code,
                "short_reason": short_reason,
                "full_reason": row["reason"],
                "source_drawing": source_name,
                "source_handle": row["source_handle"],
                "source_layer": row["source_layer"],
                "source_x": row["source_x"],
                "source_y": row["source_y"],
                "target_x": row["target_x"],
                "target_y": row["target_y"],
            })
        _add_legend(doc, points, sorted(heights)[len(heights) // 2], "原暖通图：全部迁移拒绝对象")
        _set_view(doc, points)
        output = output_dir / f"01_原暖通图_拒绝对象标注_{source_path.stem}.dxf"
        doc.audit()
        doc.saveas(output)
        source_outputs.append(str(output.resolve()))

    building = next(sheet for sheet in sheets if sheet.discipline == "building")
    target_doc = ezdxf.readfile(building.drawing)
    target_points: list[tuple[float, float]] = []
    target_heights: list[float] = []
    for row in rejected:
        target_sheet = target_sheet_by_source.get(str(row["source_sheet_id"]))
        if target_sheet is None:
            continue
        x, y = float(row["target_x"]), float(row["target_y"])
        _mark(target_doc, row, x, y, target_sheet)
        target_points.append((x, y))
        target_heights.append(_sheet_style(target_sheet)[1])
    _add_legend(
        target_doc,
        target_points,
        sorted(target_heights)[len(target_heights) // 2],
        "建筑底图：暖通拒绝对象的拟迁移位置（仅审批，不代表已迁移）",
    )
    _set_view(target_doc, target_points)
    target_output = output_dir / "02_建筑底图_暖通拒绝位置标注.dxf"
    target_doc.audit()
    target_doc.saveas(target_output)

    write_csv(output_dir / "暖通拒绝对象明细.csv", detail_rows)
    write_json(output_dir / "暖通拒绝对象明细.json", detail_rows)
    category_counts = Counter(str(row["category"]) for row in rejected)
    reason_counts = Counter(_reason_code(str(row["reason"]))[0] for row in rejected)
    floor_counts = Counter(str(row["floor"]) for row in rejected)
    summary = {
        "rejected_count": len(rejected),
        "category_counts": dict(category_counts),
        "reason_code_counts": dict(reason_counts),
        "floor_counts": dict(floor_counts),
        "source_annotated_dxf": source_outputs,
        "target_location_annotated_dxf": str(target_output.resolve()),
        "detail_csv": str((output_dir / "暖通拒绝对象明细.csv").resolve()),
        "notice": f"目标图仅标出拟迁移位置；{len(rejected)}个对象仍未写入正式融合图。",
    }
    write_json(output_dir / "stage_summary.json", summary)
    return summary


def main() -> int:
    result = generate(parse_args().run)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
