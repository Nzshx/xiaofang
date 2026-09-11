from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import ezdxf
from ezdxf.addons.importer import Importer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multi_drawing_pipeline.common import (  # noqa: E402
    Detection,
    Registration,
    Sheet,
    add_marker,
    ensure_layer,
    read_json,
    write_csv,
    write_json,
)
from multi_drawing_pipeline.stages.stage_05_migrate import (  # noqa: E402
    XDATA_APPID,
    _import_entity,
    _mapped_point,
    _transform_matrix,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成拒绝配准对象的迁移前/假设迁移后对照DXF")
    parser.add_argument("--run", type=Path, required=True, help="已完成到第5阶段的运行目录")
    parser.add_argument("--kind", default="自动报警", help="专业分图类别，例如自动报警、给排水")
    parser.add_argument("--floor", default="F3", help="楼层标准编号，例如F3、ROOF")
    parser.add_argument("--count", type=int, default=8, help="抽取的拒绝设备块数量")
    return parser.parse_args()


def _choose_cluster(items: list[Detection], sheet: Sheet, count: int) -> list[Detection]:
    if not items:
        return []
    diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
    radius = diagonal * 0.055
    seed = max(
        items,
        key=lambda item: sum(math.hypot(other.x - item.x, other.y - item.y) <= radius for other in items),
    )
    ordered = sorted(items, key=lambda item: math.hypot(item.x - seed.x, item.y - seed.y))
    # 仅在稠密局部内兼顾类别，避免为了凑齐类别选到图纸另一端，导致样例无法查看。
    local_pool = ordered[:max(count * 6, count)]
    selected: list[Detection] = []
    seen_categories: set[str] = set()
    for item in local_pool:
        if item.category not in seen_categories:
            selected.append(item)
            seen_categories.add(item.category)
        if len(selected) >= count:
            return selected
    for item in local_pool:
        if item not in selected:
            selected.append(item)
        if len(selected) >= count:
            break
    return selected


def _set_view(doc: ezdxf.document.Drawing, points: list[tuple[float, float]], text_height: float) -> None:
    min_x = min(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_x = max(point[0] for point in points)
    max_y = max(point[1] for point in points)
    width = max(max_x - min_x, text_height * 45.0)
    height = max(max_y - min_y, text_height * 28.0)
    margin = max(width, height) * 0.22
    min_x -= margin
    max_x += margin
    min_y -= margin
    max_y += margin
    aspect_ratio = 16.0 / 9.0
    view_height = max(max_y - min_y, (max_x - min_x) / aspect_ratio)
    doc.set_modelspace_vport(
        view_height,
        center=((min_x + max_x) / 2.0, (min_y + max_y) / 2.0),
        dxfattribs={"aspect_ratio": aspect_ratio},
    )
    doc.header["$TILEMODE"] = 1


def _label_setup(sheet: Sheet) -> tuple[float, float]:
    diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
    text_height = max(diagonal * 0.00055, 1e-6)
    return text_height, text_height * 3.1


def _add_banner(doc: ezdxf.document.Drawing, text: str, x: float, y: float, height: float) -> None:
    ensure_layer(doc, "拒绝示例_说明", 1, 50)
    entity = doc.modelspace().add_text(
        text,
        height=height * 1.15,
        dxfattribs={"layer": "拒绝示例_说明", "color": 1},
    )
    entity.set_placement((x, y))


def generate(run_dir: Path, source_kind: str, floor: str, count: int) -> dict[str, object]:
    run_dir = run_dir.resolve()
    sheets = [Sheet(**row) for row in read_json(run_dir / "02_sheets" / "sheets.json")]
    detections = [Detection(**row) for row in read_json(run_dir / "03_recognition" / "recognized_objects.json")]
    registrations = [Registration(**row) for row in read_json(run_dir / "04_registration" / "registrations.json")]
    registration = next(
        item for item in registrations
        if item.source_kind == source_kind and item.floor == floor and not item.accepted
    )
    source_sheet = next(item for item in sheets if item.sheet_id == registration.source_sheet_id)
    target_sheet = next(item for item in sheets if item.sheet_id == registration.target_sheet_id)
    candidates = [
        item for item in detections
        if item.sheet_id == source_sheet.sheet_id and item.entity_type == "INSERT"
    ]
    selected = _choose_cluster(candidates, source_sheet, count)
    if not selected:
        raise RuntimeError("指定的拒绝分图没有可展示的设备块")

    stage_dir = run_dir / "07_rejected_examples"
    stage_dir.mkdir(parents=True, exist_ok=True)
    text_height, marker_radius = _label_setup(source_sheet)

    source_doc = ezdxf.readfile(source_sheet.drawing)
    source_points = [(item.x, item.y) for item in selected]
    for item in selected:
        add_marker(
            source_doc, item.x, item.y, f"REJECT {item.detection_id} {item.category}",
            layer_prefix="拒绝示例_迁移前", color=1,
            radius=marker_radius, text_height=text_height, draw_label=True,
        )
    _add_banner(
        source_doc,
        f"迁移前拒绝对象示例 {source_kind} {floor}（仅标{len(selected)}个）",
        min(point[0] for point in source_points), max(point[1] for point in source_points) + marker_radius * 3.0,
        text_height,
    )
    _set_view(source_doc, source_points, text_height)
    before_path = stage_dir / f"01_{source_kind}_{floor}_迁移前_仅标拒绝对象.dxf"
    source_doc.saveas(before_path)

    target_doc = ezdxf.readfile(target_sheet.drawing)
    if XDATA_APPID not in target_doc.appids:
        target_doc.appids.add(XDATA_APPID)
    ensure_layer(target_doc, "拒绝示例_位移箭头", 1, 50)
    ensure_layer(target_doc, "拒绝示例_原坐标参考", 4, 35)
    source_import_doc = ezdxf.readfile(source_sheet.drawing)
    importer = Importer(source_import_doc, target_doc)
    source_entities = source_import_doc.entitydb
    preview_points: list[tuple[float, float]] = []
    rows: list[dict[str, object]] = []
    matrix = _transform_matrix(registration)
    for item in selected:
        mapped_x, mapped_y = _mapped_point(item, registration)
        source_entity = source_entities.get(item.handle)
        if source_entity is None:
            continue
        imported = _import_entity(importer, target_doc.modelspace(), source_entity, matrix, item)
        if imported is not None:
            imported.dxf.color = 1
        add_marker(
            target_doc, mapped_x, mapped_y, f"REJECT {item.detection_id} {item.category}",
            layer_prefix="拒绝示例_假设迁移位置", color=1,
            radius=marker_radius, text_height=text_height, draw_label=True,
        )
        target_doc.modelspace().add_circle(
            (item.x, item.y), marker_radius * 0.55,
            dxfattribs={"layer": "拒绝示例_原坐标参考", "color": 4},
        )
        target_doc.modelspace().add_line(
            (item.x, item.y), (mapped_x, mapped_y),
            dxfattribs={"layer": "拒绝示例_位移箭头", "color": 1},
        )
        preview_points.extend([(item.x, item.y), (mapped_x, mapped_y)])
        rows.append({
            "detection_id": item.detection_id,
            "category": item.category,
            "source_x": item.x, "source_y": item.y,
            "hypothetical_target_x": mapped_x, "hypothetical_target_y": mapped_y,
            "delta_x": mapped_x - item.x, "delta_y": mapped_y - item.y,
            "source_handle": item.handle, "source_layer": item.layer,
            "registration_reason": registration.reason,
        })
    importer.finalize()
    _add_banner(
        target_doc,
        f"拒绝变换假设预览：红=候选位置 青=专业图原坐标 正式结果未迁移",
        min(point[0] for point in preview_points), max(point[1] for point in preview_points) + marker_radius * 3.0,
        text_height,
    )
    _set_view(target_doc, preview_points, text_height)
    after_path = stage_dir / f"02_{source_kind}_{floor}_假设迁移后_仅标拒绝对象.dxf"
    target_doc.audit()
    target_doc.saveas(after_path)

    write_json(stage_dir / "拒绝对象示例明细.json", rows)
    write_csv(stage_dir / "拒绝对象示例明细.csv", rows)
    summary = {
        "source_kind": source_kind, "floor": floor,
        "selected_count": len(rows), "registration_reason": registration.reason,
        "before_dxf": str(before_path.resolve()), "after_preview_dxf": str(after_path.resolve()),
        "notice": "after_preview_dxf仅用于显示被拒绝变换，未写入正式融合结果",
    }
    write_json(stage_dir / "stage_summary.json", summary)
    return summary


def main() -> int:
    args = parse_args()
    result = generate(args.run, args.kind, args.floor, args.count)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
