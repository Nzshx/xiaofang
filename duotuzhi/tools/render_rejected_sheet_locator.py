from __future__ import annotations

import argparse
import json
from pathlib import Path

import ezdxf
from ezdxf import bbox
from ezdxf.addons.drawing import Frontend, RenderContext, layout
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.math import BoundingBox2d


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="渲染被拒绝专业分图在原CAD中的定位图")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--kind", default="给排水")
    parser.add_argument("--floor", default="EQUIPMENT")
    return parser.parse_args()


def _intersects(entity: object, limits: tuple[float, float, float, float]) -> bool:
    try:
        extents = bbox.extents([entity], fast=True)
    except Exception:
        return False
    if not extents.has_data:
        return False
    min_x, min_y, max_x, max_y = limits
    return not (
        extents.extmax.x < min_x
        or extents.extmin.x > max_x
        or extents.extmax.y < min_y
        or extents.extmin.y > max_y
    )


def _render(
    doc: ezdxf.document.Drawing,
    limits: tuple[float, float, float, float],
    sheets: list[dict[str, object]],
    output: Path,
    title: str,
) -> None:
    min_x, min_y, max_x, max_y = limits
    modelspace = doc.modelspace()
    marker_layer = "拒绝分图_定位"
    if marker_layer not in doc.layers:
        doc.layers.add(marker_layer, color=1)
    diagonal = max(((max_x - min_x) ** 2 + (max_y - min_y) ** 2) ** 0.5, 1.0)
    text_height = diagonal * 0.0055
    marker_entities: list[object] = []
    for index, sheet in enumerate(sheets, start=1):
        x0 = float(sheet["min_x"])
        y0 = float(sheet["min_y"])
        x1 = float(sheet["max_x"])
        y1 = float(sheet["max_y"])
        marker_entities.append(modelspace.add_lwpolyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
            close=True,
            dxfattribs={"layer": marker_layer, "color": 1, "lineweight": 80},
        ))
        label = modelspace.add_text(
            f"REJECTED CANDIDATE {index} [{sheet['sheet_id']}]",
            height=text_height,
            dxfattribs={"layer": marker_layer, "color": 1},
        )
        label.set_placement((x0, y1 + text_height * 0.6))
        marker_entities.append(label)

    context = RenderContext(doc)
    context.set_current_layout(modelspace)
    backend = SVGBackend()
    Frontend(context, backend).draw_layout(
        modelspace,
        finalize=True,
        filter_func=lambda entity: _intersects(entity, limits),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    page = layout.Page(420, 210, units=layout.Units.mm)
    render_box = BoundingBox2d([(min_x, min_y), (max_x, max_y)])
    svg = backend.get_string(
        page,
        settings=layout.Settings(fit_page=True, output_layers=True),
        render_box=render_box,
    )
    svg = svg.replace("<svg ", f"<svg data-title=\"{title}\" style=\"background:#202830\" ", 1)
    output.write_text(svg, encoding="utf-8")
    for entity in marker_entities:
        modelspace.delete_entity(entity)


def main() -> int:
    args = parse_args()
    run_dir = args.run.resolve()
    sheets = json.loads((run_dir / "02_sheets" / "sheets.json").read_text(encoding="utf-8"))
    registrations = json.loads(
        (run_dir / "04_registration" / "registrations.json").read_text(encoding="utf-8")
    )
    rejected_ids = {
        item["source_sheet_id"]
        for item in registrations
        if not item["accepted"] and item["source_kind"] == args.kind and item["floor"] == args.floor
    }
    selected = [item for item in sheets if item["sheet_id"] in rejected_ids]
    if not selected:
        raise SystemExit("没有找到符合条件的拒绝分图")
    drawings = {item["drawing"] for item in selected}
    if len(drawings) != 1:
        raise SystemExit("所选拒绝分图不在同一个源CAD中")
    source = Path(next(iter(drawings)))
    doc = ezdxf.readfile(source)
    output_dir = run_dir / "09_rejected_sheet_locator"

    group_min_x = min(float(item["min_x"]) for item in selected)
    group_min_y = min(float(item["min_y"]) for item in selected)
    group_max_x = max(float(item["max_x"]) for item in selected)
    group_max_y = max(float(item["max_y"]) for item in selected)
    group_margin = max(group_max_x - group_min_x, group_max_y - group_min_y) * 0.12
    overview_limits = (
        group_min_x - group_margin,
        group_min_y - group_margin,
        group_max_x + group_margin,
        group_max_y + group_margin,
    )
    overview = output_dir / f"00_{args.kind}_{args.floor}_两处拒绝候选总览.svg"
    _render(doc, overview_limits, selected, overview, f"原专业CAD定位总览：{args.kind} {args.floor}")

    detail_files: list[str] = []
    for index, sheet in enumerate(selected, start=1):
        width = float(sheet["max_x"]) - float(sheet["min_x"])
        height = float(sheet["max_y"]) - float(sheet["min_y"])
        margin = max(width, height) * 0.10
        limits = (
            float(sheet["min_x"]) - margin,
            float(sheet["min_y"]) - margin,
            float(sheet["max_x"]) + margin,
            float(sheet["max_y"]) + margin,
        )
        output = output_dir / f"0{index}_{args.kind}_{args.floor}_拒绝候选{index}_{sheet['sheet_id']}.svg"
        _render(doc, limits, [sheet], output, f"拒绝候选 {index}：原CAD局部放大")
        detail_files.append(str(output))

    print(json.dumps({"overview": str(overview), "details": detail_files}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
