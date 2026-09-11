"""Generate stable, scope-aware PNG products for backend and vision delivery.

The renderer intentionally uses the same approved hard-obstacle sources as the
navigation stages.  A physical floor with one building produces one floor
record; a floor with multiple validated building envelopes produces only one
record per ``building_scope_id`` (never an additional combined floor image).

Every pipeline run also emits a high-resolution, route-free vision input for
each scope.  Those images contain only the approved obstacle geometry and the
classified inspection-object points, so a vision model can propose a route
without being biased by the deterministic planner's existing route output.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont


BBox = Tuple[float, float, float, float]

# Keep the PNG route colours aligned with Stage 10's DXF ``PASS_COLORS``:
# ACI 3, 1, 6, 5, 2, 4, 30, 140 and 200.  The RGB values below use slightly
# darker yellow/cyan variants so that thin route lines remain legible on the
# white business-image background while preserving the same colour semantics.
ROUTE_PASS_COLORS: Tuple[str, ...] = (
    "#16a05d",  # pass 1: green, primary traversal
    "#e53935",  # pass 2: red
    "#b52bbd",  # pass 3: magenta
    "#1565c0",  # pass 4: blue
    "#d49a00",  # pass 5: yellow/gold
    "#0097a7",  # pass 6: cyan
    "#ef6c00",  # pass 7: orange
    "#558b2f",  # pass 8: green variant
    "#6a4fb3",  # pass 9+: violet variant
)

VISION_INPUT_MAX_IMAGE_SIDE = 6400
VISION_INPUT_DIRNAME = "vision_input_points_no_route_2x"
VISION_INPUT_SUFFIX = "obstacles_inspection_points_NO_ROUTE_2x.png"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _physical_floor_id(value: Any) -> str:
    scope = _text(value)
    return scope.split("__", 1)[0] if "__" in scope else scope


def _read_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object: {}".format(path))
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_.\-一-鿿]+", "_", value).strip("._")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return "{}_{}".format(normalized[:80] or "scope", digest)


def _readable_safe_component(value: str) -> str:
    """Return a readable, filesystem-safe scope name for vision products."""

    normalized = re.sub(r"[^0-9A-Za-z_.\-一-鿿]+", "_", value).strip("._")
    return normalized[:120] or "scope"


def _load_features(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        feature
        for feature in _read_json(path).get("features", [])
        if isinstance(feature, dict)
    ]


def _iter_xy(value: Any) -> Iterable[Tuple[float, float]]:
    if not isinstance(value, (list, tuple)):
        return
    if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
        yield float(value[0]), float(value[1])
        return
    for item in value:
        yield from _iter_xy(item)


def _geometry_bbox(geometry: Mapping[str, Any]) -> BBox | None:
    points = list(_iter_xy(geometry.get("coordinates")))
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _iter_geometry_lines(geometry: Mapping[str, Any]) -> Iterable[List[Tuple[float, float]]]:
    kind = _text(geometry.get("type"))
    coordinates = geometry.get("coordinates") or []
    if kind == "LineString":
        yield [(float(x), float(y)) for x, y, *_ in coordinates]
    elif kind == "MultiLineString":
        for line in coordinates:
            yield [(float(x), float(y)) for x, y, *_ in line]
    elif kind == "Polygon":
        for ring in coordinates:
            yield [(float(x), float(y)) for x, y, *_ in ring]
    elif kind == "MultiPolygon":
        for polygon in coordinates:
            for ring in polygon:
                yield [(float(x), float(y)) for x, y, *_ in ring]


def _expand_bbox(bbox: BBox, ratio: float = 0.025) -> BBox:
    minx, miny, maxx, maxy = bbox
    span = max(maxx - minx, maxy - miny, 1.0)
    margin = span * ratio
    return minx - margin, miny - margin, maxx + margin, maxy + margin


def _bbox_area(bbox: BBox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _bbox_distance_sq(x: float, y: float, bbox: BBox) -> float:
    dx = max(bbox[0] - x, 0.0, x - bbox[2])
    dy = max(bbox[1] - y, 0.0, y - bbox[3])
    return dx * dx + dy * dy


def _bboxes_intersect(left: BBox, right: BBox) -> bool:
    return not (
        left[2] < right[0]
        or left[0] > right[2]
        or left[3] < right[1]
        or left[1] > right[3]
    )


@dataclass(frozen=True)
class ScopeSpec:
    scope_id: str
    floor_id: str
    building_id: str
    cad_bbox: BBox
    multi_building: bool


@dataclass(frozen=True)
class BusinessImageRecord:
    scope_id: str
    floor_id: str
    building_id: str
    multi_building: bool
    obstacle_image_path: Path
    route_image_path: Path
    image_width: int
    image_height: int
    cad_bbox: BBox
    scale: float
    offset_x: float
    offset_y: float

    def cad_to_pixel(self, x: float, y: float) -> Tuple[float, float]:
        minx, _miny, _maxx, maxy = self.cad_bbox
        return (
            self.offset_x + (x - minx) * self.scale,
            self.offset_y + (maxy - y) * self.scale,
        )

    def cad_to_percent(self, x: float, y: float) -> Tuple[float, float]:
        px, py = self.cad_to_pixel(x, y)
        return (
            round(max(0.0, min(100.0, px / max(1, self.image_width) * 100.0)), 6),
            round(max(0.0, min(100.0, py / max(1, self.image_height) * 100.0)), 6),
        )

    def to_manifest_row(
        self,
        *,
        target_count: int,
        route_segment_count: int,
        access_route_count: int,
    ) -> Dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "floor_id": self.floor_id,
            "building_id": self.building_id,
            "multi_building": self.multi_building,
            "cad_bbox": list(self.cad_bbox),
            "image_width": self.image_width,
            "image_height": self.image_height,
            "transform": {
                "scale_pixels_per_cad_unit": self.scale,
                "offset_x_pixels": self.offset_x,
                "offset_y_pixels": self.offset_y,
            },
            "obstacle_image_path": str(self.obstacle_image_path),
            "route_image_path": str(self.route_image_path),
            "target_count": target_count,
            "route_segment_count": route_segment_count,
            "access_route_count": access_route_count,
        }


def _sheet_bboxes(run_dir: Path) -> Dict[str, BBox]:
    result: Dict[str, BBox] = {}
    summary_path = run_dir / "pipeline_summary.json"
    if summary_path.is_file():
        summary = _read_json(summary_path)
        candidate = Path(_text((summary.get("cad_preprocess") or {}).get("sheets_json")))
        if candidate.is_file():
            for sheet in _read_json(candidate).get("sheets", []):
                if not isinstance(sheet, dict):
                    continue
                floor_id = _text(sheet.get("floor_id"))
                bbox = sheet.get("inspection_region_bbox") or sheet.get("bbox") or []
                if floor_id and len(bbox) == 4:
                    result[floor_id] = tuple(float(item) for item in bbox)  # type: ignore[assignment]

    # Compatibility with small fixtures and older runs that only have Stage 05A.
    legacy = run_dir / "obstacle_building_region_render" / "obstacle_building_vision_manifest.json"
    if legacy.is_file():
        for row in _read_json(legacy).get("images", []):
            if not isinstance(row, dict):
                continue
            floor_id = _text(row.get("floor_id"))
            bbox = row.get("cad_bbox") or []
            if floor_id and len(bbox) == 4 and floor_id not in result:
                result[floor_id] = tuple(float(item) for item in bbox)  # type: ignore[assignment]
    return result


def _targets(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / "navigation_graph" / "inputs" / "navigation_targets.geojson"
    result: List[Dict[str, Any]] = []
    for feature in _load_features(path):
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if geometry.get("type") != "Point" or len(coordinates) < 2:
            continue
        row = dict(feature.get("properties") or {})
        row["cad_x"] = float(coordinates[0])
        row["cad_y"] = float(coordinates[1])
        result.append(row)
    return result


def _route_features(run_dir: Path) -> List[Dict[str, Any]]:
    walk_dir = run_dir / "path_planning" / "dual_graph" / "physical_walk"
    beautified = walk_dir / "forwarding_route_beautified.geojson"
    original = walk_dir / "forwarding_route.geojson"
    return _load_features(beautified if beautified.is_file() else original)


def load_target_access_features(run_dir: Path) -> List[Dict[str, Any]]:
    """Load vector-validated target-to-navigation access paths.

    These are the safe branch paths omitted from ``forwarding_route.geojson``
    when a target was not selected by the default semantic planner.
    """

    path = (
        Path(run_dir)
        / "area_graph_navigation_refined"
        / "refined_navigation_graph.json"
    )
    if not path.is_file():
        return []
    graph = _read_json(path)
    node_targets = {
        _text(node.get("node_id")): _text(node.get("target_id"))
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and _text(node.get("target_id"))
    }
    result: List[Dict[str, Any]] = []
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict):
            continue
        kind = _text(edge.get("kind"))
        if kind not in {"target_access_edge", "virtual_target_access_edge"}:
            continue
        node_a = _text(edge.get("node_a"))
        node_b = _text(edge.get("node_b"))
        target_id = node_targets.get(node_a) or node_targets.get(node_b)
        if not target_id:
            for node_id in (node_a, node_b):
                if node_id.startswith("TARGET::") or node_id.startswith("VIRTUAL::"):
                    target_id = node_id.split("::", 1)[1]
                    break
        geometry = edge.get("geometry") or {}
        if not target_id or not list(_iter_geometry_lines(geometry)):
            continue
        result.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_type": "target_access_route",
                    "floor_id": _text(edge.get("floor_id")),
                    "target_id": target_id,
                    "access_kind": kind,
                    "vector_validated": bool(
                        edge.get("vector_valid_with_raster_tolerance", True)
                    ),
                },
                "geometry": geometry,
            }
        )
    return result


def _hard_obstacle_sources(run_dir: Path) -> List[Tuple[Path, bool]]:
    manifest_path = run_dir / "obstacles" / "navigation_hard_constraints" / "manifest.json"
    result: List[Tuple[Path, bool]] = []
    if manifest_path.is_file():
        for row in _read_json(manifest_path).get("sources", []):
            if not isinstance(row, dict):
                continue
            path = Path(_text(row.get("path")))
            if path.is_file():
                result.append((path.resolve(), bool(row.get("is_repair"))))
    if result:
        return result

    summary_path = run_dir / "pipeline_summary.json"
    if summary_path.is_file():
        summary = _read_json(summary_path)
        for value in (summary.get("obstacle_recognition") or {}).get("union_geojsons", []):
            path = Path(_text(value))
            if path.is_file():
                result.append((path.resolve(), False))
    repair = (
        run_dir
        / "obstacles"
        / "outer_wall_topology_repair"
        / "door_aware_outer_wall_repairs.geojson"
    )
    if repair.is_file():
        result.append((repair.resolve(), True))
    return result


def _load_obstacles_and_repairs(
    run_dir: Path,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, BBox]]:
    obstacles: Dict[str, List[Dict[str, Any]]] = {}
    repair_scope_bboxes: Dict[str, BBox] = {}
    for path, is_repair in _hard_obstacle_sources(run_dir):
        for feature in _load_features(path):
            properties = dict(feature.get("properties") or {})
            floor_id = _text(properties.get("source_floor_id")) or _physical_floor_id(
                properties.get("floor_id")
            )
            if not floor_id:
                continue
            tagged = dict(feature)
            tagged["_business_is_repair"] = is_repair
            obstacles.setdefault(floor_id, []).append(tagged)
            if is_repair:
                scope_id = _text(properties.get("building_scope_id")) or _text(
                    properties.get("floor_id")
                )
                bbox = _geometry_bbox(feature.get("geometry") or {})
                if scope_id and bbox:
                    repair_scope_bboxes[scope_id] = bbox
    return obstacles, repair_scope_bboxes


def _derive_scope_bbox_from_targets(
    scope_id: str, floor_bbox: BBox, targets: Sequence[Mapping[str, Any]]
) -> BBox:
    points = [
        (float(row["cad_x"]), float(row["cad_y"]))
        for row in targets
        if _text(row.get("building_scope_id")) == scope_id
    ]
    if not points:
        return floor_bbox
    minx, miny, maxx, maxy = floor_bbox
    floor_span = max(maxx - minx, maxy - miny, 1.0)
    pad = floor_span * 0.12
    return (
        max(minx, min(point[0] for point in points) - pad),
        max(miny, min(point[1] for point in points) - pad),
        min(maxx, max(point[0] for point in points) + pad),
        min(maxy, max(point[1] for point in points) + pad),
    )


def _scope_specs(
    floor_bboxes: Mapping[str, BBox],
    repair_scope_bboxes: Mapping[str, BBox],
    targets: Sequence[Mapping[str, Any]],
) -> List[ScopeSpec]:
    explicit: Dict[str, set[str]] = {}
    for row in targets:
        floor_id = _text(row.get("source_floor_id")) or _physical_floor_id(row.get("floor_id"))
        scope_id = _text(row.get("building_scope_id"))
        if floor_id and scope_id and scope_id != floor_id:
            explicit.setdefault(floor_id, set()).add(scope_id)
    repairs: Dict[str, set[str]] = {}
    for scope_id in repair_scope_bboxes:
        repairs.setdefault(_physical_floor_id(scope_id), set()).add(scope_id)

    floors = set(floor_bboxes) | set(repairs) | {
        _text(row.get("source_floor_id")) or _physical_floor_id(row.get("floor_id"))
        for row in targets
    }
    result: List[ScopeSpec] = []
    for floor_id in sorted(value for value in floors if value):
        candidates = repairs.get(floor_id, set()) or explicit.get(floor_id, set())
        floor_bbox = floor_bboxes.get(floor_id)
        if floor_bbox is None:
            candidate_boxes = [repair_scope_bboxes[item] for item in candidates if item in repair_scope_bboxes]
            if candidate_boxes:
                floor_bbox = (
                    min(item[0] for item in candidate_boxes),
                    min(item[1] for item in candidate_boxes),
                    max(item[2] for item in candidate_boxes),
                    max(item[3] for item in candidate_boxes),
                )
        if floor_bbox is None:
            continue
        if len(candidates) > 1:
            for scope_id in sorted(candidates):
                bbox = repair_scope_bboxes.get(scope_id) or _derive_scope_bbox_from_targets(
                    scope_id, floor_bbox, targets
                )
                result.append(
                    ScopeSpec(
                        scope_id=scope_id,
                        floor_id=floor_id,
                        building_id=scope_id.split("__", 1)[1] if "__" in scope_id else scope_id,
                        cad_bbox=_expand_bbox(bbox),
                        multi_building=True,
                    )
                )
        else:
            result.append(
                ScopeSpec(
                    scope_id=floor_id,
                    floor_id=floor_id,
                    building_id="",
                    cad_bbox=_expand_bbox(floor_bbox, ratio=0.01),
                    multi_building=False,
                )
            )
    return result


def _point_in_bbox(x: float, y: float, bbox: BBox, tolerance: float = 1e-6) -> bool:
    return (
        bbox[0] - tolerance <= x <= bbox[2] + tolerance
        and bbox[1] - tolerance <= y <= bbox[3] + tolerance
    )


def assign_targets_to_scopes(
    records: Mapping[str, BusinessImageRecord], targets: Sequence[Mapping[str, Any]]
) -> Dict[str, List[Mapping[str, Any]]]:
    """Assign every target to at most one output scope."""

    grouped: Dict[str, List[Mapping[str, Any]]] = {scope_id: [] for scope_id in records}
    by_floor: Dict[str, List[BusinessImageRecord]] = {}
    for record in records.values():
        by_floor.setdefault(record.floor_id, []).append(record)
    for target in targets:
        floor_id = _text(target.get("source_floor_id")) or _physical_floor_id(target.get("floor_id"))
        candidates = by_floor.get(floor_id, [])
        if not candidates:
            continue
        explicit = _text(target.get("building_scope_id"))
        exact = [record for record in candidates if record.scope_id == explicit]
        if exact:
            chosen = exact[0]
        elif len(candidates) == 1:
            chosen = candidates[0]
        else:
            x, y = float(target["cad_x"]), float(target["cad_y"])
            containing = [record for record in candidates if _point_in_bbox(x, y, record.cad_bbox)]
            if containing:
                chosen = min(containing, key=lambda record: _bbox_area(record.cad_bbox))
            else:
                # Vector envelopes can stop at the wall centreline while a block
                # insertion point sits just outside it.  Never silently lose the
                # business object: attach it to the nearest building envelope.
                chosen = min(
                    candidates,
                    key=lambda record: _bbox_distance_sq(x, y, record.cad_bbox),
                )
        grouped[chosen.scope_id].append(target)
    return grouped


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _new_record(spec: ScopeSpec, output_dir: Path, max_image_side: int) -> BusinessImageRecord:
    minx, miny, maxx, maxy = spec.cad_bbox
    cad_width = max(maxx - minx, 1.0)
    cad_height = max(maxy - miny, 1.0)
    header = 72
    margin = 42
    usable = max(512, max_image_side - 2 * margin - header)
    scale = min(usable / cad_width, usable / cad_height)
    width = max(720, int(round(cad_width * scale + 2 * margin)))
    height = max(520, int(round(cad_height * scale + 2 * margin + header)))
    safe = _safe_component(spec.scope_id)
    return BusinessImageRecord(
        scope_id=spec.scope_id,
        floor_id=spec.floor_id,
        building_id=spec.building_id,
        multi_building=spec.multi_building,
        obstacle_image_path=(output_dir / "{}_obstacles.png".format(safe)).resolve(),
        route_image_path=(output_dir / "{}_route_points.png".format(safe)).resolve(),
        image_width=width,
        image_height=height,
        cad_bbox=spec.cad_bbox,
        scale=scale,
        offset_x=float(margin),
        offset_y=float(margin + header),
    )


def _draw_header(image: Image.Image, record: BusinessImageRecord, suffix: str) -> None:
    draw = ImageDraw.Draw(image)
    title = record.floor_id
    if record.multi_building:
        title += " | {}".format(record.building_id)
    draw.rectangle((0, 0, image.width, 70), fill="#f7f9fc")
    draw.line((0, 70, image.width, 70), fill="#d6dde8", width=1)
    draw.text((24, 18), "{} | {}".format(title, suffix), fill="#253247", font=_font(26, bold=True))


def _draw_route_legend(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image)
    font = _font(17)
    entries = [
        ("route_passes", "", "实际路线（按遍历次数着色）"),
        ("line", "#6fbd86", "安全接入支线"),
        ("point", "#ff9f1a", "分类后巡检对象"),
    ]
    widths = []
    for _kind, _color, label in entries:
        box = draw.textbbox((0, 0), label, font=font)
        widths.append(35 + box[2] - box[0] + 18)
    total = sum(widths)
    x = max(330, image.width - total - 22)
    y = 35
    for (kind, color, label), width in zip(entries, widths):
        if kind == "point":
            draw.ellipse((x, y - 5, x + 10, y + 5), fill=color, outline="white")
        elif kind == "route_passes":
            for index, route_color in enumerate(ROUTE_PASS_COLORS[:4]):
                left = x + index * 5
                draw.line((left, y, left + 5, y), fill=route_color, width=4)
        else:
            draw.line((x, y, x + 18, y), fill=color, width=4)
        draw.text((x + 24, y - 10), label, fill="#40516a", font=font)
        x += width


def draw_route_header_and_legend(
    image: Image.Image, record: BusinessImageRecord
) -> None:
    """Apply the route title and semantic legend to a transport image."""

    _draw_header(image, record, "route + inspection points")
    _draw_route_legend(image)


def _draw_obstacles(
    record: BusinessImageRecord,
    features: Sequence[Mapping[str, Any]],
) -> Image.Image:
    image = Image.new("RGB", (record.image_width, record.image_height), "white")
    _draw_header(image, record, "obstacle map")
    draw = ImageDraw.Draw(image)
    normal_width = max(2, int(round(max(image.size) / 1500.0)))
    repair_width = max(normal_width + 1, int(round(max(image.size) / 900.0)))
    for feature in features:
        is_repair = bool(feature.get("_business_is_repair"))
        color = "#1976d2" if is_repair else "#596779"
        width = repair_width if is_repair else normal_width
        for line in _iter_geometry_lines(feature.get("geometry") or {}):
            pixels = [record.cad_to_pixel(x, y) for x, y in line]
            if len(pixels) >= 2:
                draw.line(pixels, fill=color, width=width, joint="curve")
    border = (
        record.offset_x,
        record.offset_y,
        record.image_width - record.offset_x,
        record.image_height - 42,
    )
    draw.rectangle(border, outline="#8aa6cf", width=2)
    return image


def _draw_route_and_targets(
    base: Image.Image,
    record: BusinessImageRecord,
    targets: Sequence[Mapping[str, Any]],
    route_features: Sequence[Mapping[str, Any]],
    access_features: Sequence[Mapping[str, Any]],
) -> Tuple[Image.Image, int, int]:
    image = base.copy()
    draw_route_header_and_legend(image, record)
    route_width = max(4, int(round(max(image.size) / 800.0)))
    access_count = draw_target_access_routes(
        image,
        record,
        targets,
        access_features,
        width=max(2, route_width - 2),
    )
    route_count = draw_route_traversals(
        image,
        record,
        route_features,
        width=route_width,
    )
    draw_target_markers_and_labels(image, record, targets)
    return image, route_count, access_count


def route_pass_color(properties: Mapping[str, Any]) -> str:
    """Return the PNG colour matching the Stage 10 DXF pass-index layer."""

    try:
        pass_index = int(properties.get("pass_index") or 1)
    except (TypeError, ValueError):
        pass_index = 1
    palette_index = min(max(pass_index - 1, 0), len(ROUTE_PASS_COLORS) - 1)
    return ROUTE_PASS_COLORS[palette_index]


def draw_route_traversals(
    image: Image.Image,
    record: BusinessImageRecord,
    route_features: Sequence[Mapping[str, Any]],
    *,
    width: int | None = None,
    fixed_color: str = "",
) -> int:
    """Draw every actual route edge, preserving its DXF pass-index colour."""

    draw = ImageDraw.Draw(image)
    line_width = width or max(3, int(round(max(image.size) / 900.0)))
    count = 0
    for feature in sorted(
        route_features,
        key=lambda row: int((row.get("properties") or {}).get("sequence_no") or 0),
    ):
        properties = feature.get("properties") or {}
        if _physical_floor_id(properties.get("floor_id")) != record.floor_id:
            continue
        if properties.get("feature_type") != "route_edge_traversal":
            continue
        explicit_scope = _text(properties.get("building_scope_id"))
        if explicit_scope and explicit_scope != record.scope_id:
            continue
        color = fixed_color or route_pass_color(properties)
        for line in _iter_geometry_lines(feature.get("geometry") or {}):
            if not line:
                continue
            line_bbox = (
                min(point[0] for point in line),
                min(point[1] for point in line),
                max(point[0] for point in line),
                max(point[1] for point in line),
            )
            if not _bboxes_intersect(line_bbox, record.cad_bbox):
                continue
            pixels = [record.cad_to_pixel(x, y) for x, y in line]
            if len(pixels) >= 2:
                draw.line(pixels, fill=color, width=line_width, joint="curve")
                count += 1
    return count


def draw_target_access_routes(
    image: Image.Image,
    record: BusinessImageRecord,
    targets: Sequence[Mapping[str, Any]],
    access_features: Sequence[Mapping[str, Any]],
    *,
    color: str = "#6fbd86",
    width: int | None = None,
) -> int:
    """Draw safe target-access branches under the darker forwarding route."""

    draw = ImageDraw.Draw(image)
    line_width = width or max(2, int(round(max(image.size) / 900.0)))
    target_ids = {_text(row.get("target_id")) for row in targets}
    count = 0
    for feature in access_features:
        properties = feature.get("properties") or {}
        if _text(properties.get("target_id")) not in target_ids:
            continue
        if _physical_floor_id(properties.get("floor_id")) != record.floor_id:
            continue
        if not bool(properties.get("vector_validated", True)):
            continue
        for line in _iter_geometry_lines(feature.get("geometry") or {}):
            if not line:
                continue
            line_bbox = (
                min(point[0] for point in line),
                min(point[1] for point in line),
                max(point[0] for point in line),
                max(point[1] for point in line),
            )
            if not _bboxes_intersect(line_bbox, record.cad_bbox):
                continue
            draw.line(
                [record.cad_to_pixel(x, y) for x, y in line],
                fill=color,
                width=line_width,
                joint="curve",
            )
            count += 1
    return count


def _target_display_name(target: Mapping[str, Any]) -> str:
    return (
        _text(target.get("target_class"))
        or _text(target.get("class_name"))
        or _text(target.get("source_class_name"))
        or _text(target.get("raw_name"))
        or _text(target.get("original_object_name"))
        or _text(target.get("target_id"))
    )


def _place_label(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    px: float,
    py: float,
    radius: int,
    label: str,
    font: ImageFont.ImageFont,
    occupied: List[Tuple[int, int, int, int]],
) -> Tuple[int, int, int, int]:
    text_bbox = draw.textbbox((0, 0), label, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    offsets = [
        (radius + 5, -text_height - 3),
        (radius + 5, radius + 2),
        (-text_width - radius - 5, -text_height - 3),
        (-text_width - radius - 5, radius + 2),
        (-text_width // 2, -text_height - radius - 5),
        (-text_width // 2, radius + 5),
    ]
    candidates: List[Tuple[int, int, int, int]] = []
    for dx, dy in offsets:
        left = int(round(px + dx))
        top = int(round(py + dy))
        box = (left - 3, top - 2, left + text_width + 4, top + text_height + 3)
        candidates.append(box)
        inside = box[0] >= 2 and box[1] >= 72 and box[2] < image.width - 2 and box[3] < image.height - 2
        overlaps = any(_bboxes_intersect(box, used) for used in occupied)
        if inside and not overlaps:
            return box
    return min(
        candidates,
        key=lambda box: sum(_bboxes_intersect(box, used) for used in occupied),
    )


def draw_target_markers_and_labels(
    image: Image.Image,
    record: BusinessImageRecord,
    targets: Sequence[Mapping[str, Any]],
    *,
    start_target_id: str = "",
) -> None:
    """Draw every inspection point with its classified inspection-object name."""

    draw = ImageDraw.Draw(image)
    radius = max(6, int(round(max(image.size) / 840.0)))
    original_font_size = max(15, int(round(max(image.size) / 175.0)))
    font = _font(max(5, int(round(original_font_size / 3.0))))
    occupied: List[Tuple[int, int, int, int]] = []
    for target in sorted(targets, key=lambda row: _text(row.get("target_id"))):
        px, py = record.cad_to_pixel(float(target["cad_x"]), float(target["cad_y"]))
        is_start = _text(target.get("target_id")) == _text(start_target_id)
        fill = "#2e9d55" if is_start else "#ff9f1a"
        draw.ellipse(
            (px - radius, py - radius, px + radius, py + radius),
            fill=fill,
            outline="white",
            width=2,
        )
        label = _target_display_name(target)
        if len(label) > 24:
            label = label[:23] + "…"
        box = _place_label(draw, image, px, py, radius, label, font, occupied)
        occupied.append(box)
        draw.rounded_rectangle(box, radius=3, fill="#fffffff0", outline="#d9dee7", width=1)
        draw.text((box[0] + 3, box[1] + 1), label, fill="#273548", font=font)


def _draw_vision_input_legend(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image)
    font = _font(max(22, int(round(max(image.size) / 270.0))))
    obstacle_label = "障碍物/不可穿越边界"
    target_label = "巡检对象点位（编号 + 类别）"
    obstacle_width = draw.textbbox((0, 0), obstacle_label, font=font)[2]
    target_width = draw.textbbox((0, 0), target_label, font=font)[2]
    total_width = 52 + obstacle_width + 70 + target_width
    x = max(900, image.width - total_width - 34)
    y = 35
    draw.line((x, y, x + 28, y), fill="#596779", width=7)
    draw.text((x + 38, y - 13), obstacle_label, fill="#40516a", font=font)
    x += 52 + obstacle_width
    draw.ellipse(
        (x, y - 10, x + 20, y + 10),
        fill="#ff8c00",
        outline="white",
        width=3,
    )
    draw.text((x + 31, y - 13), target_label, fill="#40516a", font=font)


def _vision_target_label(target: Mapping[str, Any]) -> str:
    target_id = _text(target.get("target_id"))
    short_id = target_id.rsplit("_", 1)[-1][-3:] if target_id else "---"
    name = _target_display_name(target)
    if len(name) > 18:
        name = name[:17] + "…"
    return "{} {}".format(short_id, name)


def draw_vision_input_points_and_labels(
    image: Image.Image,
    record: BusinessImageRecord,
    targets: Sequence[Mapping[str, Any]],
) -> None:
    """Draw uniquely labelled inspection points without any route geometry."""

    draw = ImageDraw.Draw(image)
    radius = max(10, int(round(max(image.size) / 620.0)))
    font = _font(max(20, int(round(max(image.size) / 285.0))), bold=True)
    occupied: List[Tuple[int, int, int, int]] = []
    for target in sorted(targets, key=lambda row: _text(row.get("target_id"))):
        px, py = record.cad_to_pixel(float(target["cad_x"]), float(target["cad_y"]))
        draw.ellipse(
            (px - radius, py - radius, px + radius, py + radius),
            fill="#ff8c00",
            outline="white",
            width=max(3, radius // 4),
        )
        label = _vision_target_label(target)
        box = _place_label(draw, image, px, py, radius, label, font, occupied)
        occupied.append(box)
        draw.rounded_rectangle(
            box,
            radius=5,
            fill="#fffffff2",
            outline="#c7ced9",
            width=2,
        )
        draw.text((box[0] + 4, box[1] + 1), label, fill="#172235", font=font)


def _draw_vision_input(
    record: BusinessImageRecord,
    obstacle_features: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
) -> Image.Image:
    image = _draw_obstacles(record, obstacle_features)
    _draw_header(image, record, "obstacles + inspection points | NO ROUTE")
    _draw_vision_input_legend(image)
    draw_vision_input_points_and_labels(image, record, targets)
    return image


def generate_business_image_outputs(
    run_dir: Path,
    *,
    output_run_dir: Path | None = None,
    max_image_side: int = 3200,
    vision_input_max_image_side: int = VISION_INPUT_MAX_IMAGE_SIDE,
) -> Dict[str, Any]:
    """Generate obstacle, route, and route-free vision PNGs for every scope.

    ``run_dir`` is the route-runtime data source.  ``output_run_dir`` may point
    at a parent orchestration run (for example, a multi-drawing run) so the
    renderer can reuse the same runtime contract while publishing the standard
    top-level ``business_outputs`` directory.
    """

    run = Path(run_dir).expanduser().resolve()
    output_run = (
        Path(output_run_dir).expanduser().resolve()
        if output_run_dir is not None
        else run
    )
    output_dir = output_run / "business_outputs" / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    vision_output_dir = output_run / "business_outputs" / VISION_INPUT_DIRNAME
    vision_output_dir.mkdir(parents=True, exist_ok=True)
    targets = _targets(run)
    routes = _route_features(run)
    access_routes = load_target_access_features(run)
    obstacles, repair_scope_bboxes = _load_obstacles_and_repairs(run)
    specs = _scope_specs(_sheet_bboxes(run), repair_scope_bboxes, targets)
    records = {
        spec.scope_id: _new_record(spec, output_dir, max(1200, int(max_image_side)))
        for spec in specs
    }
    vision_side = max(1200, int(vision_input_max_image_side))
    vision_records = {
        spec.scope_id: _new_record(spec, vision_output_dir, vision_side)
        for spec in specs
    }
    grouped_targets = assign_targets_to_scopes(records, targets)
    grouped_vision_targets = assign_targets_to_scopes(vision_records, targets)

    rows: List[Dict[str, Any]] = []
    vision_rows: List[Dict[str, Any]] = []
    for scope_id in sorted(records):
        record = records[scope_id]
        base = _draw_obstacles(record, obstacles.get(record.floor_id, []))
        route_image, route_count, access_route_count = _draw_route_and_targets(
            base,
            record,
            grouped_targets.get(scope_id, []),
            routes,
            access_routes,
        )
        base.save(record.obstacle_image_path, format="PNG", optimize=True)
        route_image.save(record.route_image_path, format="PNG", optimize=True)
        vision_record = vision_records[scope_id]
        scope_vision_targets = grouped_vision_targets.get(scope_id, [])
        vision_image = _draw_vision_input(
            vision_record,
            obstacles.get(vision_record.floor_id, []),
            scope_vision_targets,
        )
        vision_path = vision_output_dir / "{}_{}".format(
            _readable_safe_component(scope_id),
            VISION_INPUT_SUFFIX,
        )
        vision_image.save(
            vision_path,
            format="PNG",
            optimize=True,
            compress_level=6,
        )
        vision_row = {
            "scope_id": scope_id,
            "floor_id": vision_record.floor_id,
            "building_id": vision_record.building_id,
            "multi_building": vision_record.multi_building,
            "image_path": str(vision_path.resolve()),
            "image_width": vision_image.width,
            "image_height": vision_image.height,
            "target_count": len(scope_vision_targets),
            "contains_route": False,
            "cad_bbox": list(vision_record.cad_bbox),
            "transform": {
                "scale_pixels_per_cad_unit": vision_record.scale,
                "offset_x_pixels": vision_record.offset_x,
                "offset_y_pixels": vision_record.offset_y,
            },
        }
        vision_rows.append(vision_row)
        row = record.to_manifest_row(
            target_count=len(grouped_targets.get(scope_id, [])),
            route_segment_count=route_count,
            access_route_count=access_route_count,
        )
        row.update(
            {
                "vision_input_image_path": vision_row["image_path"],
                "vision_input_image_width": vision_row["image_width"],
                "vision_input_image_height": vision_row["image_height"],
                "vision_input_contains_route": False,
            }
        )
        rows.append(row)

    vision_manifest_path = vision_output_dir / "manifest.json"
    vision_manifest = {
        "purpose": "high_resolution_vision_model_input_with_obstacles_and_inspection_points_only",
        "contains_route": False,
        "max_image_side": vision_side,
        "image_count": len(vision_rows),
        "target_count": sum(row["target_count"] for row in vision_rows),
        "images": vision_rows,
    }
    _write_json(vision_manifest_path, vision_manifest)

    manifest_path = output_run / "business_outputs" / "business_image_manifest.json"
    manifest = {
        "policy": "physical_floor_or_building_scope_v1",
        "source_route_runtime_dir": str(run),
        "output_run_dir": str(output_run),
        "image_count": len(rows) * 3,
        "scope_count": len(rows),
        "max_image_side": max(1200, int(max_image_side)),
        "vision_input_max_image_side": vision_side,
        "vision_input_manifest": str(vision_manifest_path.resolve()),
        "rules": {
            "single_building_floor": "one physical-floor image triplet",
            "multi_building_floor": "building-scope image triplets only; no combined floor image",
            "obstacles": "approved original obstacles plus confirmed topology repairs",
            "vision_input": "approved obstacles plus classified inspection points; no route geometry",
        },
        "images": rows,
    }
    _write_json(manifest_path, manifest)
    result = {
        "manifest": str(manifest_path.resolve()),
        "source_route_runtime_dir": str(run),
        "output_run_dir": str(output_run),
        "output_dir": str(output_dir.resolve()),
        "vision_input_manifest": str(vision_manifest_path.resolve()),
        "vision_input_output_dir": str(vision_output_dir.resolve()),
        "scope_count": len(rows),
        "image_count": len(rows) * 3,
        "vision_input_image_count": len(vision_rows),
        "scopes": [row["scope_id"] for row in rows],
    }
    summary_path = run / "pipeline_summary.json"
    if summary_path.is_file():
        summary = _read_json(summary_path)
        summary["business_image_outputs"] = result
        _write_json(summary_path, summary)
    return result


def load_business_image_records(run_dir: Path) -> Dict[str, BusinessImageRecord]:
    run = Path(run_dir).expanduser().resolve()
    manifest_path = run / "business_outputs" / "business_image_manifest.json"
    if not manifest_path.is_file():
        # Lazy generation is only a compatibility path for legacy/backend
        # fixtures. Full pipeline runs call generate_business_image_outputs()
        # directly and therefore use the standard 6400-pixel vision product.
        generate_business_image_outputs(run, vision_input_max_image_side=1200)
    manifest = _read_json(manifest_path)
    records: Dict[str, BusinessImageRecord] = {}
    for row in manifest.get("images", []):
        if not isinstance(row, dict):
            continue
        scope_id = _text(row.get("scope_id"))
        bbox = row.get("cad_bbox") or []
        transform = row.get("transform") or {}
        obstacle_path = Path(_text(row.get("obstacle_image_path")))
        route_path = Path(_text(row.get("route_image_path")))
        if not scope_id or len(bbox) != 4 or not obstacle_path.is_file():
            continue
        records[scope_id] = BusinessImageRecord(
            scope_id=scope_id,
            floor_id=_text(row.get("floor_id")) or _physical_floor_id(scope_id),
            building_id=_text(row.get("building_id")),
            multi_building=bool(row.get("multi_building")),
            obstacle_image_path=obstacle_path.resolve(),
            route_image_path=route_path.resolve() if route_path.is_file() else obstacle_path.resolve(),
            image_width=int(row.get("image_width") or 0),
            image_height=int(row.get("image_height") or 0),
            cad_bbox=tuple(float(item) for item in bbox),  # type: ignore[arg-type]
            scale=float(transform.get("scale_pixels_per_cad_unit") or 0.0),
            offset_x=float(transform.get("offset_x_pixels") or 0.0),
            offset_y=float(transform.get("offset_y_pixels") or 0.0),
        )
    return {key: value for key, value in records.items() if value.scale > 0.0}
