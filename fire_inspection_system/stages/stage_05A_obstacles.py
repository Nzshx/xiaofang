"""Stage 05A: render recognized floor obstacles for building-region vision review.

This stage consumes the per-region obstacle-detail GeoJSON files produced by
``stage_05_obstacles``.  It does not re-read or re-classify CAD entities.
Column obstacles are excluded before the remaining geometry is merged by floor.

The generated PNG files deliberately use a simple visual vocabulary:

* white background;
* recognized obstacles in red;
* a light gray inspection-region frame;
* no CAD text, dimensions, axes, legends, or model-generated annotations.

Every image has a JSON/CSV manifest entry containing the exact CAD-to-pixel and
pixel-to-CAD affine transforms.  A vision model can therefore return a pixel
bbox or polygon and downstream code can map it back to the source CAD space.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - depends on runtime packaging
    raise RuntimeError(
        "stage_05A_obstacles requires Pillow. Install it with: pip install Pillow"
    ) from exc

from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.ops import unary_union


MANIFEST_JSON = "obstacle_building_vision_manifest.json"
MANIFEST_CSV = "obstacle_building_vision_manifest.csv"
BUILDING_REGIONS_JSON = "building_regions.json"
BUILDING_REGIONS_CSV = "building_regions.csv"
SHEETS_WITH_BUILDINGS_JSON = "drawing_sheets_floors_with_building_regions.json"
VISION_OUTPUT_DIR = "vision_building_regions"
BUILDING_ENVELOPE_OBSTACLES = "upper_floor_building_envelope_obstacles.geojson"
BUILDING_ENVELOPE_AUDIT = "upper_floor_building_envelope_obstacles_audit.json"
BUILDING_VISION_SCHEMA_VERSION = "building-detection-v1"
ARK_CHAT_COMPLETIONS_URL = (
    "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
)
DEFAULT_ARK_VISION_MODEL = "doubao-seed-2-0-lite-260215"
DEFAULT_ARK_API_KEY = "ark-7f05dde4-a396-49a1-ba3c-963933317d21-f4d0b"


@dataclass(frozen=True)
class ObstacleRenderConfig:
    """Rendering controls selected for high-resolution vision-model inputs."""

    max_side_pixels: int = 6000
    max_total_pixels: int = 32_000_000
    min_side_pixels: int = 1024
    margin_pixels: int = 36
    obstacle_color: str = "#ff3b30"
    obstacle_outline_color: str = "#ff3b30"
    inspection_frame_color: str = "#d9d9d9"
    background_color: str = "#ffffff"
    obstacle_outline_width: int = 1
    inspection_frame_width: int = 2
    excluded_obstacle_types: tuple[str, ...] = ("column",)

    def validate(self) -> None:
        if self.max_side_pixels < 256:
            raise ValueError("max_side_pixels must be at least 256")
        if self.min_side_pixels < 64:
            raise ValueError("min_side_pixels must be at least 64")
        if self.max_total_pixels < 65_536:
            raise ValueError("max_total_pixels is too small")
        if self.margin_pixels < 0:
            raise ValueError("margin_pixels must be non-negative")
        if self.margin_pixels * 2 >= self.min_side_pixels:
            raise ValueError("margin_pixels leaves no drawable image area")
        if self.obstacle_outline_width < 0 or self.inspection_frame_width < 0:
            raise ValueError("line widths must be non-negative")
        if not self.excluded_obstacle_types:
            raise ValueError("excluded_obstacle_types must include column")
        excluded = {str(value).strip().lower() for value in self.excluded_obstacle_types}
        if "column" not in excluded:
            raise ValueError("column must remain excluded from Stage 05A images")


@dataclass(frozen=True)
class ObstacleRenderResult:
    output_dir: Path
    manifest_json: Path
    manifest_csv: Path
    image_paths: tuple[Path, ...]
    image_count: int
    floor_count: int
    inspection_region_count: int


@dataclass(frozen=True)
class ArkVisionConfig:
    """Volcano Ark vision inference settings; environment overrides source default."""

    model: str = DEFAULT_ARK_VISION_MODEL
    model_name: str = "Doubao-Seed-2.0-lite"
    api_url: str = ARK_CHAT_COMPLETIONS_URL
    api_key_env: str = "ARK_API_KEY"
    timeout_seconds: int = 240
    max_retries: int = 3
    max_tokens: int = 4096
    force: bool = False
    temperature: float = 0.0

    def validate(self) -> None:
        if not self.model.strip():
            raise ValueError("Ark vision model/endpoint must not be empty")
        if not self.api_url.lower().startswith("https://"):
            raise ValueError("Ark vision api_url must use HTTPS")
        if not self.api_key_env.strip():
            raise ValueError("api_key_env must not be empty")
        if self.timeout_seconds < 30:
            raise ValueError("timeout_seconds must be at least 30")
        if self.max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if self.max_tokens < 256:
            raise ValueError("max_tokens must be at least 256")
        if not 0.0 <= self.temperature <= 1.0:
            raise ValueError("temperature must be between 0 and 1")


@dataclass(frozen=True)
class BuildingVectorValidationConfig:
    """Refine vision candidates with obstacle-density and connectivity checks."""

    enabled: bool = True
    split_projection_threshold_ratio: float = 0.018
    min_split_gap_pixels: int = 90
    min_split_gap_ratio: float = 0.028
    edge_guard_ratio: float = 0.045
    part_dilation_pixels: int = 41
    part_margin_pixels: int = 18
    merge_gap_pixels: int = 70
    min_part_red_pixels: int = 900
    min_part_red_fraction: float = 0.006
    min_group_red_fraction: float = 0.035
    candidate_guard_pixels: int = 48
    min_outside_candidate_major_red_fraction: float = 0.08
    max_bridge_gap_pixels: int = 180
    max_group_overlap_fraction: float = 0.01
    min_corridor_opening_pixels: int = 24
    corridor_obstacle_clearance_pixels: int = 3

    def validate(self) -> None:
        if self.min_split_gap_pixels < 1:
            raise ValueError("min_split_gap_pixels must be positive")
        if not 0.0 <= self.min_split_gap_ratio <= 0.5:
            raise ValueError("min_split_gap_ratio must be between 0 and 0.5")
        if not 0.0 <= self.edge_guard_ratio <= 0.4:
            raise ValueError("edge_guard_ratio must be between 0 and 0.4")
        if self.part_dilation_pixels < 1:
            raise ValueError("part_dilation_pixels must be positive")
        if self.part_margin_pixels < 0:
            raise ValueError("part_margin_pixels must be non-negative")
        if self.merge_gap_pixels < 0:
            raise ValueError("merge_gap_pixels must be non-negative")
        if self.min_part_red_pixels < 1:
            raise ValueError("min_part_red_pixels must be positive")
        if self.candidate_guard_pixels < 0:
            raise ValueError("candidate_guard_pixels must be non-negative")
        if not 0.0 <= self.min_outside_candidate_major_red_fraction <= 1.0:
            raise ValueError(
                "min_outside_candidate_major_red_fraction must be between 0 and 1"
            )
        if self.max_bridge_gap_pixels < 0:
            raise ValueError("max_bridge_gap_pixels must be non-negative")
        if not 0.0 <= self.max_group_overlap_fraction <= 1.0:
            raise ValueError("max_group_overlap_fraction must be between 0 and 1")
        if self.min_corridor_opening_pixels < 1:
            raise ValueError("min_corridor_opening_pixels must be positive")
        if self.corridor_obstacle_clearance_pixels < 0:
            raise ValueError(
                "corridor_obstacle_clearance_pixels must be non-negative"
            )


@dataclass(frozen=True)
class BuildingVisionResult:
    output_dir: Path
    building_regions_json: Path
    building_regions_csv: Path
    sheets_with_buildings_json: Path
    annotated_image_paths: tuple[Path, ...]
    response_paths: tuple[Path, ...]
    image_count: int
    success_image_count: int
    error_count: int
    building_region_count: int
    needs_review_count: int


class ArkVisionRequestError(RuntimeError):
    """Sanitized request failure for Volcano Ark vision calls."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class InspectionRegion:
    sheet_id: str
    floor_id: str
    floor_name: str
    sheet_title: str
    region_id: str
    bbox: tuple[float, float, float, float]
    source: str
    confidence: float | None

    @property
    def full_region_id(self) -> str:
        return f"{self.sheet_id}:{self.region_id}"


def extract_building_labels(sheet_title: str) -> list[str]:
    """Extract unique building-number labels such as 1#, 2#, and 3#."""

    title = str(sheet_title or "")
    candidates: list[tuple[int, str]] = []
    for match in re.finditer(
        r"(?<!\d)(\d+(?:\s*[、,，/]\s*\d+)+)\s*[#＃号]", title
    ):
        for number_match in re.finditer(r"\d+", match.group(1)):
            candidates.append(
                (
                    match.start(1) + number_match.start(),
                    f"{number_match.group(0)}#",
                )
            )

    for match in re.finditer(r"(?<![0-9A-Za-z])([A-Za-z]?\d+)\s*[#＃号]", title):
        candidates.append((match.start(1), f"{match.group(1).upper()}#"))

    labels: list[str] = []
    seen: set[str] = set()
    for _position, label in sorted(candidates, key=lambda item: item[0]):
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def find_multi_building_floor_ids(sheets_json: Path | str) -> list[str]:
    """Return usable floor ids whose floor-plan title indicates 2+ buildings."""

    payload = _read_json(Path(sheets_json).expanduser().resolve())
    floor_ids: list[str] = []
    seen: set[str] = set()
    for sheet in payload.get("sheets", []) if isinstance(payload, dict) else []:
        if not isinstance(sheet, dict) or not sheet.get("path_planning_usable"):
            continue
        floor_id = str(sheet.get("floor_id") or "").strip()
        if not floor_id or floor_id in seen:
            continue
        labels = extract_building_labels(str(sheet.get("sheet_title") or ""))
        raw_hint = sheet.get("building_count_hint")
        try:
            count_hint = int(raw_hint) if raw_hint is not None else len(labels)
        except (TypeError, ValueError):
            count_hint = len(labels)
        if max(count_hint, len(labels)) > 1:
            seen.add(floor_id)
            floor_ids.append(floor_id)
    return floor_ids


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "size": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    numbers = tuple(_safe_float(item) for item in value)
    if any(item is None for item in numbers):
        return None
    minx, miny, maxx, maxy = (float(item) for item in numbers)
    minx, maxx = sorted((minx, maxx))
    miny, maxy = sorted((miny, maxy))
    if maxx <= minx or maxy <= miny:
        return None
    return minx, miny, maxx, maxy


def _safe_token(value: str, fallback: str) -> str:
    token = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "")).strip("_.")
    return token or fallback


def _sheet_is_usable(sheet: dict[str, Any]) -> bool:
    raw = sheet.get("path_planning_usable")
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def _parse_regions_from_sheet(sheet: dict[str, Any]) -> list[InspectionRegion]:
    if not _sheet_is_usable(sheet):
        return []

    sheet_id = str(sheet.get("sheet_id") or "").strip()
    floor_id = str(sheet.get("floor_id") or "").strip()
    if not sheet_id or not floor_id or floor_id.upper() == "UNKNOWN":
        return []

    floor_name = str(sheet.get("floor_name") or "").strip()
    sheet_title = str(sheet.get("sheet_title") or "").strip()
    raw_regions = sheet.get("inspection_regions")
    if not isinstance(raw_regions, list):
        raw_regions = []

    regions: list[InspectionRegion] = []
    for index, raw_region in enumerate(raw_regions, start=1):
        if not isinstance(raw_region, dict):
            continue
        region_bbox = _valid_bbox(raw_region.get("bbox"))
        if region_bbox is None:
            continue
        confidence = _safe_float(raw_region.get("confidence"))
        regions.append(
            InspectionRegion(
                sheet_id=sheet_id,
                floor_id=floor_id,
                floor_name=floor_name,
                sheet_title=sheet_title,
                region_id=str(raw_region.get("region_id") or f"R{index:02d}").strip(),
                bbox=region_bbox,
                source=str(raw_region.get("source") or "").strip(),
                confidence=confidence,
            )
        )

    if regions:
        return regions

    fallback_bbox = _valid_bbox(
        sheet.get("inspection_region_bbox") or sheet.get("bbox")
    )
    if fallback_bbox is None:
        return []
    return [
        InspectionRegion(
            sheet_id=sheet_id,
            floor_id=floor_id,
            floor_name=floor_name,
            sheet_title=sheet_title,
            region_id="R01",
            bbox=fallback_bbox,
            source=str(sheet.get("inspection_region_source") or "sheet_bbox_fallback"),
            confidence=_safe_float(sheet.get("inspection_region_confidence")),
        )
    ]


def load_inspection_regions(sheets_json: Path | str) -> list[InspectionRegion]:
    path = Path(sheets_json).expanduser().resolve()
    payload = _read_json(path)
    raw_sheets = payload.get("sheets") if isinstance(payload, dict) else None
    if not isinstance(raw_sheets, list):
        raise ValueError(f"Invalid sheets JSON; missing sheets list: {path}")

    regions: list[InspectionRegion] = []
    seen: set[str] = set()
    for sheet in raw_sheets:
        if not isinstance(sheet, dict):
            continue
        for region in _parse_regions_from_sheet(sheet):
            if region.full_region_id in seen:
                raise ValueError(
                    f"Duplicate inspection region id: {region.full_region_id}"
                )
            seen.add(region.full_region_id)
            regions.append(region)
    if not regions:
        raise RuntimeError(f"No usable inspection regions found in: {path}")
    return regions


def _polygonal_parts(geometry: Any) -> list[Polygon]:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        parts: list[Polygon] = []
        for child in geometry.geoms:
            parts.extend(_polygonal_parts(child))
        return parts
    return []


def _valid_polygonal(geometry: Any) -> Any:
    if geometry is None or geometry.is_empty:
        return GeometryCollection()
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    parts = [part for part in _polygonal_parts(geometry) if part.area > 0]
    if not parts:
        return GeometryCollection()
    if len(parts) == 1:
        return parts[0]
    return unary_union(parts)


def _resolve_obstacle_detail_paths(
    obstacle_geojsons: Iterable[Path | str],
) -> list[Path]:
    """Resolve detail files even when a caller supplies Stage 05 union paths."""

    resolved: list[Path] = []
    for raw_path in obstacle_geojsons:
        path = Path(raw_path).expanduser().resolve()
        if path.parent.name == "per_floor_union":
            detail_dir = path.parent.parent / "per_region_geojson"
            detail_paths = sorted(detail_dir.glob("*.geojson"))
            if not detail_paths:
                raise FileNotFoundError(
                    "Column-free rendering requires Stage 05 per-region obstacle "
                    f"details, but none were found in: {detail_dir}"
                )
            resolved.extend(detail_paths)
        else:
            resolved.append(path)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in resolved:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def load_renderable_obstacles(
    obstacle_geojsons: Iterable[Path | str],
    *,
    excluded_obstacle_types: Iterable[str] = ("column",),
) -> tuple[
    dict[str, Any],
    dict[str, int],
    dict[str, int],
    dict[str, int],
    list[Path],
]:
    """Load obstacle details, exclude configured types, and merge by floor."""

    geometries: dict[str, list[Any]] = {}
    rendered_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    excluded_counts: dict[str, int] = {}
    source_paths: list[Path] = []
    excluded = {
        str(value).strip().lower()
        for value in excluded_obstacle_types
        if str(value).strip()
    }

    for path in _resolve_obstacle_detail_paths(obstacle_geojsons):
        payload = _read_json(path)
        source_paths.append(path)
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            raise ValueError(f"Invalid obstacle GeoJSON; missing features: {path}")

        for feature in features:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            floor_id = str(properties.get("floor_id") or "").strip()
            if not floor_id:
                raise ValueError(f"Obstacle feature has no floor_id: {path}")
            obstacle_type = str(properties.get("obstacle_type") or "").strip().lower()
            if not obstacle_type:
                raise ValueError(
                    "Obstacle detail has no obstacle_type, so column exclusion "
                    f"cannot be guaranteed: {path}"
                )
            source_counts[floor_id] = source_counts.get(floor_id, 0) + 1
            if obstacle_type in excluded:
                excluded_counts[floor_id] = excluded_counts.get(floor_id, 0) + 1
                continue
            rendered_counts[floor_id] = rendered_counts.get(floor_id, 0) + 1

            raw_geometry = feature.get("geometry")
            if raw_geometry is None:
                continue
            geometry = _valid_polygonal(shape(raw_geometry))
            if not geometry.is_empty:
                geometries.setdefault(floor_id, []).append(geometry)

    if not source_paths:
        raise RuntimeError("No Stage 05 obstacle-detail GeoJSON files were supplied")

    unions = {
        floor_id: _valid_polygonal(unary_union(items))
        for floor_id, items in geometries.items()
    }
    return (
        unions,
        rendered_counts,
        source_counts,
        excluded_counts,
        source_paths,
    )


def _choose_image_size(
    cad_width: float,
    cad_height: float,
    config: ObstacleRenderConfig,
) -> tuple[int, int]:
    aspect = cad_width / cad_height
    if aspect >= 1.0:
        width = config.max_side_pixels
        height = max(config.min_side_pixels, int(round(width / aspect)))
    else:
        height = config.max_side_pixels
        width = max(config.min_side_pixels, int(round(height * aspect)))

    pixel_count = width * height
    if pixel_count > config.max_total_pixels:
        shrink = math.sqrt(config.max_total_pixels / pixel_count)
        width = max(config.min_side_pixels, int(math.floor(width * shrink)))
        height = max(config.min_side_pixels, int(math.floor(height * shrink)))

    width = min(width, config.max_side_pixels)
    height = min(height, config.max_side_pixels)
    return width, height


def _make_transform(
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    margin: int,
) -> dict[str, Any]:
    minx, miny, maxx, maxy = bbox
    drawable_width = width - 2 * margin
    drawable_height = height - 2 * margin
    if drawable_width <= 0 or drawable_height <= 0:
        raise ValueError("Image margins leave no drawable area")

    scale = min(drawable_width / (maxx - minx), drawable_height / (maxy - miny))
    rendered_width = (maxx - minx) * scale
    rendered_height = (maxy - miny) * scale
    offset_x = (width - rendered_width) / 2.0
    offset_y = (height - rendered_height) / 2.0

    cad_to_pixel = [
        [scale, 0.0, offset_x - scale * minx],
        [0.0, -scale, offset_y + scale * maxy],
        [0.0, 0.0, 1.0],
    ]
    pixel_to_cad = [
        [1.0 / scale, 0.0, minx - offset_x / scale],
        [0.0, -1.0 / scale, maxy + offset_y / scale],
        [0.0, 0.0, 1.0],
    ]
    return {
        "scale_pixels_per_cad_unit": scale,
        "cad_units_per_pixel": 1.0 / scale,
        "offset_x_pixels": offset_x,
        "offset_y_pixels": offset_y,
        "content_pixel_bbox": [
            offset_x,
            offset_y,
            offset_x + rendered_width,
            offset_y + rendered_height,
        ],
        "cad_to_pixel_matrix": cad_to_pixel,
        "pixel_to_cad_matrix": pixel_to_cad,
        "formula": {
            "cad_to_pixel": [
                "pixel_x = offset_x + (cad_x - cad_min_x) * scale",
                "pixel_y = offset_y + (cad_max_y - cad_y) * scale",
            ],
            "pixel_to_cad": [
                "cad_x = cad_min_x + (pixel_x - offset_x) / scale",
                "cad_y = cad_max_y - (pixel_y - offset_y) / scale",
            ],
        },
    }


def _point_to_pixel(
    point: Sequence[float],
    bbox: tuple[float, float, float, float],
    transform: dict[str, Any],
) -> tuple[int, int]:
    minx, _miny, _maxx, maxy = bbox
    scale = float(transform["scale_pixels_per_cad_unit"])
    offset_x = float(transform["offset_x_pixels"])
    offset_y = float(transform["offset_y_pixels"])
    x = offset_x + (float(point[0]) - minx) * scale
    y = offset_y + (maxy - float(point[1])) * scale
    return int(round(x)), int(round(y))


def _iter_polygon_pixels(
    geometry: Any,
    bbox: tuple[float, float, float, float],
    transform: dict[str, Any],
) -> Iterator[tuple[list[tuple[int, int]], list[list[tuple[int, int]]]]]:
    for polygon in _polygonal_parts(geometry):
        exterior = [
            _point_to_pixel(point, bbox, transform)
            for point in polygon.exterior.coords
        ]
        holes = [
            [_point_to_pixel(point, bbox, transform) for point in ring.coords]
            for ring in polygon.interiors
        ]
        if len(exterior) >= 3:
            yield exterior, holes


def _component_count(geometry: Any) -> int:
    return len(_polygonal_parts(geometry))


def render_obstacle_region(
    region: InspectionRegion,
    floor_geometry: Any,
    output_path: Path,
    config: ObstacleRenderConfig,
) -> dict[str, Any]:
    minx, miny, maxx, maxy = region.bbox
    width, height = _choose_image_size(maxx - minx, maxy - miny, config)
    transform = _make_transform(region.bbox, width, height, config.margin_pixels)
    clip_box = box(minx, miny, maxx, maxy)
    clipped = (
        _valid_polygonal(floor_geometry.intersection(clip_box))
        if floor_geometry is not None and not floor_geometry.is_empty
        else GeometryCollection()
    )

    image = Image.new("RGB", (width, height), config.background_color)
    draw = ImageDraw.Draw(image)
    for exterior, holes in _iter_polygon_pixels(clipped, region.bbox, transform):
        draw.polygon(
            exterior,
            fill=config.obstacle_color,
            outline=config.obstacle_outline_color,
            width=config.obstacle_outline_width,
        )
        for hole in holes:
            if len(hole) >= 3:
                draw.polygon(hole, fill=config.background_color)

    content_bbox = transform["content_pixel_bbox"]
    frame = tuple(int(round(value)) for value in content_bbox)
    if config.inspection_frame_width > 0:
        draw.rectangle(
            frame,
            outline=config.inspection_frame_color,
            width=config.inspection_frame_width,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    building_labels_hint = extract_building_labels(region.sheet_title)

    return {
        "image_id": output_path.stem,
        "image_path": str(output_path.resolve()),
        "image_width": width,
        "image_height": height,
        "sheet_id": region.sheet_id,
        "floor_id": region.floor_id,
        "floor_name": region.floor_name,
        "sheet_title": region.sheet_title,
        "building_count_hint": len(building_labels_hint),
        "building_labels_hint": building_labels_hint,
        "region_id": region.region_id,
        "full_region_id": region.full_region_id,
        "inspection_region_source": region.source,
        "inspection_region_confidence": region.confidence,
        "cad_bbox": [minx, miny, maxx, maxy],
        "obstacle_geometry_type": clipped.geom_type,
        "obstacle_component_count": _component_count(clipped),
        "obstacle_area_cad_units2": float(clipped.area),
        "obstacle_coverage_ratio": float(clipped.area / clip_box.area),
        "transform": transform,
        "render_style": {
            "background_color": config.background_color,
            "obstacle_color": config.obstacle_color,
            "obstacle_outline_color": config.obstacle_outline_color,
            "inspection_frame_color": config.inspection_frame_color,
        },
    }


def _write_manifest_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "image_id",
        "image_path",
        "image_width",
        "image_height",
        "sheet_id",
        "floor_id",
        "floor_name",
        "sheet_title",
        "building_count_hint",
        "building_labels_hint",
        "region_id",
        "full_region_id",
        "inspection_region_source",
        "inspection_region_confidence",
        "cad_minx",
        "cad_miny",
        "cad_maxx",
        "cad_maxy",
        "scale_pixels_per_cad_unit",
        "cad_units_per_pixel",
        "offset_x_pixels",
        "offset_y_pixels",
        "floor_obstacle_count",
        "floor_source_obstacle_count",
        "excluded_column_count",
        "obstacle_component_count",
        "obstacle_area_cad_units2",
        "obstacle_coverage_ratio",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            bbox = record["cad_bbox"]
            transform = record["transform"]
            writer.writerow(
                {
                    **{field: record.get(field, "") for field in fields},
                    "building_labels_hint": "|".join(
                        str(value) for value in record.get("building_labels_hint", [])
                    ),
                    "cad_minx": bbox[0],
                    "cad_miny": bbox[1],
                    "cad_maxx": bbox[2],
                    "cad_maxy": bbox[3],
                    "scale_pixels_per_cad_unit": transform[
                        "scale_pixels_per_cad_unit"
                    ],
                    "cad_units_per_pixel": transform["cad_units_per_pixel"],
                    "offset_x_pixels": transform["offset_x_pixels"],
                    "offset_y_pixels": transform["offset_y_pixels"],
                }
            )


def run_stage(
    sheets_json: Path | str,
    obstacle_geojsons: Iterable[Path | str],
    run_dir: Path | str,
    *,
    config: ObstacleRenderConfig | None = None,
    floor_ids: Iterable[str] | None = None,
) -> ObstacleRenderResult:
    """Render red-obstacle images for selected inspection-region floors."""

    active_config = config or ObstacleRenderConfig()
    active_config.validate()
    sheets_path = Path(sheets_json).expanduser().resolve()
    run_path = Path(run_dir).expanduser().resolve()
    output_dir = run_path / "obstacle_building_region_render"
    image_dir = output_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    regions = load_inspection_regions(sheets_path)
    floor_filter = {
        str(value).strip() for value in floor_ids or [] if str(value).strip()
    }
    if floor_filter:
        regions = [region for region in regions if region.floor_id in floor_filter]
    if not regions:
        raise RuntimeError("No inspection regions matched the Stage 05A floor filter")
    (
        floor_unions,
        rendered_counts,
        source_counts,
        excluded_counts,
        obstacle_paths,
    ) = load_renderable_obstacles(
        obstacle_geojsons,
        excluded_obstacle_types=active_config.excluded_obstacle_types,
    )

    records: list[dict[str, Any]] = []
    image_paths: list[Path] = []
    for region in regions:
        image_name = "_".join(
            (
                _safe_token(region.sheet_id, "SHEET"),
                _safe_token(region.floor_id, "FLOOR"),
                _safe_token(region.region_id, "REGION"),
                "obstacles_red.png",
            )
        )
        image_path = image_dir / image_name
        record = render_obstacle_region(
            region,
            floor_unions.get(region.floor_id, GeometryCollection()),
            image_path,
            active_config,
        )
        record["floor_obstacle_count"] = rendered_counts.get(region.floor_id, 0)
        record["floor_source_obstacle_count"] = source_counts.get(region.floor_id, 0)
        record["excluded_column_count"] = excluded_counts.get(region.floor_id, 0)
        records.append(record)
        image_paths.append(image_path.resolve())

    manifest_json = output_dir / MANIFEST_JSON
    manifest_csv = output_dir / MANIFEST_CSV
    payload = {
        "schema_version": 1,
        "stage": "stage_05A_obstacles",
        "purpose": (
            "Vision-review evidence for coarse building count and building-region "
            "localization from the spatial density of recognized obstacles."
        ),
        "important_limitations": [
            "Red geometry is the Stage 05 recognized obstacle result, not complete CAD linework.",
            "Column obstacles are intentionally excluded from every rendered image.",
            "Dense obstacle groups are evidence for candidate buildings, not exact building boundaries.",
            "Vision-model building regions must remain reviewable candidates until vector validation.",
        ],
        "sources": {
            "sheets_json": _fingerprint(sheets_path),
            "obstacle_detail_geojsons": [
                _fingerprint(path) for path in obstacle_paths
            ],
        },
        "config": asdict(active_config),
        "image_count": len(records),
        "floor_count": len({record["floor_id"] for record in records}),
        "inspection_region_count": len(regions),
        "images": records,
    }
    _write_json(manifest_json, payload)
    _write_manifest_csv(manifest_csv, records)

    return ObstacleRenderResult(
        output_dir=output_dir.resolve(),
        manifest_json=manifest_json.resolve(),
        manifest_csv=manifest_csv.resolve(),
        image_paths=tuple(image_paths),
        image_count=len(image_paths),
        floor_count=len({region.floor_id for region in regions}),
        inspection_region_count=len(regions),
    )


def _image_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _image_data_url(path: Path) -> str:
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{_image_mime_type(path)};base64,{payload}"


def _resolve_record_image_path(record: dict[str, Any], manifest_path: Path) -> Path:
    raw_path = str(record.get("image_path") or "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()
        if path.is_file():
            return path.resolve()

    image_id = _safe_token(str(record.get("image_id") or ""), "image")
    fallback = manifest_path.parent / "images" / f"{image_id}.png"
    if fallback.is_file():
        return fallback.resolve()
    raise FileNotFoundError(f"Image for manifest record was not found: {image_id}")


def _building_vision_prompt(record: dict[str, Any]) -> str:
    labels = [str(value) for value in record.get("building_labels_hint") or []]
    count_hint = int(record.get("building_count_hint") or len(labels))
    title_hint = (
        f"{count_hint} buildings, labels: {', '.join(labels)}"
        if count_hint
        else "unknown"
    )
    return f"""
You are detecting Building regions from a fire-inspection preprocessing image.

Image facts:
- image_id: {record.get("image_id")}
- floor_id: {record.get("floor_id")}
- sheet_title: {record.get("sheet_title")}
- title building hint: {title_hint}
- image size: {record.get("image_width")} x {record.get("image_height")} pixels

Visual vocabulary:
- white background means empty canvas;
- red shapes are recognized non-column obstacles from Stage 05;
- the light gray rectangle is the inspection region / valid floor-plan extent;
- no CAD titles, dimensions, legends, or axes are included in the image.

Task:
1. Determine how many distinct buildings are present in this floor plan.
2. Draw one coarse Building region around each distinct building cluster.
3. Keep the inspection region as the valid drawing extent, but do not return it
   as a building.
4. If the title says "1#, 2#, 3#楼" or similar, treat those labels as a strong
   prior: there should normally be one Building region for each label.
5. Do not merge adjacent buildings that belong to different labels.
6. Exclude title blocks, legends, whitespace, and anything outside the gray frame.

Return only strict JSON using this schema:
{{
  "schema_version": "{BUILDING_VISION_SCHEMA_VERSION}",
  "image_id": "{record.get("image_id")}",
  "status": "ok",
  "main_plan": true,
  "building_count": 3,
  "buildings": [
    {{
      "building_id": "B01",
      "name_text": "1#",
      "bbox_1000": [100, 120, 360, 880],
      "polygon_1000": [[100, 120], [360, 120], [360, 880], [100, 880]],
      "confidence": 0.92,
      "evidence": "short reason based on red obstacle clusters and title hint",
      "boundary_quality": "clear",
      "needs_review": false,
      "uncertainty_reason": ""
    }}
  ],
  "excluded_regions": []
}}

Coordinate rules:
- bbox_1000 and polygon_1000 are relative to the full PNG image.
- x and y coordinates must be in the 0..1000 range.
- bbox format is [left, top, right, bottom].
- polygon_1000 may be approximate, but it must enclose the same building as bbox.
- If uncertain, still return your best candidate and mark needs_review=true.
""".strip()


def _ark_request_payload(
    config: ArkVisionConfig,
    record: dict[str, Any],
    image_data_url: str,
    *,
    use_json_response_format: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise vision model for CAD floor-plan preprocessing. "
                    "Return only valid JSON. Do not include markdown."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _building_vision_prompt(record)},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url},
                    },
                ],
            },
        ],
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if use_json_response_format:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _sanitize_error_text(value: str, *, limit: int = 2000) -> str:
    text = re.sub(r"ark-[0-9A-Za-z_-]+", "ark-***", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _unique_texts(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def _post_ark_once(
    config: ArkVisionConfig,
    api_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        config.api_url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=config.timeout_seconds,
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
        raise ArkVisionRequestError(
            f"Ark API HTTP {exc.code}: {_sanitize_error_text(body)}",
            status_code=exc.code,
            retryable=retryable,
        ) from exc
    except urllib.error.URLError as exc:
        raise ArkVisionRequestError(
            f"Ark API network error: {_sanitize_error_text(exc.reason)}",
            retryable=True,
        ) from exc
    except TimeoutError as exc:
        raise ArkVisionRequestError(
            "Ark API request timed out",
            retryable=True,
        ) from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArkVisionRequestError(
            f"Ark API returned non-JSON response: {_sanitize_error_text(raw)}",
            retryable=False,
        ) from exc
    if not isinstance(parsed, dict):
        raise ArkVisionRequestError("Ark API response was not a JSON object")
    return parsed


def _post_ark_with_retries(
    config: ArkVisionConfig,
    api_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    last_error: ArkVisionRequestError | None = None
    for attempt in range(1, config.max_retries + 1):
        try:
            return _post_ark_once(config, api_key, payload)
        except ArkVisionRequestError as exc:
            last_error = exc
            if not exc.retryable or attempt >= config.max_retries:
                raise
            time.sleep(min(2.0 ** (attempt - 1), 8.0))
    assert last_error is not None
    raise last_error


def _call_ark_building_vision(
    config: ArkVisionConfig,
    api_key: str,
    record: dict[str, Any],
    image_path: Path,
) -> tuple[dict[str, Any], bool]:
    image_data_url = _image_data_url(image_path)
    payload = _ark_request_payload(
        config,
        record,
        image_data_url,
        use_json_response_format=True,
    )
    try:
        return _post_ark_with_retries(config, api_key, payload), True
    except ArkVisionRequestError as exc:
        if exc.status_code != 400:
            raise
        payload = _ark_request_payload(
            config,
            record,
            image_data_url,
            use_json_response_format=False,
        )
        return _post_ark_with_retries(config, api_key, payload), False


def _extract_choice_content(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Ark response did not contain choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ValueError("Ark response choice did not contain a message")
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    raise ValueError("Ark response message content was not text")


def _parse_json_object_from_text(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        if start < 0:
            raise
        parsed, _end = json.JSONDecoder().raw_decode(value[start:])
    if not isinstance(parsed, dict):
        raise ValueError("Vision model JSON root must be an object")
    return parsed


def _safe_int(value: Any) -> int | None:
    number = _safe_float(value)
    if number is None:
        return None
    return int(round(number))


def _clamp_number(value: Any, low: float, high: float) -> float | None:
    number = _safe_float(value)
    if number is None:
        return None
    return min(max(number, low), high)


def _normalize_bbox_1000(value: Any) -> list[float] | None:
    bbox = _valid_bbox(value)
    if bbox is None:
        return None
    left, top, right, bottom = bbox
    values = [
        min(max(left, 0.0), 1000.0),
        min(max(top, 0.0), 1000.0),
        min(max(right, 0.0), 1000.0),
        min(max(bottom, 0.0), 1000.0),
    ]
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return [round(value, 3) for value in values]


def _normalize_polygon_1000(value: Any) -> list[list[float]] | None:
    if not isinstance(value, list):
        return None
    points: list[list[float]] = []
    for raw_point in value:
        if not isinstance(raw_point, (list, tuple)) or len(raw_point) < 2:
            continue
        x = _clamp_number(raw_point[0], 0.0, 1000.0)
        y = _clamp_number(raw_point[1], 0.0, 1000.0)
        if x is None or y is None:
            continue
        point = [round(x, 3), round(y, 3)]
        if not points or points[-1] != point:
            points.append(point)
    if len(points) < 3:
        return None
    if points[0] == points[-1]:
        points.pop()
    return points if len(points) >= 3 else None


def _bbox_1000_to_polygon(bbox: Sequence[float]) -> list[list[float]]:
    left, top, right, bottom = (float(value) for value in bbox)
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _bbox_from_points(points: Sequence[Sequence[float]]) -> list[float] | None:
    if not points:
        return None
    xs = [_safe_float(point[0]) for point in points if len(point) >= 2]
    ys = [_safe_float(point[1]) for point in points if len(point) >= 2]
    xs = [value for value in xs if value is not None]
    ys = [value for value in ys if value is not None]
    if not xs or not ys:
        return None
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    if right <= left or bottom <= top:
        return None
    return [round(left, 3), round(top, 3), round(right, 3), round(bottom, 3)]


def _point_1000_to_pixel(
    point: Sequence[float],
    width: int,
    height: int,
) -> list[float]:
    x = min(max(float(point[0]), 0.0), 1000.0) * float(width) / 1000.0
    y = min(max(float(point[1]), 0.0), 1000.0) * float(height) / 1000.0
    return [round(x, 3), round(y, 3)]


def _pixel_to_cad_point(record: dict[str, Any], point: Sequence[float]) -> list[float]:
    matrix = record.get("transform", {}).get("pixel_to_cad_matrix")
    if not isinstance(matrix, list) or len(matrix) < 2:
        raise ValueError(f"Manifest record has no pixel_to_cad_matrix: {record.get('image_id')}")
    x = float(point[0])
    y = float(point[1])
    cad_x = float(matrix[0][0]) * x + float(matrix[0][1]) * y + float(matrix[0][2])
    cad_y = float(matrix[1][0]) * x + float(matrix[1][1]) * y + float(matrix[1][2])
    cad_bbox = _valid_bbox(record.get("cad_bbox"))
    if cad_bbox:
        minx, miny, maxx, maxy = cad_bbox
        cad_x = min(max(cad_x, minx), maxx)
    cad_y = min(max(cad_y, miny), maxy)
    return [round(cad_x, 6), round(cad_y, 6)]


def _point_pixel_to_1000(
    point: Sequence[float],
    width: int,
    height: int,
) -> list[float]:
    x = min(max(float(point[0]), 0.0), float(width)) * 1000.0 / float(width)
    y = min(max(float(point[1]), 0.0), float(height)) * 1000.0 / float(height)
    return [round(x, 3), round(y, 3)]


def _pixel_bbox_to_polygon(bbox: Sequence[float]) -> list[list[float]]:
    left, top, right, bottom = (float(value) for value in bbox)
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _pixel_bbox_to_1000(
    bbox: Sequence[float],
    width: int,
    height: int,
) -> list[float]:
    left, top, right, bottom = (float(value) for value in bbox)
    return [
        round(left * 1000.0 / float(width), 3),
        round(top * 1000.0 / float(height), 3),
        round(right * 1000.0 / float(width), 3),
        round(bottom * 1000.0 / float(height), 3),
    ]


def _pixel_polygon_to_1000(
    polygon: Sequence[Sequence[float]],
    width: int,
    height: int,
) -> list[list[float]]:
    return [_point_pixel_to_1000(point, width, height) for point in polygon]


def _pixel_polygon_to_cad(
    record: dict[str, Any],
    polygon: Sequence[Sequence[float]],
) -> list[list[float]]:
    return [_pixel_to_cad_point(record, point) for point in polygon]


def _pixel_bbox_to_cad_bbox(
    record: dict[str, Any],
    bbox: Sequence[float],
) -> list[float] | None:
    polygon = _pixel_bbox_to_polygon(bbox)
    return _bbox_from_points(_pixel_polygon_to_cad(record, polygon))


def _clip_pixel_bbox(
    bbox: Sequence[float],
    clip_bbox: Sequence[float],
) -> list[int] | None:
    left = int(round(max(float(bbox[0]), float(clip_bbox[0]))))
    top = int(round(max(float(bbox[1]), float(clip_bbox[1]))))
    right = int(round(min(float(bbox[2]), float(clip_bbox[2]))))
    bottom = int(round(min(float(bbox[3]), float(clip_bbox[3]))))
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def _bbox_red_count(mask: Any, bbox: Sequence[int]) -> int:
    left, top, right, bottom = (int(value) for value in bbox)
    if right <= left or bottom <= top:
        return 0
    return int((mask[top:bottom, left:right] > 0).sum())


def _union_pixel_bbox(boxes: Sequence[Sequence[float]]) -> list[float] | None:
    if not boxes:
        return None
    return [
        float(min(box[0] for box in boxes)),
        float(min(box[1] for box in boxes)),
        float(max(box[2] for box in boxes)),
        float(max(box[3] for box in boxes)),
    ]


def _expand_pixel_bbox(
    bbox: Sequence[float],
    margin: float,
    clip_bbox: Sequence[float],
) -> list[int] | None:
    return _clip_pixel_bbox(
        [
            float(bbox[0]) - margin,
            float(bbox[1]) - margin,
            float(bbox[2]) + margin,
            float(bbox[3]) + margin,
        ],
        clip_bbox,
    )


def _pixel_bboxes_overlap_or_close(
    a: Sequence[float],
    b: Sequence[float],
    gap: float,
) -> bool:
    return not (
        float(a[2]) + gap < float(b[0])
        or float(b[2]) + gap < float(a[0])
        or float(a[3]) + gap < float(b[1])
        or float(b[3]) + gap < float(a[1])
    )


def _merge_close_pixel_bboxes(
    boxes: Sequence[Sequence[float]],
    *,
    gap: float,
    clip_bbox: Sequence[float],
) -> list[list[int]]:
    merged = [list(map(float, box)) for box in boxes]
    changed = True
    while changed:
        changed = False
        next_boxes: list[list[float]] = []
        used = [False] * len(merged)
        for index, box_a in enumerate(merged):
            if used[index]:
                continue
            current = list(box_a)
            used[index] = True
            for other_index in range(index + 1, len(merged)):
                if used[other_index]:
                    continue
                box_b = merged[other_index]
                if _pixel_bboxes_overlap_or_close(current, box_b, gap):
                    current = [
                        min(current[0], box_b[0]),
                        min(current[1], box_b[1]),
                        max(current[2], box_b[2]),
                        max(current[3], box_b[3]),
                    ]
                    used[other_index] = True
                    changed = True
            next_boxes.append(current)
        merged = next_boxes
    clipped: list[list[int]] = []
    for box_value in merged:
        clipped_box = _clip_pixel_bbox(box_value, clip_bbox)
        if clipped_box is not None:
            clipped.append(clipped_box)
    return clipped


def _normalize_excluded_regions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    regions: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        bbox = _normalize_bbox_1000(item.get("bbox_1000") or item.get("bbox"))
        if bbox is None:
            continue
        regions.append(
            {
                "label": str(item.get("label") or item.get("name") or "").strip(),
                "bbox_1000": bbox,
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return regions


def _normalize_building_detection(
    record: dict[str, Any],
    parsed: dict[str, Any],
    config: ArkVisionConfig,
    *,
    response_path: Path,
    annotated_image_path: Path,
    cached: bool,
) -> dict[str, Any]:
    labels = [str(value) for value in record.get("building_labels_hint") or []]
    hint_count = int(record.get("building_count_hint") or len(labels))
    width = int(record.get("image_width") or 0)
    height = int(record.get("image_height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid image size in manifest: {record.get('image_id')}")

    buildings: list[dict[str, Any]] = []
    raw_buildings = parsed.get("buildings")
    if not isinstance(raw_buildings, list):
        raw_buildings = []

    for raw_index, raw_building in enumerate(raw_buildings, start=1):
        if not isinstance(raw_building, dict):
            continue
        bbox = _normalize_bbox_1000(
            raw_building.get("bbox_1000")
            or raw_building.get("bbox")
            or raw_building.get("box_1000")
        )
        polygon = _normalize_polygon_1000(
            raw_building.get("polygon_1000") or raw_building.get("polygon")
        )
        if polygon is None and bbox is not None:
            polygon = _bbox_1000_to_polygon(bbox)
        if bbox is None and polygon is not None:
            bbox = _bbox_from_points(polygon)
        if bbox is None or polygon is None:
            continue

        index = len(buildings) + 1
        label_hint = labels[index - 1] if index <= len(labels) else ""
        name_text = str(
            raw_building.get("name_text")
            or raw_building.get("label")
            or raw_building.get("name")
            or label_hint
            or ""
        ).strip()
        confidence = _clamp_number(raw_building.get("confidence"), 0.0, 1.0)
        if confidence is None:
            confidence = 0.0

        polygon_pixel = [
            _point_1000_to_pixel(point, width, height) for point in polygon
        ]
        bbox_pixel = _bbox_from_points(polygon_pixel)
        if bbox_pixel is None:
            bbox_pixel = [
                round(bbox[0] * width / 1000.0, 3),
                round(bbox[1] * height / 1000.0, 3),
                round(bbox[2] * width / 1000.0, 3),
                round(bbox[3] * height / 1000.0, 3),
            ]
        polygon_cad = [
            _pixel_to_cad_point(record, point) for point in polygon_pixel
        ]
        bbox_cad = _bbox_from_points(polygon_cad)

        buildings.append(
            {
                "building_id": str(
                    raw_building.get("building_id") or f"B{index:02d}"
                ).strip(),
                "building_index": index,
                "name_text": name_text,
                "label_hint": label_hint,
                "bbox_1000": bbox,
                "polygon_1000": polygon,
                "bbox_pixel": bbox_pixel,
                "polygon_pixel": polygon_pixel,
                "bbox_cad": bbox_cad,
                "polygon_cad": polygon_cad,
                "confidence": round(float(confidence), 4),
                "evidence": str(raw_building.get("evidence") or "").strip(),
                "boundary_quality": str(
                    raw_building.get("boundary_quality") or "approximate"
                ).strip(),
                "needs_review": bool(raw_building.get("needs_review", False)),
                "uncertainty_reason": str(
                    raw_building.get("uncertainty_reason") or ""
                ).strip(),
            }
        )

    review_reasons: list[str] = []
    model_count = _safe_int(parsed.get("building_count"))
    if model_count is not None and model_count != len(buildings):
        review_reasons.append(
            f"model_count={model_count} differs from normalized_count={len(buildings)}"
        )
    if hint_count and len(buildings) != hint_count:
        review_reasons.append(
            f"title_hint_count={hint_count} differs from detected_count={len(buildings)}"
        )
    if any(building["needs_review"] for building in buildings):
        review_reasons.append("one_or_more_buildings_need_review")
    if not buildings:
        review_reasons.append("no_valid_building_region_returned")

    status = str(parsed.get("status") or "ok").strip().lower() or "ok"
    if review_reasons and status == "ok":
        status = "needs_review"
    needs_review = bool(review_reasons) or status not in {"ok", "success"}

    return {
        "schema_version": BUILDING_VISION_SCHEMA_VERSION,
        "stage": "stage_05A_obstacles",
        "status": status,
        "image_id": str(record.get("image_id") or ""),
        "sheet_id": str(record.get("sheet_id") or ""),
        "floor_id": str(record.get("floor_id") or ""),
        "floor_name": str(record.get("floor_name") or ""),
        "sheet_title": str(record.get("sheet_title") or ""),
        "region_id": str(record.get("region_id") or ""),
        "full_region_id": str(record.get("full_region_id") or ""),
        "main_plan": bool(parsed.get("main_plan", True)),
        "building_count_hint": hint_count,
        "building_labels_hint": labels,
        "model_building_count": model_count,
        "building_count": len(buildings),
        "needs_review": needs_review,
        "review_reasons": review_reasons,
        "model": config.model,
        "model_name": config.model_name,
        "source_image_path": str(record.get("image_path") or ""),
        "response_path": str(response_path.resolve()),
        "annotated_image_path": str(annotated_image_path.resolve()),
        "cached": cached,
        "created_at_epoch": time.time(),
        "buildings": buildings,
        "excluded_regions": _normalize_excluded_regions(parsed.get("excluded_regions")),
    }


def _error_building_detection(
    record: dict[str, Any],
    config: ArkVisionConfig,
    *,
    response_path: Path,
    annotated_image_path: Path,
    error_type: str,
    error_message: str,
) -> dict[str, Any]:
    labels = [str(value) for value in record.get("building_labels_hint") or []]
    return {
        "schema_version": BUILDING_VISION_SCHEMA_VERSION,
        "stage": "stage_05A_obstacles",
        "status": "error",
        "image_id": str(record.get("image_id") or ""),
        "sheet_id": str(record.get("sheet_id") or ""),
        "floor_id": str(record.get("floor_id") or ""),
        "floor_name": str(record.get("floor_name") or ""),
        "sheet_title": str(record.get("sheet_title") or ""),
        "region_id": str(record.get("region_id") or ""),
        "full_region_id": str(record.get("full_region_id") or ""),
        "main_plan": True,
        "building_count_hint": int(record.get("building_count_hint") or len(labels)),
        "building_labels_hint": labels,
        "model_building_count": None,
        "building_count": 0,
        "needs_review": True,
        "review_reasons": ["vision_model_error"],
        "model": config.model,
        "model_name": config.model_name,
        "source_image_path": str(record.get("image_path") or ""),
        "response_path": str(response_path.resolve()),
        "annotated_image_path": str(annotated_image_path.resolve()),
        "cached": False,
        "created_at_epoch": time.time(),
        "error_type": error_type,
        "error_message": _sanitize_error_text(error_message),
        "buildings": [],
        "excluded_regions": [],
    }


def _content_pixel_bbox(record: dict[str, Any]) -> list[int]:
    transform = record.get("transform") if isinstance(record.get("transform"), dict) else {}
    raw_bbox = transform.get("content_pixel_bbox") if isinstance(transform, dict) else None
    width = int(record.get("image_width") or 0)
    height = int(record.get("image_height") or 0)
    if isinstance(raw_bbox, list) and len(raw_bbox) == 4:
        bbox = _clip_pixel_bbox(raw_bbox, [0, 0, width, height])
        if bbox is not None:
            return bbox
    return [0, 0, width, height]


def _red_obstacle_mask_from_image(
    image_path: Path,
    record: dict[str, Any],
) -> Any:
    import numpy as np

    with Image.open(image_path) as image:
        array = np.array(image.convert("RGB"))
    red = (
        (array[:, :, 0] > 180)
        & (array[:, :, 1] < 120)
        & (array[:, :, 2] < 120)
    )
    mask = np.zeros(red.shape, dtype=np.uint8)
    left, top, right, bottom = _content_pixel_bbox(record)
    mask[top:bottom, left:right] = red[top:bottom, left:right].astype(np.uint8) * 255
    return mask


def _smooth_projection(values: Any, window: int) -> Any:
    import numpy as np

    if window <= 1:
        return values.astype(float)
    kernel = np.ones(int(window), dtype=float) / float(window)
    return np.convolve(values.astype(float), kernel, mode="same")


def _gap_candidates_from_projection(
    projection: Any,
    *,
    threshold: float,
    min_gap: int,
    edge_guard: int,
) -> list[tuple[int, int, float]]:
    candidates: list[tuple[int, int, float]] = []
    start: int | None = None
    size = len(projection)
    for index, value in enumerate(projection):
        in_gap = value <= threshold
        if in_gap and start is None:
            start = index
        elif not in_gap and start is not None:
            if index - start >= min_gap and start > edge_guard and index < size - edge_guard:
                mean_value = float(projection[start:index].mean())
                candidates.append((start, index, mean_value))
            start = None
    if start is not None and size - start >= min_gap and start > edge_guard:
        mean_value = float(projection[start:size].mean())
        candidates.append((start, size, mean_value))
    return candidates


def _best_mask_split(
    mask: Any,
    bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
) -> tuple[str, int, float, list[int], list[int]] | None:
    import numpy as np

    left, top, right, bottom = (int(value) for value in bbox)
    crop = mask[top:bottom, left:right] > 0
    red_total = int(crop.sum())
    if red_total <= 0:
        return None

    width = right - left
    height = bottom - top
    best: tuple[str, int, float, list[int], list[int]] | None = None
    axis_specs = [
        ("x", crop.sum(axis=0), width, height),
        ("y", crop.sum(axis=1), height, width),
    ]
    for axis, projection, axis_len, orth_len in axis_specs:
        if axis_len < 2 * config.min_split_gap_pixels:
            continue
        window = max(9, int(round(axis_len * 0.018)))
        if window % 2 == 0:
            window += 1
        smoothed = _smooth_projection(projection, window)
        threshold = max(
            2.0,
            float(orth_len) * config.split_projection_threshold_ratio,
            float(smoothed.max()) * config.split_projection_threshold_ratio,
        )
        min_gap = max(
            config.min_split_gap_pixels,
            int(round(axis_len * config.min_split_gap_ratio)),
        )
        edge_guard = int(round(axis_len * config.edge_guard_ratio))
        for start, end, mean_value in _gap_candidates_from_projection(
            smoothed,
            threshold=threshold,
            min_gap=min_gap,
            edge_guard=edge_guard,
        ):
            split_local = int(round((start + end) / 2.0))
            if axis == "x":
                first_crop = crop[:, :split_local]
                second_crop = crop[:, split_local:]
                first = [left, top, left + split_local, bottom]
                second = [left + split_local, top, right, bottom]
            else:
                first_crop = crop[:split_local, :]
                second_crop = crop[split_local:, :]
                first = [left, top, right, top + split_local]
                second = [left, top + split_local, right, bottom]
            first_red = int(first_crop.sum())
            second_red = int(second_crop.sum())
            min_group_red = max(
                config.min_part_red_pixels,
                int(round(red_total * config.min_group_red_fraction)),
            )
            if first_red < min_group_red or second_red < min_group_red:
                continue
            balance = min(first_red, second_red) / max(first_red, second_red)
            gap_width = end - start
            low_bonus = 1.0 - min(mean_value / max(threshold, 1.0), 1.0)
            score = float(gap_width) * (0.35 + balance) * (0.5 + low_bonus)
            if best is None or score > best[2]:
                best = (axis, split_local, score, first, second)
    return best


def _best_mask_axis_split(
    mask: Any,
    bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
    *,
    axis: str,
    threshold_multiplier: float = 1.0,
) -> tuple[int, list[int], list[int]] | None:
    left, top, right, bottom = (int(value) for value in bbox)
    crop = mask[top:bottom, left:right] > 0
    red_total = int(crop.sum())
    if red_total <= 0:
        return None
    width = right - left
    height = bottom - top
    if axis == "x":
        projection = crop.sum(axis=0)
        axis_len = width
        orth_len = height
    elif axis == "y":
        projection = crop.sum(axis=1)
        axis_len = height
        orth_len = width
    else:
        raise ValueError(f"Unsupported split axis: {axis}")
    if axis_len < 2 * config.min_split_gap_pixels:
        return None

    window = max(9, int(round(axis_len * 0.018)))
    if window % 2 == 0:
        window += 1
    smoothed = _smooth_projection(projection, window)
    threshold = max(
        2.0,
        float(orth_len) * config.split_projection_threshold_ratio,
        float(smoothed.max()) * config.split_projection_threshold_ratio,
    ) * max(0.1, threshold_multiplier)
    min_gap = max(
        config.min_split_gap_pixels,
        int(round(axis_len * config.min_split_gap_ratio)),
    )
    edge_guard = int(round(axis_len * config.edge_guard_ratio))

    best: tuple[float, int, list[int], list[int]] | None = None
    for start, end, mean_value in _gap_candidates_from_projection(
        smoothed,
        threshold=threshold,
        min_gap=min_gap,
        edge_guard=edge_guard,
    ):
        split_local = int(round((start + end) / 2.0))
        if axis == "x":
            first_crop = crop[:, :split_local]
            second_crop = crop[:, split_local:]
            first = [left, top, left + split_local, bottom]
            second = [left + split_local, top, right, bottom]
        else:
            first_crop = crop[:split_local, :]
            second_crop = crop[split_local:, :]
            first = [left, top, right, top + split_local]
            second = [left, top + split_local, right, bottom]
        first_red = int(first_crop.sum())
        second_red = int(second_crop.sum())
        min_group_red = max(
            config.min_part_red_pixels,
            int(round(red_total * config.min_group_red_fraction)),
        )
        if first_red < min_group_red or second_red < min_group_red:
            continue
        balance = min(first_red, second_red) / max(first_red, second_red)
        gap_width = end - start
        low_bonus = 1.0 - min(mean_value / max(threshold, 1.0), 1.0)
        score = float(gap_width) * (0.35 + balance) * (0.5 + low_bonus)
        if best is None or score > best[0]:
            split_abs = left + split_local if axis == "x" else top + split_local
            best = (score, split_abs, first, second)
    if best is None:
        return None
    _score, split_abs, first, second = best
    return split_abs, first, second


def _red_bbox_in_region(
    mask: Any,
    bbox: Sequence[int],
    *,
    clip_bbox: Sequence[int],
    margin: int,
) -> list[int] | None:
    import numpy as np

    left, top, right, bottom = (int(value) for value in bbox)
    crop = mask[top:bottom, left:right] > 0
    if not crop.any():
        return None
    ys, xs = np.where(crop)
    raw = [
        left + int(xs.min()),
        top + int(ys.min()),
        left + int(xs.max()) + 1,
        top + int(ys.max()) + 1,
    ]
    return _expand_pixel_bbox(raw, margin, clip_bbox)


def _split_mask_into_regions(
    mask: Any,
    *,
    target_count: int,
    content_bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
) -> list[list[int]]:
    root = _red_bbox_in_region(
        mask,
        content_bbox,
        clip_bbox=content_bbox,
        margin=max(config.part_margin_pixels * 2, 24),
    )
    if root is None:
        return []
    regions = [root]
    while len(regions) < target_count:
        split_options: list[tuple[float, int, list[int], list[int]]] = []
        for index, region in enumerate(regions):
            split = _best_mask_split(mask, region, config)
            if split is None:
                continue
            _axis, _coord, score, first, second = split
            split_options.append((score, index, first, second))
        if not split_options:
            break
        _score, index, first, second = max(split_options, key=lambda item: item[0])
        regions.pop(index)
        regions.extend([first, second])

    refined: list[list[int]] = []
    for region in regions:
        red_bbox = _red_bbox_in_region(
            mask,
            region,
            clip_bbox=content_bbox,
            margin=config.part_margin_pixels,
        )
        if red_bbox is not None:
            refined.append(red_bbox)
    return refined


def _component_part_boxes(
    mask: Any,
    region_bbox: Sequence[int],
    *,
    content_bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
) -> list[list[int]]:
    import cv2
    import numpy as np

    left, top, right, bottom = (int(value) for value in region_bbox)
    crop = mask[top:bottom, left:right]
    red_total = int((crop > 0).sum())
    if red_total <= 0:
        return []
    kernel_size = max(3, int(config.part_dilation_pixels))
    if kernel_size % 2 == 0:
        kernel_size += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    dilated = cv2.dilate(crop, kernel, iterations=1)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        dilated,
        8,
    )
    boxes: list[list[int]] = []
    min_red = max(
        config.min_part_red_pixels,
        int(round(red_total * config.min_part_red_fraction)),
    )
    for label in range(1, count):
        x, y, width, height, _area = [int(value) for value in stats[label]]
        label_mask = labels == label
        red_count = int((label_mask & (crop > 0)).sum())
        if red_count < min_red:
            continue
        raw_box = [left + x, top + y, left + x + width, top + y + height]
        expanded = _expand_pixel_bbox(
            raw_box,
            config.part_margin_pixels,
            content_bbox,
        )
        if expanded is not None:
            boxes.append(expanded)

    if not boxes:
        fallback = _red_bbox_in_region(
            mask,
            region_bbox,
            clip_bbox=content_bbox,
            margin=config.part_margin_pixels,
        )
        return [fallback] if fallback is not None else []
    return _merge_close_pixel_bboxes(
        boxes,
        gap=config.merge_gap_pixels,
        clip_bbox=content_bbox,
    )


def _gap_distance_between_pixel_bboxes(
    a: Sequence[float],
    b: Sequence[float],
) -> float:
    dx = max(0.0, max(float(a[0]), float(b[0])) - min(float(a[2]), float(b[2])))
    dy = max(0.0, max(float(a[1]), float(b[1])) - min(float(a[3]), float(b[3])))
    return math.hypot(dx, dy)


def _pixel_bbox_intersection_area(
    a: Sequence[float],
    b: Sequence[float],
) -> float:
    width = max(0.0, min(float(a[2]), float(b[2])) - max(float(a[0]), float(b[0])))
    height = max(0.0, min(float(a[3]), float(b[3])) - max(float(a[1]), float(b[1])))
    return width * height


def _candidate_pixel_bboxes(detection: dict[str, Any]) -> list[list[float]]:
    output: list[list[float]] = []
    for building in detection.get("buildings") or []:
        if not isinstance(building, dict):
            continue
        bbox = _valid_bbox(building.get("bbox_pixel"))
        if bbox is not None:
            output.append([float(value) for value in bbox])
    return output


def _filter_group_parts_by_vision_candidates(
    mask: Any,
    groups: Sequence[dict[str, Any]],
    detection: dict[str, Any],
    *,
    content_bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
) -> list[dict[str, Any]]:
    """Keep vector parts supported by at least one original vision candidate.

    Candidate ownership is intentionally evaluated against the complete candidate
    set instead of candidate labels.  The model is reliable as a coarse spatial
    prior, but the title-derived B01/B02/B03 order is not a reliable spatial id.
    """

    candidates = _candidate_pixel_bboxes(detection)
    if not candidates:
        return [dict(group) for group in groups]

    expanded_candidates = [
        _expand_pixel_bbox(candidate, config.candidate_guard_pixels, content_bbox)
        for candidate in candidates
    ]
    expanded_candidates = [candidate for candidate in expanded_candidates if candidate]
    output: list[dict[str, Any]] = []
    for group in groups:
        raw_parts = [
            [int(round(float(value))) for value in part]
            for part in group.get("parts_pixel") or []
            if isinstance(part, (list, tuple)) and len(part) == 4
        ]
        part_red_counts = [_bbox_red_count(mask, part) for part in raw_parts]
        group_red_total = max(1, sum(part_red_counts))
        kept: list[list[int]] = []
        rejected: list[dict[str, Any]] = []
        for part, red_count in zip(raw_parts, part_red_counts):
            direct_support = any(
                _pixel_bbox_intersection_area(part, candidate) > 0.0
                for candidate in candidates
            )
            guarded_support = any(
                _pixel_bbox_intersection_area(part, candidate) > 0.0
                for candidate in expanded_candidates
            )
            major_fraction = float(red_count) / float(group_red_total)
            keep = direct_support or (
                guarded_support
                and major_fraction >= config.min_outside_candidate_major_red_fraction
            )
            if keep:
                kept.append(part)
            else:
                rejected.append(
                    {
                        "bbox_pixel": part,
                        "red_pixel_count": red_count,
                        "group_red_fraction": round(major_fraction, 6),
                        "reason": "outside_all_original_vision_candidates",
                    }
                )
        union_box = _union_pixel_bbox(kept)
        if union_box is None:
            continue
        updated = dict(group)
        updated["parts_pixel"] = kept
        updated["bbox_pixel"] = union_box
        updated["center_pixel"] = [
            (union_box[0] + union_box[2]) / 2.0,
            (union_box[1] + union_box[3]) / 2.0,
        ]
        updated["red_pixel_count"] = sum(_bbox_red_count(mask, part) for part in kept)
        updated["rejected_candidate_external_parts"] = rejected
        output.append(updated)
    return output


def _reassign_three_region_parts_to_candidate_roles(
    mask: Any,
    groups: Sequence[dict[str, Any]],
    detection: dict[str, Any],
) -> list[dict[str, Any]]:
    """Make the L-layout split mutually exclusive using coarse candidate overlap."""

    if len(groups) != 3 or not all(
        group.get("grouping_method") == "three_region_l_shape_density_split"
        for group in groups
    ):
        return [dict(group) for group in groups]
    candidates = _candidate_pixel_bboxes(detection)
    if len(candidates) != 3:
        return [dict(group) for group in groups]
    left_candidate = min(
        candidates,
        key=lambda bbox: (bbox[0] + bbox[2]) / 2.0,
    )
    right_candidates = [candidate for candidate in candidates if candidate is not left_candidate]
    right_candidates.sort(key=lambda bbox: (bbox[1] + bbox[3]) / 2.0)
    role_candidates = [left_candidate, right_candidates[0], right_candidates[1]]

    assigned: list[list[list[int]]] = [[], [], []]
    seen: set[tuple[int, int, int, int]] = set()
    for source_group_index, group in enumerate(groups):
        for raw_part in group.get("parts_pixel") or []:
            if not isinstance(raw_part, (list, tuple)) or len(raw_part) != 4:
                continue
            part = [int(round(float(value))) for value in raw_part]
            token = tuple(part)
            if token in seen:
                continue
            seen.add(token)
            overlaps = [
                _pixel_bbox_intersection_area(part, candidate)
                for candidate in role_candidates
            ]
            best_overlap = max(overlaps)
            target_index = (
                overlaps.index(best_overlap)
                if best_overlap > 0.0
                else source_group_index
            )
            assigned[target_index].append(part)

    output: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        parts = assigned[index]
        union_box = _union_pixel_bbox(parts)
        if union_box is None:
            return [dict(value) for value in groups]
        updated = dict(group)
        updated["parts_pixel"] = parts
        updated["bbox_pixel"] = union_box
        updated["center_pixel"] = [
            (union_box[0] + union_box[2]) / 2.0,
            (union_box[1] + union_box[3]) / 2.0,
        ]
        updated["red_pixel_count"] = sum(_bbox_red_count(mask, part) for part in parts)
        updated["exclusive_assignment_method"] = (
            "maximum_overlap_with_left_upper_right_lower_right_candidate_roles"
        )
        output.append(updated)
    return output


def _bridge_between_pixel_bboxes(
    first: Sequence[float],
    second: Sequence[float],
    *,
    clip_bbox: Sequence[int],
) -> list[int] | None:
    ax1, ay1, ax2, ay2 = [float(value) for value in first]
    bx1, by1, bx2, by2 = [float(value) for value in second]
    overlap_y1 = max(ay1, by1)
    overlap_y2 = min(ay2, by2)
    if overlap_y2 > overlap_y1:
        if ax2 < bx1:
            return _clip_pixel_bbox([ax2, overlap_y1, bx1, overlap_y2], clip_bbox)
        if bx2 < ax1:
            return _clip_pixel_bbox([bx2, overlap_y1, ax1, overlap_y2], clip_bbox)
    overlap_x1 = max(ax1, bx1)
    overlap_x2 = min(ax2, bx2)
    if overlap_x2 > overlap_x1:
        if ay2 < by1:
            return _clip_pixel_bbox([overlap_x1, ay2, overlap_x2, by1], clip_bbox)
        if by2 < ay1:
            return _clip_pixel_bbox([overlap_x1, by2, overlap_x2, ay1], clip_bbox)
    return None


def _connect_group_parts(
    group: dict[str, Any],
    *,
    content_bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
) -> tuple[dict[str, Any], bool]:
    """Connect small axis-aligned skeleton gaps without bridging unrelated plans."""

    parts = [
        [int(round(float(value))) for value in part]
        for part in group.get("parts_pixel") or []
        if isinstance(part, (list, tuple)) and len(part) == 4
    ]
    bridges: list[list[int]] = []
    for _attempt in range(max(1, len(parts) * 2)):
        geometry = unary_union([box(*part) for part in parts]) if parts else GeometryCollection()
        polygon_parts = list(_polygonal_parts(geometry))
        if len(polygon_parts) <= 1:
            updated = dict(group)
            updated["parts_pixel"] = parts
            updated["synthetic_bridge_boxes_pixel"] = bridges
            updated["topology_connected"] = bool(parts)
            union_box = _union_pixel_bbox(parts)
            if union_box is not None:
                updated["bbox_pixel"] = union_box
            return updated, bool(parts)

        best: tuple[float, Any, Any] | None = None
        for left_index, left_part in enumerate(polygon_parts):
            for right_part in polygon_parts[left_index + 1 :]:
                distance = float(left_part.distance(right_part))
                if best is None or distance < best[0]:
                    best = (distance, left_part, right_part)
        if best is None or best[0] > float(config.max_bridge_gap_pixels):
            break
        bridge = _bridge_between_pixel_bboxes(
            best[1].bounds,
            best[2].bounds,
            clip_bbox=content_bbox,
        )
        if bridge is None:
            break
        if (bridge[2] - bridge[0]) <= 0 or (bridge[3] - bridge[1]) <= 0:
            break
        parts.append(bridge)
        bridges.append(bridge)

    updated = dict(group)
    updated["parts_pixel"] = parts
    updated["synthetic_bridge_boxes_pixel"] = bridges
    updated["topology_connected"] = False
    return updated, False


def _groups_have_legal_overlap(
    groups: Sequence[dict[str, Any]],
    config: BuildingVectorValidationConfig,
) -> bool:
    geometries = [
        unary_union([box(*part) for part in group.get("parts_pixel") or []])
        for group in groups
    ]
    for left_index, left in enumerate(geometries):
        if left.is_empty:
            return False
        for right in geometries[left_index + 1 :]:
            denominator = max(1.0, min(float(left.area), float(right.area)))
            overlap_fraction = float(left.intersection(right).area) / denominator
            if overlap_fraction > config.max_group_overlap_fraction:
                return False
    return True


def _cad_units_per_pixel(record: dict[str, Any]) -> float | None:
    content_bbox = _content_pixel_bbox(record)
    origin_x = (float(content_bbox[0]) + float(content_bbox[2])) / 2.0
    origin_y = (float(content_bbox[1]) + float(content_bbox[3])) / 2.0
    points = _pixel_polygon_to_cad(
        record,
        [
            [origin_x, origin_y],
            [origin_x + 1.0, origin_y],
            [origin_x, origin_y + 1.0],
        ],
    )
    if len(points) < 3:
        return None
    x_scale = math.dist(points[0], points[1])
    y_scale = math.dist(points[0], points[2])
    values = [value for value in (x_scale, y_scale) if math.isfinite(value) and value > 0]
    return sum(values) / len(values) if values else None


def _detect_corridor_connections(
    groups: Sequence[dict[str, Any]],
    record: dict[str, Any],
    floor_geometry: Any | None,
    config: BuildingVectorValidationConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate open shared boundaries and assign connected-building groups."""

    if floor_geometry is None or floor_geometry.is_empty or len(groups) < 2:
        return [dict(group) for group in groups], []
    cad_per_pixel = _cad_units_per_pixel(record)
    if cad_per_pixel is None:
        return [dict(group) for group in groups], []
    minimum_opening = cad_per_pixel * float(config.min_corridor_opening_pixels)
    clearance = cad_per_pixel * float(config.corridor_obstacle_clearance_pixels)
    geometries = [
        _valid_polygonal(unary_union(_shapely_parts_from_pixel_boxes(record, group.get("parts_pixel") or [])))
        for group in groups
    ]
    parent = list(range(len(groups)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    raw_connections: list[dict[str, Any]] = []
    obstacle_guard = floor_geometry.buffer(clearance) if clearance > 0 else floor_geometry
    for left_index, left in enumerate(geometries):
        if left.is_empty:
            continue
        for right_index in range(left_index + 1, len(geometries)):
            right = geometries[right_index]
            if right.is_empty:
                continue
            shared_boundary = left.boundary.intersection(right.boundary)
            if shared_boundary.is_empty:
                continue
            open_boundary = shared_boundary.difference(obstacle_guard)
            open_length = float(open_boundary.length)
            if open_length < minimum_opening:
                continue
            corridor = _valid_polygonal(
                open_boundary.buffer(max(clearance, cad_per_pixel * 4.0), cap_style=2)
                .intersection(left.union(right))
            )
            if corridor.is_empty:
                continue
            union(left_index, right_index)
            raw_connections.append(
                {
                    "left_index": left_index,
                    "right_index": right_index,
                    "open_boundary_length_cad": round(open_length, 6),
                    "minimum_opening_cad": round(minimum_opening, 6),
                    "corridor_polygons_cad": [
                        [[round(float(x), 6), round(float(y), 6)] for x, y in polygon.exterior.coords]
                        for polygon in _polygonal_parts(corridor)
                    ],
                    "validation_method": "shared_free_boundary_vector_clearance",
                }
            )

    components: dict[int, list[int]] = {}
    for index in range(len(groups)):
        components.setdefault(find(index), []).append(index)
    connected_components = sorted(
        (indices for indices in components.values() if len(indices) > 1),
        key=lambda indices: min(indices),
    )
    group_name_by_index: dict[int, str] = {}
    for connection_index, indices in enumerate(connected_components, start=1):
        group_name = f"C{connection_index:03d}"
        for index in indices:
            group_name_by_index[index] = group_name

    updated_groups: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        updated = dict(group)
        if index in group_name_by_index:
            updated["corridor_connection_group"] = group_name_by_index[index]
            updated["connected_group_indices"] = components[find(index)]
        updated_groups.append(updated)

    connections: list[dict[str, Any]] = []
    for connection_index, connection in enumerate(raw_connections, start=1):
        left_index = int(connection["left_index"])
        right_index = int(connection["right_index"])
        connections.append(
            {
                "corridor_id": f"CORRIDOR_{connection_index:03d}",
                "corridor_connection_group": group_name_by_index.get(left_index, ""),
                **connection,
            }
        )
    return updated_groups, connections


def _major_component_cluster_groups(
    mask: Any,
    *,
    target_count: int,
    content_bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
) -> list[dict[str, Any]]:
    import cv2

    if target_count != 2:
        return []
    total_red = int((mask > 0).sum())
    if total_red <= 0:
        return []

    kernel_size = max(3, int(config.part_dilation_pixels))
    if kernel_size % 2 == 0:
        kernel_size += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    dilated = cv2.dilate(mask, kernel, iterations=1)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        dilated,
        8,
    )
    min_major_red = max(
        config.min_part_red_pixels * 8,
        int(round(total_red * 0.04)),
    )
    components: list[dict[str, Any]] = []
    for label in range(1, count):
        x, y, width, height, _area = [int(value) for value in stats[label]]
        red_count = int(((labels == label) & (mask > 0)).sum())
        if red_count < min_major_red:
            continue
        expanded = _expand_pixel_bbox(
            [x, y, x + width, y + height],
            config.part_margin_pixels,
            content_bbox,
        )
        if expanded is None:
            continue
        components.append({"bbox": expanded, "red_pixel_count": red_count})

    if len(components) <= target_count:
        return []

    groups: list[list[dict[str, Any]]] = [[component] for component in components]
    while len(groups) > target_count:
        best: tuple[float, int, int] | None = None
        for left_index in range(len(groups)):
            left_bbox = _union_pixel_bbox([item["bbox"] for item in groups[left_index]])
            if left_bbox is None:
                continue
            for right_index in range(left_index + 1, len(groups)):
                right_bbox = _union_pixel_bbox(
                    [item["bbox"] for item in groups[right_index]]
                )
                if right_bbox is None:
                    continue
                distance = _gap_distance_between_pixel_bboxes(left_bbox, right_bbox)
                if best is None or distance < best[0]:
                    best = (distance, left_index, right_index)
        if best is None:
            break
        _distance, left_index, right_index = best
        groups[left_index].extend(groups[right_index])
        groups.pop(right_index)

    output: list[dict[str, Any]] = []
    for group in groups:
        part_boxes = _merge_close_pixel_bboxes(
            [item["bbox"] for item in group],
            gap=config.merge_gap_pixels,
            clip_bbox=content_bbox,
        )
        union_box = _union_pixel_bbox(part_boxes)
        if union_box is None:
            continue
        output.append(
            {
                "bbox_pixel": union_box,
                "parts_pixel": part_boxes,
                "center_pixel": [
                    (union_box[0] + union_box[2]) / 2.0,
                    (union_box[1] + union_box[3]) / 2.0,
                ],
                "red_pixel_count": sum(item["red_pixel_count"] for item in group),
                "grouping_method": "major_component_hierarchical_cluster",
            }
        )
    return output if len(output) == target_count else []


def _candidate_top_right_split_x(
    detection: dict[str, Any],
    width: int,
) -> int | None:
    buildings = [
        building
        for building in detection.get("buildings") or []
        if isinstance(building, dict) and isinstance(building.get("bbox_1000"), list)
    ]
    if len(buildings) < 3:
        return None
    buildings = sorted(
        buildings,
        key=lambda item: (
            float(item["bbox_1000"][0]) + float(item["bbox_1000"][2])
        )
        / 2.0,
    )
    second = buildings[1]["bbox_1000"]
    third = buildings[2]["bbox_1000"]
    values = [
        _safe_float(second[2]),
        _safe_float(third[0]),
    ]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return int(round(sum(values) / len(values) * width / 1000.0))


def _group_from_subregions(
    mask: Any,
    subregions: Sequence[Sequence[int]],
    *,
    content_bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
    method: str,
    merge_parts: bool = True,
) -> dict[str, Any] | None:
    part_boxes: list[list[int]] = []
    for subregion in subregions:
        clipped = _clip_pixel_bbox(subregion, content_bbox)
        if clipped is None:
            continue
        for part_box in _component_part_boxes(
            mask,
            clipped,
            content_bbox=content_bbox,
            config=config,
        ):
            clipped_part = _clip_pixel_bbox(part_box, clipped)
            if clipped_part is not None:
                part_boxes.append(clipped_part)
    if merge_parts:
        part_boxes = _merge_close_pixel_bboxes(
            part_boxes,
            gap=config.merge_gap_pixels,
            clip_bbox=content_bbox,
        )
    union_box = _union_pixel_bbox(part_boxes)
    if union_box is None:
        return None
    red_count = sum(_bbox_red_count(mask, part_box) for part_box in part_boxes)
    return {
        "bbox_pixel": union_box,
        "parts_pixel": part_boxes,
        "center_pixel": [
            (union_box[0] + union_box[2]) / 2.0,
            (union_box[1] + union_box[3]) / 2.0,
        ],
        "red_pixel_count": red_count,
        "grouping_method": method,
    }


def _three_region_l_shape_groups(
    mask: Any,
    detection: dict[str, Any],
    record: dict[str, Any],
    *,
    target_count: int,
    content_bbox: Sequence[int],
    config: BuildingVectorValidationConfig,
) -> list[dict[str, Any]]:
    if target_count != 3:
        return []
    root = _red_bbox_in_region(
        mask,
        content_bbox,
        clip_bbox=content_bbox,
        margin=max(config.part_margin_pixels * 2, 24),
    )
    if root is None:
        return []

    horizontal = _best_mask_axis_split(
        mask,
        root,
        config,
        axis="y",
        threshold_multiplier=1.8,
    )
    if horizontal is None:
        return []
    split_y, upper, lower = horizontal

    lower_vertical = _best_mask_axis_split(
        mask,
        lower,
        config,
        axis="x",
        threshold_multiplier=1.5,
    )
    if lower_vertical is None:
        return []
    lower_split_x, lower_left, lower_right = lower_vertical

    upper_vertical = _best_mask_axis_split(
        mask,
        upper,
        config,
        axis="x",
        threshold_multiplier=3.5,
    )
    if upper_vertical is not None:
        upper_split_x, upper_left, upper_right = upper_vertical
    else:
        candidate_x = _candidate_top_right_split_x(
            detection,
            int(record.get("image_width") or 0),
        )
        if candidate_x is None:
            return []
        candidate_x = max(int(upper[0]) + config.min_split_gap_pixels, candidate_x)
        candidate_x = min(int(upper[2]) - config.min_split_gap_pixels, candidate_x)
        upper_split_x = candidate_x
        upper_left = [int(upper[0]), int(upper[1]), upper_split_x, int(upper[3])]
        upper_right = [upper_split_x, int(upper[1]), int(upper[2]), int(upper[3])]

    if upper_split_x <= lower_split_x:
        upper_split_x = max(
            lower_split_x + config.min_split_gap_pixels,
            upper_split_x,
        )
        if upper_split_x >= int(upper[2]):
            return []
        upper_left = [int(upper[0]), int(upper[1]), upper_split_x, int(upper[3])]
        upper_right = [upper_split_x, int(upper[1]), int(upper[2]), int(upper[3])]

    method = "three_region_l_shape_density_split"
    groups = [
        _group_from_subregions(
            mask,
            [upper_left, lower_left],
            content_bbox=content_bbox,
            config=config,
            method=method,
            merge_parts=False,
        ),
        _group_from_subregions(
            mask,
            [upper_right],
            content_bbox=content_bbox,
            config=config,
            method=method,
        ),
        _group_from_subregions(
            mask,
            [lower_right],
            content_bbox=content_bbox,
            config=config,
            method=method,
        ),
    ]
    if any(group is None for group in groups):
        return []
    return [group for group in groups if group is not None]


def _sort_region_groups_for_labels(groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if not groups:
        return []
    heights = [float(group["bbox_pixel"][3]) - float(group["bbox_pixel"][1]) for group in groups]
    median_height = sorted(heights)[len(heights) // 2]
    row_tolerance = max(80.0, median_height * 0.45)

    remaining = sorted(groups, key=lambda item: (item["center_pixel"][1], item["center_pixel"][0]))
    rows: list[list[dict[str, Any]]] = []
    for group in remaining:
        center_y = float(group["center_pixel"][1])
        placed = False
        for row in rows:
            row_center = sum(float(item["center_pixel"][1]) for item in row) / len(row)
            if abs(center_y - row_center) <= row_tolerance:
                row.append(group)
                placed = True
                break
        if not placed:
            rows.append([group])

    ordered: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda items: min(float(item["center_pixel"][1]) for item in items)):
        ordered.extend(sorted(row, key=lambda item: float(item["center_pixel"][0])))
    return ordered


def _shapely_parts_from_pixel_boxes(
    record: dict[str, Any],
    boxes: Sequence[Sequence[float]],
) -> list[Any]:
    parts: list[Any] = []
    for pixel_box in boxes:
        cad_box = _pixel_bbox_to_cad_bbox(record, pixel_box)
        if cad_box is None:
            continue
        parts.append(box(*cad_box))
    return parts


def _vector_review_fallback(
    detection: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    updated = dict(detection)
    updated["validation_status"] = "vision_candidate_only"
    updated["vision_candidate_only"] = True
    updated["needs_review"] = True
    reasons = list(updated.get("review_reasons") or [])
    reasons.append(reason)
    updated["review_reasons"] = _unique_texts(reasons)
    return updated


def _refine_detection_with_vector_mask(
    record: dict[str, Any],
    detection: dict[str, Any],
    image_path: Path,
    *,
    floor_geometry: Any | None,
    corridor_blocking_geometry: Any | None = None,
    config: BuildingVectorValidationConfig,
) -> dict[str, Any]:
    if not config.enabled or detection.get("status") == "error":
        return detection
    try:
        mask = _red_obstacle_mask_from_image(image_path, record)
        import numpy as np  # noqa: F401 - used to validate optional dependency
    except Exception as exc:
        updated = dict(detection)
        updated["validation_status"] = "vision_candidate_only"
        updated["needs_review"] = True
        reasons = list(updated.get("review_reasons") or [])
        reasons.append(f"vector_validation_unavailable:{type(exc).__name__}")
        updated["review_reasons"] = _unique_texts(reasons)
        return updated

    target_count = int(
        detection.get("building_count_hint")
        or detection.get("building_count")
        or len(detection.get("buildings") or [])
        or 1
    )
    content_bbox = _content_pixel_bbox(record)
    groups: list[dict[str, Any]] = _three_region_l_shape_groups(
        mask,
        detection,
        record,
        target_count=target_count,
        content_bbox=content_bbox,
        config=config,
    )
    if not groups:
        groups = _major_component_cluster_groups(
            mask,
            target_count=target_count,
            content_bbox=content_bbox,
            config=config,
        )
    if not groups:
        group_bboxes = _split_mask_into_regions(
            mask,
            target_count=target_count,
            content_bbox=content_bbox,
            config=config,
        )
        if len(group_bboxes) != target_count:
            return _vector_review_fallback(
                detection,
                f"vector_split_count={len(group_bboxes)} differs from target_count={target_count}"
            )

        for group_bbox in group_bboxes:
            part_boxes = _component_part_boxes(
                mask,
                group_bbox,
                content_bbox=content_bbox,
                config=config,
            )
            if not part_boxes:
                continue
            union_box = _union_pixel_bbox(part_boxes)
            if union_box is None:
                continue
            red_count = sum(_bbox_red_count(mask, part_box) for part_box in part_boxes)
            groups.append(
                {
                    "bbox_pixel": union_box,
                    "parts_pixel": part_boxes,
                    "center_pixel": [
                        (union_box[0] + union_box[2]) / 2.0,
                        (union_box[1] + union_box[3]) / 2.0,
                    ],
                    "red_pixel_count": red_count,
                    "grouping_method": "projection_gap_split",
                }
            )
    if not (
        groups
        and all(
            group.get("grouping_method") == "three_region_l_shape_density_split"
            for group in groups
        )
    ):
        groups = _sort_region_groups_for_labels(groups)
    groups = _reassign_three_region_parts_to_candidate_roles(
        mask,
        groups,
        detection,
    )
    groups = _filter_group_parts_by_vision_candidates(
        mask,
        groups,
        detection,
        content_bbox=content_bbox,
        config=config,
    )
    if len(groups) != target_count:
        return _vector_review_fallback(
            detection,
            f"vector_part_count={len(groups)} differs from target_count={target_count}"
        )

    connected_groups: list[dict[str, Any]] = []
    for group_index, group in enumerate(groups, start=1):
        connected_group, connected = _connect_group_parts(
            group,
            content_bbox=content_bbox,
            config=config,
        )
        if not connected:
            return _vector_review_fallback(
                detection,
                f"building_group_{group_index}_remains_disconnected_after_gap_closing",
            )
        connected_groups.append(connected_group)
    groups = connected_groups
    if not _groups_have_legal_overlap(groups, config):
        return _vector_review_fallback(
            detection,
            "vector_groups_have_illegal_overlap_after_exclusive_assignment",
        )
    groups, corridor_connections = _detect_corridor_connections(
        groups,
        record,
        corridor_blocking_geometry
        if corridor_blocking_geometry is not None
        else floor_geometry,
        config,
    )

    width = int(record.get("image_width") or 0)
    height = int(record.get("image_height") or 0)
    labels = [str(value) for value in detection.get("building_labels_hint") or []]
    candidate_buildings = [
        building for building in detection.get("buildings") or [] if isinstance(building, dict)
    ]
    refined_buildings: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        candidate = candidate_buildings[index - 1] if index <= len(candidate_buildings) else {}
        part_polygons_pixel = [
            _pixel_bbox_to_polygon(part_box) for part_box in group["parts_pixel"]
        ]
        part_polygons_1000 = [
            _pixel_polygon_to_1000(polygon, width, height)
            for polygon in part_polygons_pixel
        ]
        part_polygons_cad = [
            _pixel_polygon_to_cad(record, polygon)
            for polygon in part_polygons_pixel
        ]
        owned_corridor_polygons_cad = [
            polygon
            for connection in corridor_connections
            if int(connection.get("left_index", -1)) == index - 1
            for polygon in connection.get("corridor_polygons_cad") or []
        ]
        planning_parts_cad = part_polygons_cad + owned_corridor_polygons_cad
        bbox_pixel = [round(float(value), 3) for value in group["bbox_pixel"]]
        polygon_pixel = _pixel_bbox_to_polygon(bbox_pixel)
        bbox_1000 = _pixel_bbox_to_1000(bbox_pixel, width, height)
        polygon_1000 = _pixel_polygon_to_1000(polygon_pixel, width, height)
        polygon_cad = _pixel_polygon_to_cad(record, polygon_pixel)
        bbox_cad = _bbox_from_points(polygon_cad)
        geometry_parts = _shapely_parts_from_pixel_boxes(record, group["parts_pixel"])
        geometry_union = (
            _valid_polygonal(unary_union(geometry_parts))
            if geometry_parts
            else GeometryCollection()
        )
        obstacle_area = 0.0
        density = 0.0
        if floor_geometry is not None and not geometry_union.is_empty:
            try:
                obstacle_area = float(floor_geometry.intersection(geometry_union).area)
                density = float(obstacle_area / geometry_union.area) if geometry_union.area else 0.0
            except Exception:
                obstacle_area = 0.0
                density = 0.0

        label_hint = labels[index - 1] if index <= len(labels) else ""
        refined = {
            **candidate,
            "building_id": f"B{index:02d}",
            "building_index": index,
            "name_text": str(candidate.get("name_text") or label_hint or "").strip(),
            "label_hint": label_hint,
            "vision_candidate": {
                "bbox_1000": candidate.get("bbox_1000"),
                "polygon_1000": candidate.get("polygon_1000"),
                "bbox_pixel": candidate.get("bbox_pixel"),
                "polygon_pixel": candidate.get("polygon_pixel"),
                "bbox_cad": candidate.get("bbox_cad"),
                "polygon_cad": candidate.get("polygon_cad"),
                "confidence": candidate.get("confidence"),
            },
            "bbox_1000": bbox_1000,
            "polygon_1000": polygon_1000,
            "bbox_pixel": bbox_pixel,
            "polygon_pixel": polygon_pixel,
            "bbox_cad": bbox_cad,
            "polygon_cad": polygon_cad,
            "parts_pixel": part_polygons_pixel,
            "parts_1000": part_polygons_1000,
            "parts_cad": planning_parts_cad,
            "structural_parts_cad": part_polygons_cad,
            "corridor_polygons_cad": owned_corridor_polygons_cad,
            "refined_geometry_type": geometry_union.geom_type,
            "red_pixel_count": group["red_pixel_count"],
            "vector_obstacle_area_cad_units2": obstacle_area,
            "vector_obstacle_density": round(density, 6),
            "validation_method": "red_obstacle_density_connectivity_split",
            "grouping_method": group.get("grouping_method"),
            "topology_connected": bool(group.get("topology_connected")),
            "rejected_candidate_external_parts": group.get(
                "rejected_candidate_external_parts", []
            ),
            "synthetic_bridge_boxes_pixel": group.get(
                "synthetic_bridge_boxes_pixel", []
            ),
            "wall_gap_repair_candidates": [
                {
                    "bbox_pixel": bridge,
                    "polygon_pixel": _pixel_bbox_to_polygon(bridge),
                    "polygon_cad": _pixel_polygon_to_cad(
                        record, _pixel_bbox_to_polygon(bridge)
                    ),
                    "reason": "building_skeleton_gap_closed_for_connected_region",
                }
                for bridge in group.get("synthetic_bridge_boxes_pixel") or []
            ],
            "corridor_connection_group": group.get("corridor_connection_group"),
            "connected_building_group_id": group.get("corridor_connection_group"),
            "boundary_quality": "vector_refined_candidate",
            "needs_review": bool(candidate.get("needs_review", False)),
        }
        refined_buildings.append(refined)

    updated = dict(detection)
    updated["buildings"] = refined_buildings
    updated["building_count"] = len(refined_buildings)
    updated["validation_status"] = "vector_validated"
    updated["validation_method"] = "red_obstacle_density_connectivity_split"
    updated["vision_candidate_only"] = False
    updated["status"] = "ok" if updated.get("status") != "error" else updated.get("status")
    updated["needs_review"] = any(building.get("needs_review") for building in refined_buildings)
    updated["review_reasons"] = _unique_texts([
        reason
        for reason in (updated.get("review_reasons") or [])
        if not str(reason).startswith("title_hint_count=")
    ])
    updated["vector_validation"] = {
        "target_count": target_count,
        "group_count": len(groups),
        "content_pixel_bbox": content_bbox,
        "total_red_pixels": int((mask > 0).sum()),
        "config": asdict(config),
        "all_buildings_connected": True,
        "exclusive_group_overlap_valid": True,
        "corridor_connections": corridor_connections,
    }
    updated["corridor_connections"] = corridor_connections
    return updated


def _attach_detection_paths(
    detection: dict[str, Any],
    *,
    response_path: Path,
    annotated_image_path: Path,
) -> dict[str, Any]:
    updated = dict(detection)
    updated["response_path"] = str(response_path.resolve())
    updated["annotated_image_path"] = str(annotated_image_path.resolve())
    return updated


_ANNOTATION_COLORS = (
    "#1473e6",
    "#18a058",
    "#f97316",
    "#9333ea",
    "#0f766e",
    "#dc2626",
)


def _hex_to_rgba(value: str, alpha: int) -> tuple[int, int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return 20, 115, 230, alpha
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), alpha


def _load_annotation_font(size: int) -> Any:
    candidates = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _draw_label(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: Any,
    color: tuple[int, int, int, int],
) -> None:
    x, y = position
    bbox = draw.textbbox((x, y), text, font=font)
    padding_x = 8
    padding_y = 5
    background = (
        bbox[0] - padding_x,
        bbox[1] - padding_y,
        bbox[2] + padding_x,
        bbox[3] + padding_y,
    )
    draw.rectangle(background, fill=color)
    draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)


def annotate_building_regions(
    record: dict[str, Any],
    detection: dict[str, Any],
    output_path: Path,
) -> Path:
    image_path = Path(str(record.get("image_path") or detection.get("source_image_path")))
    with Image.open(image_path) as source:
        image = source.convert("RGBA")

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_annotation_font(max(18, min(image.size) // 110))
    banner_font = _load_annotation_font(max(20, min(image.size) // 95))

    for index, building in enumerate(detection.get("buildings") or []):
        if not isinstance(building, dict):
            continue
        color = _hex_to_rgba(_ANNOTATION_COLORS[index % len(_ANNOTATION_COLORS)], 255)
        fill = (color[0], color[1], color[2], 42)
        raw_parts = building.get("parts_pixel")
        if not isinstance(raw_parts, list) or not raw_parts:
            raw_parts = [building.get("polygon_pixel")]
        drawn_parts: list[list[tuple[int, int]]] = []
        for polygon in raw_parts:
            if not isinstance(polygon, list) or len(polygon) < 3:
                continue
            polygon_points = [
                (int(round(float(point[0]))), int(round(float(point[1]))))
                for point in polygon
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            if len(polygon_points) >= 3:
                drawn_parts.append(polygon_points)
        if not drawn_parts:
            bbox = building.get("bbox_pixel")
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            left, top, right, bottom = [int(round(float(value))) for value in bbox]
            drawn_parts = [[(left, top), (right, top), (right, bottom), (left, bottom)]]

        all_x = [point[0] for polygon in drawn_parts for point in polygon]
        all_y = [point[1] for polygon in drawn_parts for point in polygon]
        left, top, right, bottom = min(all_x), min(all_y), max(all_x), max(all_y)
        line_width = max(4, min(image.size) // 800)
        for polygon_points in drawn_parts:
            draw.polygon(polygon_points, fill=fill)
            draw.line(
                polygon_points + [polygon_points[0]],
                fill=color,
                width=line_width,
            )
        if len(drawn_parts) == 1:
            draw.rectangle(
                (left, top, right, bottom),
                outline=(color[0], color[1], color[2], 120),
                width=max(2, min(image.size) // 1400),
            )
        label_bits = [
            str(building.get("building_id") or f"B{index + 1:02d}"),
            str(building.get("name_text") or building.get("label_hint") or "").strip(),
        ]
        confidence = _safe_float(building.get("confidence"))
        if confidence is not None and confidence > 0:
            label_bits.append(f"{confidence:.2f}")
        label = " | ".join(bit for bit in label_bits if bit)
        label_x = max(4, min(left + 10, image.width - 20))
        text_bbox = draw.textbbox((label_x, top), label, font=font)
        label_y = max(6, top - (text_bbox[3] - text_bbox[1]) - 14)
        _draw_label(draw, (label_x, label_y), label, font, color)

    hint = detection.get("building_count_hint")
    banner = (
        f"Building regions: {detection.get('building_count', 0)}"
        f" | title hint: {hint if hint is not None else 'unknown'}"
        f" | status: {detection.get('validation_status') or detection.get('status', '')}"
    )
    banner_bbox = draw.textbbox((12, 12), banner, font=banner_font)
    draw.rectangle(
        (0, 0, image.width, banner_bbox[3] + 22),
        fill=(0, 0, 0, 160),
    )
    draw.text((12, 12), banner, fill=(255, 255, 255, 255), font=banner_font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(image, overlay).convert("RGB").save(
        output_path,
        format="PNG",
        optimize=True,
    )
    return output_path.resolve()


def _flatten_building_regions(
    detections: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for detection in detections:
        for building in detection.get("buildings") or []:
            if not isinstance(building, dict):
                continue
            regions.append(
                {
                    "image_id": detection.get("image_id"),
                    "sheet_id": detection.get("sheet_id"),
                    "floor_id": detection.get("floor_id"),
                    "floor_name": detection.get("floor_name"),
                    "sheet_title": detection.get("sheet_title"),
                    "region_id": detection.get("region_id"),
                    "full_region_id": detection.get("full_region_id"),
                    "building_id": building.get("building_id"),
                    "building_index": building.get("building_index"),
                    "name_text": building.get("name_text"),
                    "label_hint": building.get("label_hint"),
                    "confidence": building.get("confidence"),
                    "boundary_quality": building.get("boundary_quality"),
                    "needs_review": building.get("needs_review"),
                    "bbox_1000": building.get("bbox_1000"),
                    "polygon_1000": building.get("polygon_1000"),
                    "bbox_pixel": building.get("bbox_pixel"),
                    "polygon_pixel": building.get("polygon_pixel"),
                    "bbox_cad": building.get("bbox_cad"),
                    "polygon_cad": building.get("polygon_cad"),
                    "parts_1000": building.get("parts_1000"),
                    "parts_pixel": building.get("parts_pixel"),
                    "parts_cad": building.get("parts_cad"),
                    "structural_parts_cad": building.get("structural_parts_cad"),
                    "corridor_polygons_cad": building.get("corridor_polygons_cad"),
                    "corridor_connection_group": building.get(
                        "corridor_connection_group"
                    ),
                    "connected_building_group_id": building.get(
                        "connected_building_group_id"
                    ),
                    "topology_connected": building.get("topology_connected"),
                    "synthetic_bridge_boxes_pixel": building.get(
                        "synthetic_bridge_boxes_pixel"
                    ),
                    "wall_gap_repair_candidates": building.get(
                        "wall_gap_repair_candidates"
                    ),
                    "rejected_candidate_external_parts": building.get(
                        "rejected_candidate_external_parts"
                    ),
                    "vision_candidate": building.get("vision_candidate"),
                    "validation_method": building.get("validation_method"),
                    "refined_geometry_type": building.get("refined_geometry_type"),
                    "red_pixel_count": building.get("red_pixel_count"),
                    "vector_obstacle_area_cad_units2": building.get(
                        "vector_obstacle_area_cad_units2"
                    ),
                    "vector_obstacle_density": building.get("vector_obstacle_density"),
                    "evidence": building.get("evidence"),
                    "uncertainty_reason": building.get("uncertainty_reason"),
                    "source_image_path": detection.get("source_image_path"),
                    "annotated_image_path": detection.get("annotated_image_path"),
                    "response_path": detection.get("response_path"),
                    "model": detection.get("model"),
                }
            )
    return regions


def _json_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _write_building_regions_csv(path: Path, regions: Sequence[dict[str, Any]]) -> None:
    fields = [
        "image_id",
        "sheet_id",
        "floor_id",
        "floor_name",
        "sheet_title",
        "region_id",
        "full_region_id",
        "building_id",
        "building_index",
        "name_text",
        "label_hint",
        "confidence",
        "boundary_quality",
        "needs_review",
        "bbox_1000",
        "polygon_1000",
        "bbox_pixel",
        "polygon_pixel",
        "bbox_cad",
        "polygon_cad",
        "parts_1000",
        "parts_pixel",
        "parts_cad",
        "structural_parts_cad",
        "corridor_polygons_cad",
        "corridor_connection_group",
        "connected_building_group_id",
        "topology_connected",
        "synthetic_bridge_boxes_pixel",
        "wall_gap_repair_candidates",
        "rejected_candidate_external_parts",
        "vision_candidate",
        "validation_method",
        "refined_geometry_type",
        "red_pixel_count",
        "vector_obstacle_area_cad_units2",
        "vector_obstacle_density",
        "source_image_path",
        "annotated_image_path",
        "response_path",
        "model",
        "evidence",
        "uncertainty_reason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for region in regions:
            writer.writerow({field: _json_cell(region.get(field)) for field in fields})


def _merge_building_regions_into_sheets(
    sheets_payload: dict[str, Any],
    detections: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    merged = json.loads(json.dumps(sheets_payload, ensure_ascii=False))
    detections_by_region = {
        str(detection.get("full_region_id") or ""): detection
        for detection in detections
        if detection.get("full_region_id")
    }
    raw_sheets = merged.get("sheets")
    if not isinstance(raw_sheets, list):
        raw_sheets = []

    for sheet in raw_sheets:
        if not isinstance(sheet, dict):
            continue
        sheet_regions: list[dict[str, Any]] = []
        inspection_regions = sheet.get("inspection_regions")
        if not isinstance(inspection_regions, list):
            inspection_regions = []
        for region in inspection_regions:
            if not isinstance(region, dict):
                continue
            full_region_id = (
                f"{sheet.get('sheet_id')}:{region.get('region_id')}"
            )
            detection = detections_by_region.get(full_region_id)
            if not detection:
                continue
            building_regions = []
            for building in detection.get("buildings") or []:
                if not isinstance(building, dict):
                    continue
                source_floor_id = str(sheet.get("floor_id") or "UNKNOWN")
                building_id = str(building.get("building_id") or "")
                detected_connection_group = str(
                    building.get("connected_building_group_id")
                    or building.get("building_connection_group")
                    or building.get("corridor_connection_group")
                    or ""
                ).strip()
                connection_verification = str(
                    building.get("corridor_connection_verification")
                    or building.get("connection_verification_status")
                    or ""
                ).strip().lower()
                trusted_connection = bool(
                    detected_connection_group
                    and building.get("corridor_connection_verified") is True
                    and connection_verification
                    in {"manual_verified", "cad_portal_verified", "door_portal_verified"}
                )
                connection_group = detected_connection_group if trusted_connection else ""
                building_scope_id = (
                    f"{source_floor_id}__CONN_{connection_group}"
                    if connection_group
                    else f"{source_floor_id}__{building_id or 'B00'}"
                )
                item = {
                    "building_id": building_id,
                    "building_index": building.get("building_index"),
                    "source_floor_id": source_floor_id,
                    "building_scope_id": building_scope_id,
                    "name_text": building.get("name_text"),
                    "label_hint": building.get("label_hint"),
                    "confidence": building.get("confidence"),
                    "bbox": building.get("bbox_cad"),
                    "polygon": building.get("polygon_cad"),
                    # Planning isolation uses structural building parts only.
                    # A visually inferred corridor must never enlarge or merge
                    # navigation domains without an independent trusted check.
                    "parts": building.get("structural_parts_cad") or building.get("parts_cad"),
                    "structural_parts": building.get("structural_parts_cad"),
                    "corridor_polygons": (
                        building.get("corridor_polygons_cad") if trusted_connection else []
                    ),
                    "detected_corridor_polygons": building.get("corridor_polygons_cad"),
                    "corridor_connection_group": connection_group,
                    "connected_building_group_id": connection_group,
                    "detected_corridor_connection_group": detected_connection_group,
                    "corridor_connection_verified": trusted_connection,
                    "corridor_connection_verification": connection_verification,
                    "detected_connection_rejected_for_planning": bool(
                        detected_connection_group and not trusted_connection
                    ),
                    "bbox_1000": building.get("bbox_1000"),
                    "polygon_1000": building.get("polygon_1000"),
                    "parts_1000": building.get("parts_1000"),
                    "needs_review": building.get("needs_review"),
                    "source": (
                        "stage_05A_obstacles_ark_vision_vector_validated"
                        if detection.get("validation_status") == "vector_validated"
                        else "stage_05A_obstacles_ark_vision_candidate"
                    ),
                    "validation_method": building.get("validation_method"),
                    "vision_candidate": building.get("vision_candidate"),
                    "vector_obstacle_density": building.get(
                        "vector_obstacle_density"
                    ),
                    "source_image_id": detection.get("image_id"),
                    "annotated_image_path": detection.get("annotated_image_path"),
                }
                building_regions.append(item)
                sheet_regions.append(item)
            region["building_regions"] = building_regions
            region["building_region_detection"] = {
                "schema_version": BUILDING_VISION_SCHEMA_VERSION,
                "status": detection.get("status"),
                "building_count": detection.get("building_count"),
                "building_count_hint": detection.get("building_count_hint"),
                "building_labels_hint": detection.get("building_labels_hint"),
                "needs_review": detection.get("needs_review"),
                "review_reasons": detection.get("review_reasons"),
                "validation_status": detection.get("validation_status"),
                "validation_method": detection.get("validation_method"),
                "source_image_path": detection.get("source_image_path"),
                "annotated_image_path": detection.get("annotated_image_path"),
                "response_path": detection.get("response_path"),
                "corridor_connections": detection.get("corridor_connections") or [],
            }

        if sheet_regions:
            sheet["building_regions"] = sheet_regions
            sheet["building_region_count"] = len(sheet_regions)
            sheet["building_region_detection_status"] = (
                "needs_review"
                if any(
                    isinstance(region, dict)
                    and isinstance(region.get("building_region_detection"), dict)
                    and region["building_region_detection"].get("needs_review")
                    for region in inspection_regions
                )
                else "ok"
            )

    merged["building_region_recognition"] = {
        "schema_version": BUILDING_VISION_SCHEMA_VERSION,
        "stage": "stage_05A_obstacles",
        "image_count": len(detections),
        "building_region_count": sum(
            int(detection.get("building_count") or 0) for detection in detections
        ),
        "analysis_scope_policy": {
            "default": "same_floor_same_building",
            "scope_field": "building_scope_id",
            "single_building_floor": "use_floor_region_without_vision_call",
            "unvalidated_building_detection": "use_candidate_regions_for_isolation_only",
            "first_floor_exception": False,
            "cross_building_merge": "trusted_manual_or_cad_portal_verification_only",
            "verified_corridor_connection_fields": [
                "connected_building_group_id",
                "building_connection_group",
                "corridor_connection_group",
            ],
        },
        "needs_review_count": sum(
            1 for detection in detections if detection.get("needs_review")
        ),
    }
    return merged


def _is_third_floor_or_above(floor_id: Any) -> bool:
    text = str(floor_id or "").strip().upper()
    match = re.fullmatch(r"F(\d+)", text)
    if match:
        return int(match.group(1)) >= 3
    return text in {"ROOF", "EQUIPMENT", "RF", "屋面", "设备层"}


def _polygon_from_ring(raw_ring: Any) -> Any | None:
    if not isinstance(raw_ring, list) or len(raw_ring) < 3:
        return None
    try:
        points = [
            (float(point[0]), float(point[1]))
            for point in raw_ring
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        polygon = Polygon(points) if len(points) >= 3 else None
        if polygon is not None and not polygon.is_valid:
            polygon = polygon.buffer(0)
        return polygon if polygon is not None and not polygon.is_empty and polygon.area > 0 else None
    except Exception:
        return None


def _nearest_red_wall_offset(
    mask: Any,
    start: tuple[float, float],
    end: tuple[float, float],
    inward: tuple[int, int],
    max_depth: int,
) -> dict[str, Any]:
    """Find the dominant nearest red-wall offset along one outer boundary edge."""
    import numpy as np

    height, width = mask.shape[:2]
    x0, y0 = start
    x1, y1 = end
    horizontal = abs(y1 - y0) <= abs(x1 - x0)
    edge_length = abs(x1 - x0) if horizontal else abs(y1 - y0)
    margin = min(20, max(2, int(edge_length * 0.025)))
    step = max(1, int(edge_length // 1600))
    depth_values = np.arange(0, max_depth + 1, dtype=int)
    if horizontal:
        left = max(0, int(round(min(x0, x1))) + margin)
        right = min(width - 1, int(round(max(x0, x1))) - margin)
        samples = np.arange(left, right + 1, step, dtype=int)
        base = int(round((y0 + y1) / 2.0))
        scan = base + int(inward[1]) * depth_values
        valid = (scan >= 0) & (scan < height)
        scan = scan[valid]
        depth_values = depth_values[valid]
        if samples.size == 0 or scan.size == 0:
            return {"offset_pixels": 0.0, "support_fraction": 0.0, "snapped": False}
        values = mask[scan[:, None], samples[None, :]] > 0
    else:
        top = max(0, int(round(min(y0, y1))) + margin)
        bottom = min(height - 1, int(round(max(y0, y1))) - margin)
        samples = np.arange(top, bottom + 1, step, dtype=int)
        base = int(round((x0 + x1) / 2.0))
        scan = base + int(inward[0]) * depth_values
        valid = (scan >= 0) & (scan < width)
        scan = scan[valid]
        depth_values = depth_values[valid]
        if samples.size == 0 or scan.size == 0:
            return {"offset_pixels": 0.0, "support_fraction": 0.0, "snapped": False}
        values = mask[samples[:, None], scan[None, :]].T > 0
    hit_columns = values.any(axis=0)
    if not bool(hit_columns.any()):
        return {"offset_pixels": 0.0, "support_fraction": 0.0, "snapped": False}
    nearest_indices = values[:, hit_columns].argmax(axis=0)
    nearest_depths = depth_values[nearest_indices]
    counts = np.bincount(nearest_depths, minlength=max_depth + 1).astype(float)
    smoothed = np.convolve(counts, np.ones(7, dtype=float), mode="same")
    dominant = int(smoothed.argmax())
    tolerance = 4
    supported = int((abs(nearest_depths - dominant) <= tolerance).sum())
    support_fraction = supported / max(1, int(samples.size))
    snapped = support_fraction >= 0.04
    return {
        "offset_pixels": float(dominant if snapped else 0.0),
        "candidate_offset_pixels": float(dominant),
        "support_fraction": round(float(support_fraction), 6),
        "sample_count": int(samples.size),
        "supported_sample_count": supported,
        "snapped": snapped,
    }


def _snapped_outer_wall_completion(
    record: dict[str, Any],
    mask: Any,
    structural_domain: Any,
) -> tuple[Any, dict[str, Any]]:
    """Snap a closed visual building outline inward onto the actual red outer wall."""
    cad_bbox = tuple(float(value) for value in record.get("cad_bbox") or [])
    transform = record.get("transform") or {}
    if len(cad_bbox) != 4 or not transform:
        return GeometryCollection(), {"status": "missing_render_transform"}
    cad_per_pixel = float(transform.get("cad_units_per_pixel") or 0.0)
    if cad_per_pixel <= 0:
        return GeometryCollection(), {"status": "invalid_render_scale"}
    completed_rings: list[Any] = []
    segment_audits: list[dict[str, Any]] = []
    snapped_segment_count = 0
    inferred_segment_count = 0
    visual_fallback_segment_count = 0
    segment_count = 0
    for polygon_index, polygon in enumerate(_polygonal_parts(structural_domain), start=1):
        cad_points = list(polygon.exterior.coords)[:-1]
        pixel_points = [
            _point_to_pixel(point, cad_bbox, transform)
            for point in cad_points
        ]
        if len(pixel_points) < 3:
            continue
        pixel_polygon = Polygon(pixel_points)
        if not pixel_polygon.is_valid:
            pixel_polygon = pixel_polygon.buffer(0)
        short_span = min(
            pixel_polygon.bounds[2] - pixel_polygon.bounds[0],
            pixel_polygon.bounds[3] - pixel_polygon.bounds[1],
        )
        max_depth = max(24, min(650, int(round(short_span * 0.18))))
        shifted_segments: list[dict[str, Any]] = []
        for index, start in enumerate(pixel_points):
            end = pixel_points[(index + 1) % len(pixel_points)]
            dx = float(end[0] - start[0])
            dy = float(end[1] - start[1])
            horizontal = abs(dy) <= abs(dx)
            midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
            if horizontal:
                candidates = [(0, 1), (0, -1)]
            else:
                candidates = [(1, 0), (-1, 0)]
            inward = next(
                (
                    normal
                    for normal in candidates
                    if pixel_polygon.covers(
                        Point(midpoint[0] + normal[0] * 3.0, midpoint[1] + normal[1] * 3.0)
                    )
                ),
                candidates[0],
            )
            support = _nearest_red_wall_offset(mask, start, end, inward, max_depth)
            minimum_inference_support = max(
                12,
                int(math.ceil(float(support.get("sample_count") or 0) * 0.015)),
            )
            if support["snapped"]:
                offset = float(support["offset_pixels"])
                placement_method = "dominant_red_wall_snap"
                snapped_segment_count += 1
            elif int(support.get("supported_sample_count") or 0) >= minimum_inference_support:
                offset = float(support.get("candidate_offset_pixels") or 0.0)
                placement_method = "low_support_red_wall_offset_inference"
                inferred_segment_count += 1
            else:
                offset = 0.0
                placement_method = "visual_boundary_no_red_evidence_fallback"
                visual_fallback_segment_count += 1
            if horizontal:
                shifted = {
                    "orientation": "horizontal",
                    "coordinate": (start[1] + end[1]) / 2.0 + inward[1] * offset,
                }
            else:
                shifted = {
                    "orientation": "vertical",
                    "coordinate": (start[0] + end[0]) / 2.0 + inward[0] * offset,
                }
            shifted_segments.append(shifted)
            segment_count += 1
            segment_audits.append({
                "polygon_index": polygon_index,
                "segment_index": index + 1,
                "start_pixel": [float(start[0]), float(start[1])],
                "end_pixel": [float(end[0]), float(end[1])],
                "inward_normal": list(inward),
                "placement_method": placement_method,
                "applied_offset_pixels": offset,
                "minimum_inference_support": minimum_inference_support,
                **support,
            })
        adjusted_points: list[list[float]] = []
        for index, original in enumerate(pixel_points):
            previous = shifted_segments[index - 1]
            current = shifted_segments[index]
            if previous["orientation"] == "horizontal" and current["orientation"] == "vertical":
                adjusted = [current["coordinate"], previous["coordinate"]]
            elif previous["orientation"] == "vertical" and current["orientation"] == "horizontal":
                adjusted = [previous["coordinate"], current["coordinate"]]
            else:
                adjusted = [float(original[0]), float(original[1])]
            adjusted_points.append(adjusted)
        adjusted_cad = _pixel_polygon_to_cad(record, adjusted_points)
        adjusted_polygon = _polygon_from_ring(adjusted_cad)
        if adjusted_polygon is None:
            continue
        half_wall_width = cad_per_pixel * 3.5
        completion = adjusted_polygon.boundary.buffer(
            half_wall_width,
            cap_style=2,
            join_style=2,
        )
        if not completion.is_valid:
            completion = completion.buffer(0)
        if not completion.is_empty:
            completed_rings.append(completion)
    if not completed_rings:
        return GeometryCollection(), {
            "status": "no_completed_ring",
            "segment_count": segment_count,
            "snapped_segment_count": snapped_segment_count,
            "inferred_segment_count": inferred_segment_count,
            "visual_boundary_fallback_segment_count": visual_fallback_segment_count,
            "segments": segment_audits,
        }
    completion_union = unary_union(completed_rings)
    if not completion_union.is_valid:
        completion_union = completion_union.buffer(0)
    return completion_union, {
        "status": "snapped_closed_wall_materialized",
        "segment_count": segment_count,
        "snapped_segment_count": snapped_segment_count,
        "inferred_red_wall_segment_count": inferred_segment_count,
        "fallback_visual_boundary_segment_count": visual_fallback_segment_count,
        "wall_width_pixels": 7.0,
        "wall_width_cad_units": cad_per_pixel * 7.0,
        "segments": segment_audits,
    }


def annotate_upper_floor_building_envelope_obstacles(
    manifest_json: Path | str,
    envelope_geojson: Path | str,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Overlay newly completed envelope obstacles on the Stage 05A review PNGs."""
    manifest_path = Path(manifest_json).resolve()
    envelope_path = Path(envelope_geojson).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = _read_json(manifest_path)
    envelope_payload = _read_json(envelope_path)
    records = {
        str(record.get("floor_id") or ""): record
        for record in manifest.get("images", []) or []
        if isinstance(record, dict) and record.get("floor_id")
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for feature in envelope_payload.get("features", []) or []:
        if not isinstance(feature, dict) or not feature.get("geometry"):
            continue
        properties = feature.get("properties") or {}
        source_floor_id = str(properties.get("source_floor_id") or "")
        if source_floor_id:
            grouped.setdefault(source_floor_id, []).append(feature)

    image_paths: list[Path] = []
    floor_rows: list[dict[str, Any]] = []
    fill_colors = [
        (0, 183, 255, 105),
        (0, 230, 170, 105),
        (145, 80, 255, 105),
    ]
    outline_colors = [
        (0, 88, 255, 255),
        (0, 145, 92, 255),
        (95, 30, 220, 255),
    ]
    for source_floor_id, features in sorted(grouped.items()):
        record = records.get(source_floor_id)
        if not record:
            floor_rows.append({
                "source_floor_id": source_floor_id,
                "status": "skipped_missing_manifest_record",
            })
            continue
        image_path = Path(str(record.get("image_path") or ""))
        if not image_path.is_file():
            floor_rows.append({
                "source_floor_id": source_floor_id,
                "status": "skipped_missing_source_image",
                "source_image": str(image_path),
            })
            continue
        cad_bbox = tuple(float(value) for value in record.get("cad_bbox") or [])
        transform = record.get("transform") or {}
        if len(cad_bbox) != 4 or not transform:
            continue
        with Image.open(image_path) as source:
            image = source.convert("RGBA")
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = _load_annotation_font(max(18, min(image.size) // 105))
        banner_font = _load_annotation_font(max(20, min(image.size) // 92))
        line_width = max(5, min(image.size) // 650)
        for index, feature in enumerate(features):
            properties = feature.get("properties") or {}
            geometry = shape(feature["geometry"])
            fill = fill_colors[index % len(fill_colors)]
            outline = outline_colors[index % len(outline_colors)]
            for exterior, holes in _iter_polygon_pixels(geometry, cad_bbox, transform):
                draw.polygon(exterior, fill=fill)
                draw.line(exterior + [exterior[0]], fill=outline, width=line_width)
                for hole in holes:
                    if len(hole) >= 3:
                        draw.polygon(hole, fill=(0, 0, 0, 0))
                        draw.line(hole + [hole[0]], fill=outline, width=line_width)
            point = geometry.representative_point()
            label_point = _point_to_pixel((point.x, point.y), cad_bbox, transform)
            label = (
                f"NEW CLOSED WALL | {properties.get('building_scope_id', '')}"
                f" | W={float(properties.get('wall_width') or properties.get('guard_width') or 0.0):.1f}"
            )
            _draw_label(
                draw,
                (max(6, label_point[0]), max(55, label_point[1])),
                label,
                font,
                outline,
            )
        banner = (
            f"{source_floor_id} | RED: existing obstacles | "
            f"CYAN/GREEN/PURPLE: newly completed closed building boundary | "
            f"count={len(features)}"
        )
        banner_bbox = draw.textbbox((12, 12), banner, font=banner_font)
        draw.rectangle((0, 0, image.width, banner_bbox[3] + 24), fill=(0, 0, 0, 190))
        draw.text((12, 12), banner, fill=(255, 255, 255, 255), font=banner_font)
        output_path = output / f"{source_floor_id}_upper_floor_closed_boundary_completion.png"
        Image.alpha_composite(image, overlay).convert("RGB").save(
            output_path,
            format="PNG",
            optimize=True,
        )
        image_paths.append(output_path.resolve())
        floor_rows.append({
            "source_floor_id": source_floor_id,
            "status": "written",
            "building_scope_count": len(features),
            "source_image": str(image_path.resolve()),
            "annotated_image": str(output_path.resolve()),
        })

    overview_path: Path | None = None
    if image_paths:
        thumbnails: list[tuple[Path, Image.Image]] = []
        target_width = 1200
        for path in image_paths:
            with Image.open(path) as source:
                thumb = source.convert("RGB")
            scale = min(1.0, target_width / max(1, thumb.width))
            thumb = thumb.resize(
                (max(1, int(thumb.width * scale)), max(1, int(thumb.height * scale))),
                Image.Resampling.LANCZOS,
            )
            thumbnails.append((path, thumb))
        columns = 2
        gap = 24
        rows = math.ceil(len(thumbnails) / columns)
        row_heights = [
            max((thumbnails[index][1].height for index in range(row * columns, min((row + 1) * columns, len(thumbnails)))), default=0)
            for row in range(rows)
        ]
        canvas_width = target_width * columns + gap * (columns + 1)
        canvas_height = sum(row_heights) + gap * (rows + 1)
        canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
        y = gap
        for row in range(rows):
            x = gap
            for index in range(row * columns, min((row + 1) * columns, len(thumbnails))):
                thumb = thumbnails[index][1]
                canvas.paste(thumb, (x, y))
                x += target_width + gap
            y += row_heights[row] + gap
        overview_path = output / "upper_floor_closed_boundary_completion_overview.png"
        canvas.save(overview_path, format="PNG", optimize=True)

    index_path = output / "upper_floor_closed_boundary_completion_review_index.json"
    _write_json(index_path, {
        "schema_version": 1,
        "source_manifest": str(manifest_path),
        "source_envelope_geojson": str(envelope_path),
        "legend": {
            "red": "existing Stage 05 recognized obstacle",
            "cyan_green_purple": "new Stage 05A closed building-boundary completion",
        },
        "annotated_image_count": len(image_paths),
        "annotated_images": [str(path) for path in image_paths],
        "overview_image": str(overview_path.resolve()) if overview_path else "",
        "floors": floor_rows,
    })
    return {
        "image_paths": image_paths,
        "overview_path": overview_path.resolve() if overview_path else None,
        "index_path": index_path.resolve(),
    }


def write_upper_floor_building_envelope_obstacles(
    sheets_with_buildings_json: Path | str,
    run_dir: Path | str,
    *,
    minimum_floor: int = 3,
    inward_guard_width_ratio: float = 0.006,
) -> dict[str, Any]:
    """Materialize a closed exterior-wall obstacle for every upper-floor building.

    The visual building domain selects the outer contour.  Each contour edge is
    then searched inward and snapped to the dominant rendered red exterior wall;
    the snapped contour is closed even where the source wall contains gaps.  A
    low-evidence edge conservatively stays on the visual boundary, so completion
    cannot create a new routable opening or join two independent buildings.
    """
    if minimum_floor != 3:
        raise ValueError("Only the audited F3-and-above envelope policy is supported")
    if not 0.0 < inward_guard_width_ratio < 0.05:
        raise ValueError("inward_guard_width_ratio must be between 0 and 0.05")
    sheets_path = Path(sheets_with_buildings_json).resolve()
    output_dir = Path(run_dir).resolve() / "obstacles" / "building_envelope_completion"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = _read_json(sheets_path)
    manifest_path = (
        Path(run_dir).resolve()
        / "obstacle_building_region_render"
        / MANIFEST_JSON
    )
    manifest_payload = _read_json(manifest_path) if manifest_path.is_file() else {}
    render_records = {
        str(record.get("floor_id") or ""): record
        for record in manifest_payload.get("images", []) or []
        if isinstance(record, dict) and record.get("floor_id")
    }
    red_masks: dict[str, Any] = {}
    features: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    seen_scopes: set[str] = set()
    for sheet in payload.get("sheets", []) or []:
        if not isinstance(sheet, dict):
            continue
        source_floor_id = str(sheet.get("floor_id") or "").strip()
        if not _is_third_floor_or_above(source_floor_id):
            continue
        inspection_regions = sheet.get("inspection_regions") or []
        for region in inspection_regions:
            if not isinstance(region, dict):
                continue
            detection = region.get("building_region_detection") or {}
            if str(detection.get("status") or "").lower() == "error":
                continue
            for building in region.get("building_regions") or []:
                if not isinstance(building, dict):
                    continue
                scope_id = str(building.get("building_scope_id") or "").strip()
                if not scope_id or scope_id in seen_scopes:
                    continue
                raw_parts = building.get("structural_parts") or building.get("parts") or []
                polygons = [
                    polygon
                    for raw_ring in raw_parts
                    if (polygon := _polygon_from_ring(raw_ring)) is not None
                ]
                if not polygons:
                    polygon = _polygon_from_ring(building.get("polygon"))
                    polygons = [polygon] if polygon is not None else []
                if not polygons:
                    audits.append({
                        "floor_id": source_floor_id,
                        "building_scope_id": scope_id,
                        "status": "skipped_missing_polygon",
                    })
                    continue
                structural_domain = unary_union(polygons)
                if not structural_domain.is_valid:
                    structural_domain = structural_domain.buffer(0)
                minx, miny, maxx, maxy = structural_domain.bounds
                short_span = min(maxx - minx, maxy - miny)
                if short_span <= 0:
                    continue
                guard_width = short_span * inward_guard_width_ratio
                coarse_envelope = structural_domain.boundary.buffer(
                    guard_width,
                    cap_style=2,
                    join_style=2,
                ).intersection(structural_domain)
                snap_audit: dict[str, Any] = {"status": "render_record_unavailable"}
                envelope = GeometryCollection()
                record = render_records.get(source_floor_id)
                if record is not None:
                    try:
                        if source_floor_id not in red_masks:
                            image_path = Path(str(record.get("image_path") or ""))
                            red_masks[source_floor_id] = _red_obstacle_mask_from_image(
                                image_path,
                                record,
                            )
                        envelope, snap_audit = _snapped_outer_wall_completion(
                            record,
                            red_masks[source_floor_id],
                            structural_domain,
                        )
                    except Exception as exc:
                        envelope = GeometryCollection()
                        snap_audit = {
                            "status": "snap_failed_using_visual_boundary_fallback",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                if envelope.is_empty:
                    envelope = coarse_envelope
                    snap_audit.setdefault("fallback", "coarse_visual_building_boundary")
                if not envelope.is_valid:
                    envelope = envelope.buffer(0)
                if envelope.is_empty or envelope.area <= 0:
                    continue
                seen_scopes.add(scope_id)
                validation_status = str(detection.get("validation_status") or "")
                feature_properties = {
                    "floor_id": scope_id,
                    "source_floor_id": source_floor_id,
                    "building_scope_id": scope_id,
                    "building_id": str(building.get("building_id") or ""),
                    "kind": "snapped_closed_upper_floor_wall_completion",
                    "obstacle_count": 1,
                    "minimum_floor": minimum_floor,
                    "guard_width": guard_width,
                    "guard_width_ratio": inward_guard_width_ratio,
                    "wall_width": float(snap_audit.get("wall_width_cad_units") or guard_width * 2.0),
                    "outer_boundary_segment_count": int(snap_audit.get("segment_count") or 0),
                    "snapped_outer_wall_segment_count": int(snap_audit.get("snapped_segment_count") or 0),
                    "inferred_outer_wall_segment_count": int(snap_audit.get("inferred_red_wall_segment_count") or 0),
                    "fallback_visual_boundary_segment_count": int(snap_audit.get("fallback_visual_boundary_segment_count") or 0),
                    "vision_validation_status": validation_status,
                    "source": "stage_05A_visual_outer_region_snapped_wall_completion",
                }
                features.append({
                    "type": "Feature",
                    "properties": feature_properties,
                    "geometry": mapping(envelope),
                })
                audits.append({
                    **feature_properties,
                    "status": "materialized",
                    "structural_domain_area": float(structural_domain.area),
                    "envelope_obstacle_area": float(envelope.area),
                    "closed_boundary": bool(structural_domain.boundary.is_closed),
                    "outer_wall_snap_audit": snap_audit,
                })
    geojson_path = output_dir / BUILDING_ENVELOPE_OBSTACLES
    audit_path = output_dir / BUILDING_ENVELOPE_AUDIT
    _write_json(geojson_path, {"type": "FeatureCollection", "features": features})
    annotation_result: dict[str, Any] = {
        "image_paths": [],
        "overview_path": None,
        "index_path": None,
    }
    if manifest_path.is_file() and features:
        annotation_result = annotate_upper_floor_building_envelope_obstacles(
            manifest_path,
            geojson_path,
            output_dir / "annotated",
        )
    _write_json(audit_path, {
        "schema_version": 1,
        "policy": "F3_and_above_visual_outer_region_selects_and_snaps_to_the_actual_red_outer_wall_then_closes_all_gaps",
        "source_sheets_with_buildings": str(sheets_path),
        "minimum_floor": minimum_floor,
        "inward_guard_width_ratio": inward_guard_width_ratio,
        "building_envelope_obstacle_count": len(features),
        "cross_building_geometry_added": False,
        "features": audits,
        "output_geojson": str(geojson_path.resolve()),
        "annotated_images": [str(path) for path in annotation_result["image_paths"]],
        "overview_image": str(annotation_result["overview_path"] or ""),
        "review_index": str(annotation_result["index_path"] or ""),
    })
    return {
        "geojson_path": geojson_path.resolve(),
        "audit_path": audit_path.resolve(),
        "obstacle_count": len(features),
        "scope_count": len(seen_scopes),
        "annotated_image_paths": list(annotation_result["image_paths"]),
        "overview_image_path": annotation_result["overview_path"],
        "review_index_path": annotation_result["index_path"],
    }


def _is_usable_cached_detection(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == BUILDING_VISION_SCHEMA_VERSION
        and value.get("status") != "error"
    )


def _response_metadata(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    finish_reason = None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")
    return {
        "id": response.get("id"),
        "object": response.get("object"),
        "created": response.get("created"),
        "model": response.get("model"),
        "finish_reason": finish_reason,
        "usage": response.get("usage"),
    }


def _run_single_building_vision(
    record: dict[str, Any],
    *,
    config: ArkVisionConfig,
    validation_config: BuildingVectorValidationConfig,
    api_key: str,
    manifest_path: Path,
    annotated_dir: Path,
    responses_dir: Path,
    floor_geometry: Any | None,
    corridor_blocking_geometry: Any | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    image_id = _safe_token(str(record.get("image_id") or ""), "image")
    image_path = _resolve_record_image_path(record, manifest_path)
    record = dict(record)
    record["image_path"] = str(image_path)
    annotated_path = (annotated_dir / f"{image_id}_building_regions.png").resolve()
    response_path = (responses_dir / image_id / "model_response.json").resolve()

    if response_path.is_file() and not config.force:
        try:
            cached_payload = _read_json(response_path)
            cached_detection = cached_payload.get("normalized_detection")
            if _is_usable_cached_detection(cached_detection):
                candidate_detection = cached_payload.get("vision_candidate_detection")
                if not _is_usable_cached_detection(candidate_detection):
                    parsed_response = cached_payload.get("parsed_response")
                    if not isinstance(parsed_response, dict):
                        raw_content = cached_payload.get("raw_content")
                        if isinstance(raw_content, str) and raw_content.strip():
                            parsed_response = _parse_json_object_from_text(raw_content)
                    if isinstance(parsed_response, dict):
                        candidate_detection = _normalize_building_detection(
                            record,
                            parsed_response,
                            config,
                            response_path=response_path,
                            annotated_image_path=annotated_path,
                            cached=True,
                        )

                if _is_usable_cached_detection(candidate_detection):
                    candidate_detection = _attach_detection_paths(
                        candidate_detection,
                        response_path=response_path,
                        annotated_image_path=annotated_path,
                    )
                    candidate_detection["cached"] = True
                    detection = _refine_detection_with_vector_mask(
                        record,
                        candidate_detection,
                        image_path,
                        floor_geometry=floor_geometry,
                        corridor_blocking_geometry=corridor_blocking_geometry,
                        config=validation_config,
                    )
                    detection["cached"] = True
                    cached_payload["vision_candidate_detection"] = candidate_detection
                    cached_payload["normalized_detection"] = detection
                    cached_payload.setdefault(
                        "cache_refresh",
                        {},
                    )["vector_validation_refreshed_at_epoch"] = time.time()
                    cached_payload["cache_refresh"][
                        "vector_validation_source"
                    ] = "vision_candidate_detection"
                    _write_json(response_path, cached_payload)
                else:
                    # A legacy cache without raw model JSON cannot be refined safely.
                    # Reusing the already-refined result is idempotent; refining it
                    # again would compound bounding-box drift.
                    detection = _attach_detection_paths(
                        cached_detection,
                        response_path=response_path,
                        annotated_image_path=annotated_path,
                    )
                    detection["cached"] = True
                    detection["cache_refinement_skipped"] = (
                        "missing_original_vision_candidate_detection"
                    )
                annotate_building_regions(record, detection, annotated_path)
                return detection, annotated_path, response_path
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    response_package: dict[str, Any] = {
        "schema_version": 1,
        "image_id": image_id,
        "stage": "stage_05A_obstacles",
        "request": {
            "api_url": config.api_url,
            "model": config.model,
            "model_name": config.model_name,
            "timeout_seconds": config.timeout_seconds,
            "max_retries": config.max_retries,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "api_key_env": config.api_key_env,
            "image_path": str(image_path),
        },
    }
    try:
        response, used_json_response_format = _call_ark_building_vision(
            config,
            api_key,
            record,
            image_path,
        )
        content = _extract_choice_content(response)
        parsed = _parse_json_object_from_text(content)
        candidate_detection = _normalize_building_detection(
            record,
            parsed,
            config,
            response_path=response_path,
            annotated_image_path=annotated_path,
            cached=False,
        )
        detection = _refine_detection_with_vector_mask(
            record,
            candidate_detection,
            image_path,
            floor_geometry=floor_geometry,
            corridor_blocking_geometry=corridor_blocking_geometry,
            config=validation_config,
        )
        response_package.update(
            {
                "used_json_response_format": used_json_response_format,
                "response_metadata": _response_metadata(response),
                "raw_content": content,
                "parsed_response": parsed,
                "vision_candidate_detection": candidate_detection,
                "normalized_detection": detection,
            }
        )
    except Exception as exc:
        detection = _error_building_detection(
            record,
            config,
            response_path=response_path,
            annotated_image_path=annotated_path,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        response_package.update(
            {
                "error": {
                    "type": type(exc).__name__,
                    "message": _sanitize_error_text(str(exc)),
                },
                "normalized_detection": detection,
            }
        )

    _write_json(response_path, response_package)
    annotate_building_regions(record, detection, annotated_path)
    return detection, annotated_path, response_path


def run_building_vision(
    manifest_json: Path | str,
    sheets_json: Path | str,
    run_dir: Path | str,
    *,
    config: ArkVisionConfig | None = None,
    validation_config: BuildingVectorValidationConfig | None = None,
    obstacle_geojsons: Iterable[Path | str] | None = None,
    floor_ids: Iterable[str] | None = None,
    image_ids: Iterable[str] | None = None,
) -> BuildingVisionResult:
    """Call Volcano Ark vision model and write Building region annotations."""

    active_config = config or ArkVisionConfig()
    active_config.validate()
    active_validation_config = validation_config or BuildingVectorValidationConfig()
    active_validation_config.validate()
    api_key = (
        os.getenv(active_config.api_key_env, "").strip()
        or DEFAULT_ARK_API_KEY
    )
    if not api_key:
        raise RuntimeError(
            f"Set {active_config.api_key_env} before enabling building vision. "
            "The API key is intentionally read from the environment only."
        )

    manifest_path = Path(manifest_json).expanduser().resolve()
    sheets_path = Path(sheets_json).expanduser().resolve()
    run_path = Path(run_dir).expanduser().resolve()
    manifest_payload = _read_json(manifest_path)
    records = manifest_payload.get("images") if isinstance(manifest_payload, dict) else None
    if not isinstance(records, list):
        raise ValueError(f"Invalid Stage 05A manifest; missing images list: {manifest_path}")

    floor_filter = {str(value).strip() for value in floor_ids or [] if str(value).strip()}
    image_filter = {str(value).strip() for value in image_ids or [] if str(value).strip()}
    selected_records: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if floor_filter and str(record.get("floor_id") or "") not in floor_filter:
            continue
        if image_filter and str(record.get("image_id") or "") not in image_filter:
            continue
        selected_records.append(record)
    if not selected_records:
        raise RuntimeError("No Stage 05A images matched the building-vision filters")

    output_dir = run_path / "obstacle_building_region_render" / VISION_OUTPUT_DIR
    annotated_dir = output_dir / "annotated"
    responses_dir = output_dir / "responses"
    annotated_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)

    floor_unions: dict[str, Any] = {}
    corridor_blocking_unions: dict[str, Any] = {}
    obstacle_paths: list[Path] = []
    if obstacle_geojsons:
        (
            floor_unions,
            _rendered_counts,
            _source_counts,
            _excluded_counts,
            obstacle_paths,
        ) = load_renderable_obstacles(obstacle_geojsons)
        (
            corridor_blocking_unions,
            _all_rendered_counts,
            _all_source_counts,
            _all_excluded_counts,
            _all_obstacle_paths,
        ) = load_renderable_obstacles(
            obstacle_geojsons,
            excluded_obstacle_types=(),
        )

    detections: list[dict[str, Any]] = []
    annotated_paths: list[Path] = []
    response_paths: list[Path] = []
    for record in selected_records:
        detection, annotated_path, response_path = _run_single_building_vision(
            record,
            config=active_config,
            validation_config=active_validation_config,
            api_key=api_key,
            manifest_path=manifest_path,
            annotated_dir=annotated_dir,
            responses_dir=responses_dir,
            floor_geometry=floor_unions.get(str(record.get("floor_id") or "")),
            corridor_blocking_geometry=corridor_blocking_unions.get(
                str(record.get("floor_id") or "")
            ),
        )
        detections.append(detection)
        annotated_paths.append(annotated_path)
        response_paths.append(response_path)

    flat_regions = _flatten_building_regions(detections)
    success_count = sum(1 for detection in detections if detection.get("status") != "error")
    error_count = sum(1 for detection in detections if detection.get("status") == "error")
    needs_review_count = sum(
        1 for detection in detections if detection.get("needs_review")
    )

    building_regions_json = output_dir / BUILDING_REGIONS_JSON
    building_regions_csv = output_dir / BUILDING_REGIONS_CSV
    sheets_with_buildings_json = output_dir / SHEETS_WITH_BUILDINGS_JSON
    result_payload = {
        "schema_version": 1,
        "building_detection_schema_version": BUILDING_VISION_SCHEMA_VERSION,
        "stage": "stage_05A_obstacles",
        "purpose": (
            "Coarse floor-plan Building region detection from Stage 05A red "
            "obstacle-render images using Volcano Ark vision."
        ),
        "important_limitations": [
            "Building regions are reviewable candidates, not exact CAD structural boundaries.",
            "Coordinates are derived from model-returned image boxes and the Stage 05A transform.",
            "The API key is not stored in this output; only the environment variable name is recorded.",
        ],
        "sources": {
            "manifest_json": _fingerprint(manifest_path),
            "sheets_json": _fingerprint(sheets_path),
        },
        "config": asdict(active_config),
        "vector_validation_config": asdict(active_validation_config),
        "image_count": len(detections),
        "success_image_count": success_count,
        "error_count": error_count,
        "building_region_count": len(flat_regions),
        "needs_review_count": needs_review_count,
        "detections": detections,
        "building_regions": flat_regions,
    }
    if obstacle_paths:
        result_payload["sources"]["obstacle_detail_geojsons"] = [
            _fingerprint(path) for path in obstacle_paths
        ]
    _write_json(building_regions_json, result_payload)
    _write_building_regions_csv(building_regions_csv, flat_regions)

    sheets_payload = _read_json(sheets_path)
    if isinstance(sheets_payload, dict):
        merged_sheets = _merge_building_regions_into_sheets(sheets_payload, detections)
        _write_json(sheets_with_buildings_json, merged_sheets)
    else:
        _write_json(sheets_with_buildings_json, {"sheets": []})

    return BuildingVisionResult(
        output_dir=output_dir.resolve(),
        building_regions_json=building_regions_json.resolve(),
        building_regions_csv=building_regions_csv.resolve(),
        sheets_with_buildings_json=sheets_with_buildings_json.resolve(),
        annotated_image_paths=tuple(path.resolve() for path in annotated_paths),
        response_paths=tuple(path.resolve() for path in response_paths),
        image_count=len(detections),
        success_image_count=success_count,
        error_count=error_count,
        building_region_count=len(flat_regions),
        needs_review_count=needs_review_count,
    )


def _default_stage_inputs(
    run_dir: Path,
) -> tuple[Path, list[Path]]:
    sheets_json = run_dir / "preprocess" / "drawing_sheets_floors.json"
    detail_dir = run_dir / "obstacles" / "per_region_geojson"
    detail_paths = sorted(detail_dir.glob("*.geojson"))
    if not detail_paths:
        raise FileNotFoundError(
            f"No Stage 05 per-region obstacle details found in: {detail_dir}"
        )
    return sheets_json, detail_paths


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Render Stage 05 obstacle unions as white-background/red-obstacle PNG "
            "images for building-region vision review."
        )
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Existing fire-inspection pipeline run directory.",
    )
    parser.add_argument("--max-side-pixels", type=int, default=6000)
    parser.add_argument("--max-total-pixels", type=int, default=32_000_000)
    parser.add_argument("--min-side-pixels", type=int, default=1024)
    parser.add_argument("--margin-pixels", type=int, default=36)
    parser.add_argument(
        "--detect-buildings",
        action="store_true",
        help="Call Volcano Ark vision model to detect Building regions.",
    )
    parser.add_argument(
        "--floor-id",
        action="append",
        default=[],
        help="Limit building detection to a floor id such as F3; may repeat.",
    )
    parser.add_argument(
        "--image-id",
        action="append",
        default=[],
        help="Limit building detection to a Stage 05A image_id; may repeat.",
    )
    parser.add_argument("--ark-model", default=DEFAULT_ARK_VISION_MODEL)
    parser.add_argument("--ark-api-url", default=ARK_CHAT_COMPLETIONS_URL)
    parser.add_argument("--ark-api-key-env", default="ARK_API_KEY")
    parser.add_argument("--vision-timeout-seconds", type=int, default=240)
    parser.add_argument("--vision-max-retries", type=int, default=3)
    parser.add_argument("--vision-max-tokens", type=int, default=4096)
    parser.add_argument("--vision-temperature", type=float, default=0.0)
    parser.add_argument(
        "--vision-force",
        action="store_true",
        help="Ignore cached model responses and call Ark again.",
    )
    parser.add_argument(
        "--no-vector-validation",
        action="store_true",
        help="Keep Ark vision candidates without obstacle-density refinement.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_dir = Path(args.run_dir).expanduser().resolve()
    sheets_json, obstacle_paths = _default_stage_inputs(run_dir)
    config = ObstacleRenderConfig(
        max_side_pixels=args.max_side_pixels,
        max_total_pixels=args.max_total_pixels,
        min_side_pixels=args.min_side_pixels,
        margin_pixels=args.margin_pixels,
    )
    result = run_stage(sheets_json, obstacle_paths, run_dir, config=config)
    print("Stage 05A obstacle rendering complete")
    print(f"  images: {result.image_count}")
    print(f"  output_dir: {result.output_dir}")
    print(f"  manifest_json: {result.manifest_json}")
    print(f"  manifest_csv: {result.manifest_csv}")
    if args.detect_buildings:
        vision_config = ArkVisionConfig(
            model=args.ark_model,
            api_url=args.ark_api_url,
            api_key_env=args.ark_api_key_env,
            timeout_seconds=args.vision_timeout_seconds,
            max_retries=args.vision_max_retries,
            max_tokens=args.vision_max_tokens,
            force=args.vision_force,
            temperature=args.vision_temperature,
        )
        vision = run_building_vision(
            result.manifest_json,
            sheets_json,
            run_dir,
            config=vision_config,
            validation_config=BuildingVectorValidationConfig(
                enabled=not args.no_vector_validation
            ),
            obstacle_geojsons=obstacle_paths,
            floor_ids=args.floor_id,
            image_ids=args.image_id,
        )
        print("Stage 05A building vision complete")
        print(f"  images: {vision.image_count}")
        print(f"  success_images: {vision.success_image_count}")
        print(f"  error_images: {vision.error_count}")
        print(f"  building_regions: {vision.building_region_count}")
        print(f"  needs_review_images: {vision.needs_review_count}")
        print(f"  building_regions_json: {vision.building_regions_json}")
        print(f"  building_regions_csv: {vision.building_regions_csv}")
        print(f"  sheets_with_buildings_json: {vision.sheets_with_buildings_json}")
        if vision.error_count >= vision.image_count:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ArkVisionConfig",
    "ArkVisionRequestError",
    "BuildingVectorValidationConfig",
    "BuildingVisionResult",
    "ObstacleRenderConfig",
    "ObstacleRenderResult",
    "annotate_building_regions",
    "annotate_upper_floor_building_envelope_obstacles",
    "extract_building_labels",
    "find_multi_building_floor_ids",
    "load_inspection_regions",
    "load_renderable_obstacles",
    "render_obstacle_region",
    "run_building_vision",
    "run_stage",
    "write_upper_floor_building_envelope_obstacles",
]
