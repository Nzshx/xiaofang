"""Stage 03A: render stage-03 inspection regions with ezdxf.

This independent stage consumes ``drawing_sheets_floors.json`` from stage 03.
It renders review images and pixel/CAD transform manifests for a later visual
building detector.  It does not infer or persist any building region itself.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


GLOBAL_IMAGE_NAME = "global_context.png"
STRUCTURE_IMAGE_NAME = "structure_high_resolution.png"
TRANSFORM_MANIFEST_NAME = "render_transform_manifest.json"
TILE_MANIFEST_NAME = "structure_tiles_manifest.json"
RESULT_FILE_NAME = "building_region_render_result.json"
REVIEW_INDEX_NAME = "building_region_render_review.md"
RENDERER_NAME = "ezdxf drawing add-on / MatplotlibBackend"

STRUCTURE_POSITIVE_MARKERS = (
    "WALL",
    "STRS",
    "STRUCT",
    "COLU",
    "COLUMN",
    "CONC",
    "CUTN",
    "GLZE",
    "CURTAIN",
    "DOOR",
    "ELEV",
    "EVTR",
    "HOLE",
    "WIND",
    "WINDOW",
    "WINLIB",
    "STAIR",
    "ROOM",
    "墙",
    "柱",
    "幕墙",
    "门",
    "窗",
    "楼梯",
    "房间",
)
STRUCTURE_NEGATIVE_MARKERS = (
    "TEXT",
    "DIM",
    "ANNO-DIMS",
    "HATCH",
    "FILL",
    "FURN",
    "FIXT",
    "EQUIP",
    "PIPE",
    "DUCT",
    "ROAD",
    "LAND",
    "TITLE",
    "FRAME",
    "TABLE",
    "LEGEND",
    "尺寸",
    "标注",
    "填充",
    "家具",
    "设备",
    "管道",
    "图框",
    "图签",
    "表格",
)


@dataclass(frozen=True)
class BuildingRegionRenderResult:
    result_json: Path
    review_index: Path
    output_dir: Path
    sheet_count: int
    image_count: int
    tile_count: int
    renderer: str = RENDERER_NAME
    vision_status: str = "pending_no_visual_model"

    def to_summary(self) -> dict[str, Any]:
        return {
            "result_json": str(self.result_json),
            "review_index": str(self.review_index),
            "output_dir": str(self.output_dir),
            "sheet_count": self.sheet_count,
            "image_count": self.image_count,
            "tile_count": self.tile_count,
            "renderer": self.renderer,
            "vision_status": self.vision_status,
        }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        minx, miny, maxx, maxy = (float(value[index]) for index in range(4))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in (minx, miny, maxx, maxy)):
        return None
    if maxx <= minx or maxy <= miny:
        return None
    return (minx, miny, maxx, maxy)


def _read_sheets(sheets_json: Path) -> list[dict[str, Any]]:
    payload = json.loads(sheets_json.read_text(encoding="utf-8"))
    rows = payload.get("sheets") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"Invalid stage-03 sheet data: {sheets_json}")
    return [row for row in rows if isinstance(row, dict)]


def _bbox_intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return (
        left[0] <= right[2]
        and left[2] >= right[0]
        and left[1] <= right[3]
        and left[3] >= right[1]
    )


def _sheet_regions(
    sheet: dict[str, Any],
) -> list[tuple[str, tuple[float, float, float, float]]]:
    regions: list[tuple[str, tuple[float, float, float, float]]] = []
    for index, region in enumerate(sheet.get("inspection_regions") or [], start=1):
        if not isinstance(region, dict):
            continue
        box = _bbox(region.get("bbox"))
        if box:
            regions.append((str(region.get("region_id") or f"R{index:02d}"), box))
    if regions:
        return regions
    box = _bbox(sheet.get("inspection_region_bbox"))
    return [("R01", box)] if box else []


def _structure_layers(all_layers: Iterable[str]) -> list[str]:
    selected: list[str] = []
    for layer in all_layers:
        upper = layer.upper()
        positive = any(marker.upper() in upper for marker in STRUCTURE_POSITIVE_MARKERS)
        negative = any(marker.upper() in upper for marker in STRUCTURE_NEGATIVE_MARKERS)
        if positive and not negative:
            selected.append(layer)
    return sorted(set(selected))


def _load_document(input_dxf: Path) -> tuple[Any, list[str], str]:
    import ezdxf

    try:
        document = ezdxf.readfile(input_dxf)
        load_mode = "readfile"
    except (OSError, ezdxf.DXFError):
        from ezdxf import recover

        document, auditor = recover.readfile(input_dxf)
        if auditor.has_errors:
            serious = [str(error) for error in auditor.errors[:20]]
            raise RuntimeError(
                "DXF recovery reported unrecoverable errors: "
                + "; ".join(serious)
            )
        load_mode = "recover.readfile"
    layers = [str(layer.dxf.name) for layer in document.layers]
    return document, layers, load_mode


def _entity_attributes(row: dict[str, str], layer: str) -> dict[str, Any]:
    attributes: dict[str, Any] = {"layer": layer}
    try:
        color = int(str(row.get("color") or "256"))
    except ValueError:
        color = 256
    if 1 <= color <= 255:
        attributes["color"] = color
    raw_true_color = str(row.get("true_color") or "").strip()
    if raw_true_color:
        try:
            attributes["true_color"] = int(raw_true_color)
        except ValueError:
            pass
    return attributes


def _build_flattened_render_document(
    *,
    inventory_dir: Path,
    target_boxes: list[tuple[float, float, float, float]],
    source_document: Any,
) -> tuple[Any, dict[str, int]]:
    """Build an in-memory DXF from the already expanded vector inventory.

    Dynamic blocks and proxy entities in many architectural drawings are not
    fully renderable by ezdxf.  The vector inventory has already expanded those
    objects into world-coordinate geometry, so using it as the drawing source
    preserves more building linework while the actual rasterization still uses
    the ezdxf drawing backend.
    """
    import ezdxf

    geometry_path = inventory_dir / "cad_geometry_inventory.csv"
    object_path = inventory_dir / "cad_object_inventory.csv"
    if not geometry_path.is_file():
        raise FileNotFoundError(geometry_path)
    if not object_path.is_file():
        raise FileNotFoundError(object_path)

    document = ezdxf.new("R2013", setup=True)
    modelspace = document.modelspace()
    known_layers = {str(layer.dxf.name) for layer in document.layers}
    source_layer_colors = {
        str(layer.dxf.name): int(layer.dxf.color)
        for layer in source_document.layers
    }

    def ensure_layer(name: str) -> str:
        layer = name.strip() or "0"
        if layer in known_layers:
            return layer
        try:
            document.layers.add(
                layer,
                color=source_layer_colors.get(layer, 7),
            )
            known_layers.add(layer)
            return layer
        except Exception:
            return "0"

    geometry_rows = 0
    geometry_entities = 0
    with geometry_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            box = _bbox(
                [
                    row.get("bbox_minx"),
                    row.get("bbox_miny"),
                    row.get("bbox_maxx"),
                    row.get("bbox_maxy"),
                ]
            )
            if box is None or not any(
                _bbox_intersects(box, target) for target in target_boxes
            ):
                continue
            try:
                geometry = json.loads(row.get("geometry_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            source_lines = list(geometry.get("lines") or [])
            if not source_lines:
                for polygon in geometry.get("polygons") or []:
                    if isinstance(polygon, list) and len(polygon) >= 3:
                        source_lines.append([*polygon, polygon[0]])
            if not source_lines:
                continue
            layer = ensure_layer(str(row.get("layer") or "0"))
            attributes = _entity_attributes(row, layer)
            added_for_row = False
            for source_line in source_lines:
                points: list[tuple[float, float]] = []
                for point in source_line or []:
                    if not isinstance(point, (list, tuple)) or len(point) < 2:
                        continue
                    try:
                        x = float(point[0])
                        y = float(point[1])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(x) and math.isfinite(y):
                        points.append((x, y))
                if len(points) < 2:
                    continue
                modelspace.add_lwpolyline(points, dxfattribs=attributes)
                geometry_entities += 1
                added_for_row = True
            if added_for_row:
                geometry_rows += 1

    font_name = "msyh.ttc"
    if "STAGE03A_CJK" not in document.styles:
        document.styles.add(
            "STAGE03A_CJK",
            font=font_name,
        )
    text_entities = 0
    seen_text: set[tuple[str, str, int, int]] = set()
    with object_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            entity_type = str(row.get("entity_type") or "").upper()
            if entity_type not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
                continue
            text = str(row.get("norm_text") or row.get("raw_text") or "").strip()
            if not text or len(text) > 240:
                continue
            box = _bbox(
                [
                    row.get("bbox_minx"),
                    row.get("bbox_miny"),
                    row.get("bbox_maxx"),
                    row.get("bbox_maxy"),
                ]
            )
            if box is None or not any(
                _bbox_intersects(box, target) for target in target_boxes
            ):
                continue
            center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
            original_layer = str(row.get("layer") or "0").strip() or "0"
            text_layer = ensure_layer(f"{original_layer}__TEXT"[:240])
            key = (
                text,
                original_layer,
                round(center[0]),
                round(center[1]),
            )
            if key in seen_text:
                continue
            seen_text.add(key)
            height = max(
                box[3] - box[1],
                (box[2] - box[0]) / max(len(text), 1) * 0.8,
                1.0,
            )
            height = min(height, 600.0)
            attributes = _entity_attributes(row, text_layer)
            attributes.update(
                {
                    "height": height,
                    "style": "STAGE03A_CJK",
                }
            )
            entity = modelspace.add_text(
                text.replace("\n", " ")[:240],
                dxfattribs=attributes,
            )
            entity.dxf.insert = center
            text_entities += 1

    return document, {
        "geometry_inventory_rows": geometry_rows,
        "render_geometry_entities": geometry_entities,
        "render_text_entities": text_entities,
        "render_entity_count": len(modelspace),
    }


def _record_modelspace(document: Any) -> Any:
    from ezdxf.addons.drawing import Frontend, RenderContext, config, recorder
    from ezdxf.addons.drawing.properties import LayoutProperties

    modelspace = document.modelspace()
    rendering = config.Configuration(
        background_policy=config.BackgroundPolicy.BLACK,
        color_policy=config.ColorPolicy.COLOR,
        line_policy=config.LinePolicy.ACCURATE,
        lineweight_policy=config.LineweightPolicy.ABSOLUTE,
        lineweight_scaling=1.2,
        min_lineweight=0.18,
        # Dense architectural hatch patterns are slow and add visual noise for
        # building localization.  Preserve their boundaries without expanding
        # every hatch stroke.
        hatch_policy=config.HatchPolicy.SHOW_OUTLINE,
        proxy_graphic_policy=config.ProxyGraphicPolicy.PREFER,
        text_policy=config.TextPolicy.FILLING,
        max_flattening_distance=0.01,
        circle_approximation_count=256,
    )
    layout_properties = LayoutProperties.from_layout(modelspace)
    layout_properties.set_colors("#000000")
    backend = recorder.Recorder()
    Frontend(
        RenderContext(document),
        backend,
        config=rendering,
    ).draw_layout(
        modelspace,
        finalize=True,
        layout_properties=layout_properties,
    )
    return backend.player()


def _pixel_dimensions(
    inspection_bbox: tuple[float, float, float, float],
    long_side: int,
) -> tuple[int, int]:
    cad_width = inspection_bbox[2] - inspection_bbox[0]
    cad_height = inspection_bbox[3] - inspection_bbox[1]
    if cad_width >= cad_height:
        width = long_side
        height = max(512, int(round(long_side * cad_height / cad_width)))
    else:
        height = long_side
        width = max(512, int(round(long_side * cad_width / cad_height)))
    return width, height


def _render_region(
    *,
    player: Any,
    output_png: Path,
    inspection_bbox: tuple[float, float, float, float],
    target_long_side: int,
    mode: str,
    structure_layers: set[str],
    dpi: int,
) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import recorder
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from PIL import Image

    minx, miny, maxx, maxy = inspection_bbox
    cad_width = maxx - minx
    cad_height = maxy - miny
    width, height = _pixel_dimensions(
        inspection_bbox,
        max(1024, int(target_long_side)),
    )
    region_player = player.copy()
    crop_distance = max(cad_width, cad_height) / max(width, height) / 4.0
    region_player.crop_rect(
        (minx, miny),
        (maxx, maxy),
        distance=max(crop_distance, 1e-6),
    )

    background = "#000000" if mode == "context" else "#ffffff"
    figure = plt.figure(
        figsize=(width / dpi, height / dpi),
        dpi=dpi,
        facecolor=background,
        frameon=False,
    )
    axes = figure.add_axes([0, 0, 1, 1], facecolor=background)
    axes.set_axis_off()
    axes.set_xlim(minx, maxx)
    axes.set_ylim(miny, maxy)
    axes.set_aspect("equal", adjustable="box")
    backend = MatplotlibBackend(axes, adjust_figure=False)
    backend.set_background(background)

    override: Callable[[Any], Any] | None = None
    if mode == "structure":

        def structure_override(properties: Any) -> Any:
            visible = properties.layer in structure_layers
            black = properties._replace(
                color="#000000",
                lineweight=max(0.18, min(properties.lineweight, 0.45)),
            )
            return recorder.Override(black, visible)

        override = structure_override

    region_player.replay(backend, override=override)
    backend.finalize()
    # Matplotlib finalization may autoscale. Reset to the trusted stage-03 bbox.
    axes.set_xlim(minx, maxx)
    axes.set_ylim(miny, maxy)
    axes.set_aspect("equal", adjustable="box")
    axes.set_axis_off()
    figure.patch.set_facecolor(background)
    axes.set_facecolor(background)
    output_png.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(
        output_png,
        dpi=dpi,
        facecolor=background,
        edgecolor=background,
        bbox_inches=None,
        pad_inches=0,
        transparent=False,
    )
    plt.close(figure)

    # ezdxf uses an alpha-aware background policy and Matplotlib may still
    # persist a transparent canvas.  Composite explicitly so downstream vision
    # models always receive deterministic RGB pixels.
    with Image.open(output_png) as source:
        source.load()
        rgba = source.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, background)
        canvas.alpha_composite(rgba)
        rgb = canvas.convert("RGB")
        final_width, final_height = rgb.size
        rgb.save(output_png, optimize=True)
    if (final_width, final_height) != (width, height):
        raise RuntimeError(
            f"Unexpected render size for {output_png}: "
            f"{(final_width, final_height)} != {(width, height)}"
        )

    cad_per_pixel_x = cad_width / max(final_width - 1, 1)
    cad_per_pixel_y = cad_height / max(final_height - 1, 1)
    return {
        "image_path": str(output_png.resolve()),
        "image_type": mode,
        "image_width": final_width,
        "image_height": final_height,
        "dpi": dpi,
        "requested_long_side": target_long_side,
        "cad_bbox": list(inspection_bbox),
        "content_rect_px": [0, 0, final_width - 1, final_height - 1],
        "background": background,
        "source": "expanded_vector_inventory_modelspace",
        "pixel_to_cad": {
            "formula": {
                "cad_x": "cad_minx + pixel_x / (image_width - 1) * cad_width",
                "cad_y": "cad_maxy - pixel_y / (image_height - 1) * cad_height",
            },
            "cad_minx": minx,
            "cad_miny": miny,
            "cad_maxx": maxx,
            "cad_maxy": maxy,
            "cad_width": cad_width,
            "cad_height": cad_height,
            "scale_cad_units_per_pixel_x": cad_per_pixel_x,
            "scale_cad_units_per_pixel_y": cad_per_pixel_y,
            "offset_x": minx,
            "offset_y_from_top": maxy,
            "y_axis_inverted": True,
            "estimated_max_mapping_error_cad_units": max(
                cad_per_pixel_x,
                cad_per_pixel_y,
            ),
        },
        "render_settings": {
            "backend": RENDERER_NAME,
            "crop_method": "Recorder.Player.crop_rect",
            "crop_distance_cad_units": crop_distance,
            "matplotlib_axes_fill": [0, 0, 1, 1],
            "antialiasing": True,
        },
    }


def _pixel_bbox_to_cad(
    pixel_bbox: tuple[int, int, int, int],
    manifest: dict[str, Any],
) -> list[float]:
    minx, miny, maxx, maxy = (float(value) for value in manifest["cad_bbox"])
    width = int(manifest["image_width"])
    height = int(manifest["image_height"])
    left, top, right, bottom = pixel_bbox
    return [
        minx + left / max(width - 1, 1) * (maxx - minx),
        maxy - bottom / max(height - 1, 1) * (maxy - miny),
        minx + right / max(width - 1, 1) * (maxx - minx),
        maxy - top / max(height - 1, 1) * (maxy - miny),
    ]


def _write_structure_tiles(
    *,
    image_path: Path,
    output_dir: Path,
    transform: dict[str, Any],
    tile_size: int,
    overlap_ratio: float,
) -> list[dict[str, Any]]:
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    overlap = max(0, min(tile_size - 1, int(round(tile_size * overlap_ratio))))
    stride = max(1, tile_size - overlap)
    tiles: list[dict[str, Any]] = []
    with Image.open(image_path) as image:
        image.load()
        width, height = image.size

        def starts(length: int) -> list[int]:
            if length <= tile_size:
                return [0]
            values = list(range(0, max(1, length - tile_size + 1), stride))
            final_start = length - tile_size
            if values[-1] != final_start:
                values.append(final_start)
            return values

        for row_index, top in enumerate(starts(height), start=1):
            for column_index, left in enumerate(starts(width), start=1):
                right = min(width, left + tile_size)
                bottom = min(height, top + tile_size)
                tile_name = f"tile_r{row_index:02d}_c{column_index:02d}.png"
                tile_path = output_dir / tile_name
                image.crop((left, top, right, bottom)).save(
                    tile_path,
                    optimize=True,
                )
                pixel_bbox = (left, top, right - 1, bottom - 1)
                tiles.append(
                    {
                        "tile_id": f"T{len(tiles) + 1:03d}",
                        "path": str(tile_path.resolve()),
                        "pixel_bbox_in_structure_image": list(pixel_bbox),
                        "cad_bbox": _pixel_bbox_to_cad(pixel_bbox, transform),
                        "width": right - left,
                        "height": bottom - top,
                        "overlap_pixels": overlap,
                    }
                )
    return tiles


def _write_review_index(path: Path, sheets: list[dict[str, Any]]) -> None:
    lines = [
        "# Building Region 渲染审核",
        "",
        "本阶段使用 ezdxf drawing backend 渲染已展开到世界坐标的 CAD 图元，"
        "仅生成视觉模型输入，不执行 Building Region 判断。",
        "",
    ]
    for sheet in sheets:
        lines.extend(
            [
                f"## {sheet['sheet_id']} · "
                f"{sheet['floor_id'] or 'UNKNOWN'} · {sheet['region_id']}",
                "",
                f"- inspection region: `{sheet['inspection_bbox']}`",
                f"- 全局语义图：[{GLOBAL_IMAGE_NAME}]"
                f"({Path(sheet['global_context']['image_path']).as_posix()})",
                f"- 结构高精图：[{STRUCTURE_IMAGE_NAME}]"
                f"({Path(sheet['structure_high_resolution']['image_path']).as_posix()})",
                f"- 转换清单：[{TRANSFORM_MANIFEST_NAME}]"
                f"({Path(sheet['transform_manifest']).as_posix()})",
                f"- 结构切片：{sheet['tile_count']} 张",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def run_stage(
    *,
    input_dxf: Path | str,
    inventory_dir: Path | str,
    sheets_json: Path | str,
    run_dir: Path | str,
    global_long_side: int = 6000,
    structure_long_side: int = 8192,
    tile_size: int = 2048,
    tile_overlap_ratio: float = 0.20,
    dpi: int = 300,
) -> BuildingRegionRenderResult:
    """Render every usable inspection region directly from the original DXF."""
    try:
        import ezdxf
        import matplotlib
        from PIL import Image as _PillowImage  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "Stage 03A requires ezdxf drawing dependencies. Install with: "
            'python -m pip install "ezdxf[draw]"'
        ) from exc

    input_path = Path(input_dxf).expanduser().resolve()
    inventory_path = Path(inventory_dir).expanduser().resolve()
    sheets_path = Path(sheets_json).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if not sheets_path.is_file():
        raise FileNotFoundError(sheets_path)

    output_dir = Path(run_dir).expanduser().resolve() / "building_region_render"
    output_dir.mkdir(parents=True, exist_ok=True)
    sheets = _read_sheets(sheets_path)
    render_jobs: list[
        tuple[dict[str, Any], str, tuple[float, float, float, float]]
    ] = []
    for sheet in sheets:
        if not bool(sheet.get("path_planning_usable")):
            continue
        for region_id, inspection_bbox in _sheet_regions(sheet):
            render_jobs.append((sheet, region_id, inspection_bbox))
    if not render_jobs:
        raise RuntimeError(f"No usable inspection regions in {sheets_path}")

    source_document, source_layers, load_mode = _load_document(input_path)
    modelspace_entity_count = len(source_document.modelspace())
    render_document, render_stats = _build_flattened_render_document(
        inventory_dir=inventory_path,
        target_boxes=[job[2] for job in render_jobs],
        source_document=source_document,
    )
    render_layers = [str(layer.dxf.name) for layer in render_document.layers]
    structure_layers = _structure_layers(render_layers)
    if not structure_layers:
        raise RuntimeError("No structural layers matched the stage-03A rules")
    player = _record_modelspace(render_document)

    rendered: list[dict[str, Any]] = []
    image_count = 0
    tile_count = 0
    structure_layer_set = set(structure_layers)
    for sheet, region_id, inspection_bbox in render_jobs:
        sheet_id = str(sheet.get("sheet_id") or "SHEET_UNKNOWN")
        floor_id = str(sheet.get("floor_id") or "")
        artifact_id = f"{sheet_id}_{floor_id or 'UNKNOWN'}_{region_id}"
        artifact_dir = output_dir / artifact_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        global_path = artifact_dir / GLOBAL_IMAGE_NAME
        structure_path = artifact_dir / STRUCTURE_IMAGE_NAME

        global_transform = _render_region(
            player=player,
            output_png=global_path,
            inspection_bbox=inspection_bbox,
            target_long_side=max(2048, int(global_long_side)),
            mode="context",
            structure_layers=structure_layer_set,
            dpi=max(96, int(dpi)),
        )
        structure_transform = _render_region(
            player=player,
            output_png=structure_path,
            inspection_bbox=inspection_bbox,
            target_long_side=max(3072, int(structure_long_side)),
            mode="structure",
            structure_layers=structure_layer_set,
            dpi=max(96, int(dpi)),
        )
        tiles = _write_structure_tiles(
            image_path=structure_path,
            output_dir=artifact_dir / "structure_tiles",
            transform=structure_transform,
            tile_size=max(512, int(tile_size)),
            overlap_ratio=max(0.0, min(float(tile_overlap_ratio), 0.5)),
        )
        tile_manifest_path = artifact_dir / TILE_MANIFEST_NAME
        _write_json(
            tile_manifest_path,
            {
                "schema_version": "stage03a-structure-tiles-v2",
                "source_image": str(structure_path.resolve()),
                "tile_size": max(512, int(tile_size)),
                "overlap_ratio": max(
                    0.0,
                    min(float(tile_overlap_ratio), 0.5),
                ),
                "tile_count": len(tiles),
                "tiles": tiles,
            },
        )
        transform_manifest_path = artifact_dir / TRANSFORM_MANIFEST_NAME
        transform_manifest = {
            "schema_version": "stage03a-render-transform-v2",
            "renderer": RENDERER_NAME,
            "renderer_versions": {
                "ezdxf": ezdxf.__version__,
                "matplotlib": matplotlib.__version__,
            },
            "input_dxf": str(input_path),
            "source_layout": "Model",
            "source_mode": "expanded_vector_inventory",
            "dxf_load_mode": load_mode,
            "sheet_id": sheet_id,
            "floor_id": floor_id,
            "region_id": region_id,
            "inspection_bbox": list(inspection_bbox),
            "global_context": global_transform,
            "structure_high_resolution": structure_transform,
            "structure_layers": structure_layers,
            "render_inventory_stats": render_stats,
            "tile_manifest": str(tile_manifest_path.resolve()),
            "vision_status": "pending_no_visual_model",
        }
        _write_json(transform_manifest_path, transform_manifest)
        rendered.append(
            {
                "artifact_id": artifact_id,
                "sheet_id": sheet_id,
                "floor_id": floor_id,
                "floor_name": str(sheet.get("floor_name") or ""),
                "region_id": region_id,
                "inspection_bbox": list(inspection_bbox),
                "global_context": global_transform,
                "structure_high_resolution": structure_transform,
                "tile_count": len(tiles),
                "tile_manifest": str(tile_manifest_path.resolve()),
                "transform_manifest": str(transform_manifest_path.resolve()),
                "vision_status": "pending_no_visual_model",
                "building_count": None,
                "building_proposals": [],
            }
        )
        image_count += 2
        tile_count += len(tiles)

    result_path = output_dir / RESULT_FILE_NAME
    payload = {
        "schema_version": "stage03a-building-region-render-v2",
        "stage": "03A",
        "purpose": (
            "render inspection regions for later visual building count and "
            "coarse localization"
        ),
        "renderer": RENDERER_NAME,
        "renderer_versions": {
            "ezdxf": ezdxf.__version__,
            "matplotlib": matplotlib.__version__,
        },
        "source_mode": "expanded_vector_inventory",
        "dxf_load_mode": load_mode,
        "vision_status": "pending_no_visual_model",
        "input_dxf": str(input_path),
        "inventory_dir": str(inventory_path),
        "sheets_json": str(sheets_path),
        "modelspace_entity_count": modelspace_entity_count,
        "source_layer_count": len(source_layers),
        "render_inventory_stats": render_stats,
        "sheet_region_count": len(rendered),
        "image_count": image_count,
        "tile_count": tile_count,
        "structure_layer_count": len(structure_layers),
        "structure_layers": structure_layers,
        "render_defaults": {
            "global_long_side": max(2048, int(global_long_side)),
            "structure_long_side": max(3072, int(structure_long_side)),
            "tile_size": max(512, int(tile_size)),
            "tile_overlap_ratio": max(
                0.0,
                min(float(tile_overlap_ratio), 0.5),
            ),
            "dpi": max(96, int(dpi)),
        },
        "sheets": rendered,
    }
    _write_json(result_path, payload)
    review_index = output_dir / REVIEW_INDEX_NAME
    _write_review_index(review_index, rendered)
    return BuildingRegionRenderResult(
        result_json=result_path,
        review_index=review_index,
        output_dir=output_dir,
        sheet_count=len(rendered),
        image_count=image_count,
        tile_count=tile_count,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stage 03A: render stage-03 inspection regions with ezdxf. "
            "No visual model is invoked."
        )
    )
    parser.add_argument("--input-dxf", type=Path, required=True)
    parser.add_argument("--inventory-dir", type=Path, required=True)
    parser.add_argument("--sheets-json", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--global-long-side", type=int, default=6000)
    parser.add_argument("--structure-long-side", type=int, default=8192)
    parser.add_argument("--tile-size", type=int, default=2048)
    parser.add_argument("--tile-overlap-ratio", type=float, default=0.20)
    parser.add_argument("--dpi", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = run_stage(
        input_dxf=args.input_dxf,
        inventory_dir=args.inventory_dir,
        sheets_json=args.sheets_json,
        run_dir=args.run_dir,
        global_long_side=args.global_long_side,
        structure_long_side=args.structure_long_side,
        tile_size=args.tile_size,
        tile_overlap_ratio=args.tile_overlap_ratio,
        dpi=args.dpi,
    )
    print(json.dumps(result.to_summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BuildingRegionRenderResult", "run_stage"]
