from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import ezdxf
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multi_drawing_pipeline.common import Detection, Registration, Sheet, entity_point, read_json, write_json  # noqa: E402
from multi_drawing_pipeline.stages.stage_04_register import (  # noqa: E402
    AXIS_GEOMETRY_LAYER_RE,
    PROFESSIONAL_LAYER_RE,
    _transform_point,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="渲染专业图与建筑图配准叠加审阅图")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--pair", action="append", required=True, help="专业图类别:楼层，例如 自动报警:F6")
    parser.add_argument(
        "--migrated-only", action="store_true",
        help="只绘制通过最终空间校验且已迁移的巡检对象",
    )
    return parser.parse_args()


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (Path(r"C:\Windows\Fonts\msyh.ttc"), Path(r"C:\Windows\Fonts\simhei.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _inside(sheet: Sheet, x: float, y: float) -> bool:
    return sheet.min_x <= x <= sheet.max_x and sheet.min_y <= y <= sheet.max_y


def _entity_segments(entity: object) -> list[list[tuple[float, float]]]:
    kind = entity.dxftype()
    try:
        if kind == "LINE":
            a, b = entity.dxf.start, entity.dxf.end
            return [[(float(a.x), float(a.y)), (float(b.x), float(b.y))]]
        if kind == "LWPOLYLINE":
            points = [(float(x), float(y)) for x, y in entity.get_points("xy")]
            if entity.closed and points:
                points.append(points[0])
            return [points] if len(points) >= 2 else []
        if kind in {"CIRCLE", "ARC"}:
            center = entity.dxf.center
            radius = float(entity.dxf.radius)
            start = 0.0 if kind == "CIRCLE" else math.radians(float(entity.dxf.start_angle))
            end = math.tau if kind == "CIRCLE" else math.radians(float(entity.dxf.end_angle))
            if end <= start:
                end += math.tau
            count = 24
            return [[
                (float(center.x) + radius * math.cos(start + (end - start) * index / count),
                 float(center.y) + radius * math.sin(start + (end - start) * index / count))
                for index in range(count + 1)
            ]]
    except Exception:
        return []
    return []


def _collect(doc: ezdxf.document.Drawing, sheet: Sheet) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    ordinary: list[list[tuple[float, float]]] = []
    axes: list[list[tuple[float, float]]] = []
    for entity in doc.modelspace():
        if entity.dxftype() not in {"LINE", "LWPOLYLINE", "CIRCLE", "ARC"}:
            continue
        point = entity_point(entity)
        if point is None or not _inside(sheet, *point):
            continue
        layer = str(entity.dxf.get("layer", ""))
        if PROFESSIONAL_LAYER_RE.search(layer):
            continue
        segments = _entity_segments(entity)
        if AXIS_GEOMETRY_LAYER_RE.search(layer):
            axes.extend(segments)
        else:
            ordinary.extend(segments)
    return ordinary, axes


def _mapped_segments(
    segments: list[list[tuple[float, float]]], registration: Registration,
) -> list[list[tuple[float, float]]]:
    transform = (
        registration.scale_x,
        math.radians(registration.rotation_deg),
        registration.translate_x,
        registration.translate_y,
    )
    return [[_transform_point(transform, x, y) for x, y in segment] for segment in segments]


def _render(
    path: Path,
    registration: Registration,
    target_sheet: Sheet,
    target_geometry: tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]],
    source_geometry: tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]],
    detections: list[Detection],
) -> dict[str, object]:
    width, height, banner = 1800, 1200, 100
    image = Image.new("RGB", (width, height), "#182128")
    draw = ImageDraw.Draw(image)
    margin = 35
    xmin, ymin, xmax, ymax = target_sheet.min_x, target_sheet.min_y, target_sheet.max_x, target_sheet.max_y
    span_x, span_y = max(xmax - xmin, 1.0), max(ymax - ymin, 1.0)
    scale = min((width - 2 * margin) / span_x, (height - banner - 2 * margin) / span_y)
    left = (width - span_x * scale) / 2.0
    top = banner + (height - banner - span_y * scale) / 2.0

    def pixel(point: tuple[float, float]) -> tuple[int, int]:
        return int(left + (point[0] - xmin) * scale), int(top + (ymax - point[1]) * scale)

    def lines(values: list[list[tuple[float, float]]], color: str, line_width: int) -> None:
        for value in values:
            if len(value) >= 2:
                draw.line([pixel(point) for point in value], fill=color, width=line_width)

    target_ordinary, target_axes = target_geometry
    source_ordinary, source_axes = source_geometry
    lines(target_ordinary, "#89939a", 1)
    lines(target_axes, "#ffd54a", 2)
    lines(source_ordinary, "#00d9ff", 1)
    lines(source_axes, "#ff4fd8", 2)

    transform = (
        registration.scale_x,
        math.radians(registration.rotation_deg),
        registration.translate_x,
        registration.translate_y,
    )
    mapped_detection_count = 0
    for item in detections:
        x, y = _transform_point(transform, item.x, item.y)
        if not _inside(target_sheet, x, y):
            continue
        px, py = pixel((x, y))
        draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill="#ff3b30", outline="#ffffff")
        mapped_detection_count += 1

    title_font, text_font = _font(26), _font(18)
    title = f"{registration.source_kind} {registration.floor} 配准叠加审阅"
    detail = (
        f"比例={registration.scale_x:.6f} 旋转={registration.rotation_deg:.3f}° "
        f"平移=({registration.translate_x:.1f}, {registration.translate_y:.1f}) "
        f"轴网匹配={registration.axis_matches} 中位残差={registration.median_residual:.1f} P95={registration.p95_residual:.1f}"
    )
    draw.text((25, 12), title, fill="white", font=title_font)
    draw.text((25, 51), detail, fill="#d9e1e6", font=text_font)
    draw.text(
        (width - 700, 15),
        "灰=建筑线  青=迁移后专业底图线  黄=建筑轴网  紫=专业轴网  红=巡检对象",
        fill="#d9e1e6", font=text_font,
    )
    image.save(path)
    return {
        "image": str(path.resolve()),
        "source_kind": registration.source_kind,
        "floor": registration.floor,
        "target_lines": len(target_ordinary),
        "target_axis_lines": len(target_axes),
        "source_lines": len(source_ordinary),
        "source_axis_lines": len(source_axes),
        "mapped_detections": mapped_detection_count,
        "scale": registration.scale_x,
        "rotation_deg": registration.rotation_deg,
        "translate_x": registration.translate_x,
        "translate_y": registration.translate_y,
        "axis_matches": registration.axis_matches,
        "median_residual": registration.median_residual,
        "p95_residual": registration.p95_residual,
    }


def main() -> int:
    args = parse_args()
    run = args.run.resolve()
    sheets = [Sheet(**item) for item in read_json(run / "02_sheets" / "sheets.json")]
    registrations = [Registration(**item) for item in read_json(run / "04_registration" / "registrations.json")]
    detections = [Detection(**item) for item in read_json(run / "03_recognition" / "recognized_objects.json")]
    migrated_ids: set[str] | None = None
    migration_audit = run / "05_migration" / "migration_audit.json"
    if args.migrated_only and migration_audit.exists():
        migrated_ids = {
            str(item["detection_id"])
            for item in read_json(migration_audit)
            if bool(item.get("migrated"))
        }
    output = run / "08_alignment_review"
    output.mkdir(parents=True, exist_ok=True)

    docs: dict[str, ezdxf.document.Drawing] = {}
    geometry: dict[tuple[str, str], tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]] = {}
    rows: list[dict[str, object]] = []
    for pair in args.pair:
        kind, floor = pair.rsplit(":", 1)
        registration = next(item for item in registrations if item.source_kind == kind and item.floor == floor)
        source_sheet = next(item for item in sheets if item.sheet_id == registration.source_sheet_id)
        target_sheet = next(item for item in sheets if item.sheet_id == registration.target_sheet_id)
        for sheet in (source_sheet, target_sheet):
            if sheet.drawing not in docs:
                docs[sheet.drawing] = ezdxf.readfile(sheet.drawing)
            key = (sheet.drawing, sheet.sheet_id)
            if key not in geometry:
                geometry[key] = _collect(docs[sheet.drawing], sheet)
        source_geometry = geometry[(source_sheet.drawing, source_sheet.sheet_id)]
        mapped_source_geometry = (
            _mapped_segments(source_geometry[0], registration),
            _mapped_segments(source_geometry[1], registration),
        )
        selected_detections = [
            item for item in detections
            if item.sheet_id == source_sheet.sheet_id
            and (migrated_ids is None or item.detection_id in migrated_ids)
        ]
        path = output / f"配准叠加_{kind}_{floor}.png"
        rows.append(_render(
            path, registration, target_sheet,
            geometry[(target_sheet.drawing, target_sheet.sheet_id)],
            mapped_source_geometry, selected_detections,
        ))
    write_json(output / "review_summary.json", rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
