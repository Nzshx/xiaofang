from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import ezdxf

from ..common import (
    Registration,
    Sheet,
    asdict_list,
    clean_text,
    entity_point,
    entity_text,
    median,
    percentile,
    read_json,
    write_csv,
    write_json,
)


Feature = tuple[str, float, float, str]
Transform = tuple[float, float, float, float]  # scale, rotation radians, dx, dy
Score = tuple[int, int, int, float, float, list[float], list[float]]

AXIS_LAYER_RE = re.compile(r"AXIS|GRID|轴线|轴网|轴号|DOTE", re.I)
AXIS_GEOMETRY_LAYER_RE = re.compile(r"AXIS|(?:^|[-_])GRID(?:$|[-_])|轴线|轴网", re.I)
AXIS_AUXILIARY_LAYER_RE = re.compile(r"DIMS?|TEXT|ANNO|DOTE", re.I)
AXIS_LABEL_RE = re.compile(r"^[A-Z]{0,3}\d{1,3}(?:[-/][A-Z0-9]{1,3})?$|^[A-Z]{1,3}$", re.I)
PROFESSIONAL_LAYER_RE = re.compile(
    r"EQUIP|PIPE|WIRE|VALVE|消防|喷淋|给水|排水|应急|广播|电话|通讯|通信|照明|电气|暖通|风管",
    re.I,
)
MAX_NESTED_AXIS_DEPTH = 10
MAX_NESTED_AXIS_ENTITIES = 1_500_000


def _effective_layer(entity: Any, inherited_layer: str = "") -> str:
    """Resolve DXF layer-0 inheritance for entities inside INSERTs."""
    layer = str(entity.dxf.get("layer", "") or "")
    if layer in {"", "0"} and inherited_layer not in {"", "0"}:
        return inherited_layer
    return layer


def _block_has_axis_content(
    doc: ezdxf.document.Drawing,
    block_name: str,
    memo: dict[str, bool],
    visiting: set[str],
) -> bool:
    """Return whether a block can lead to an axis label or axis line."""
    if block_name in memo:
        return memo[block_name]
    if block_name in visiting:
        return False
    visiting.add(block_name)
    found = bool(AXIS_LAYER_RE.search(block_name))
    try:
        block = doc.blocks.get(block_name)
    except Exception:
        block = None
    if block is not None and not found:
        for entity in block:
            layer = str(entity.dxf.get("layer", "") or "")
            kind = entity.dxftype()
            if kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
                label = clean_text(entity_text(entity)).upper()
                if AXIS_LABEL_RE.fullmatch(label) and AXIS_LAYER_RE.search(layer):
                    found = True
                    break
            elif kind == "LINE" and AXIS_GEOMETRY_LAYER_RE.search(layer) and not AXIS_AUXILIARY_LAYER_RE.search(layer):
                found = True
                break
            elif kind == "INSERT":
                child_name = str(entity.dxf.get("name", "") or "")
                if AXIS_LAYER_RE.search(f"{layer}|{child_name}") or _block_has_axis_content(
                    doc, child_name, memo, visiting,
                ):
                    found = True
                    break
    visiting.discard(block_name)
    memo[block_name] = found
    return found


def _axis_capable_blocks(doc: ezdxf.document.Drawing) -> set[str]:
    memo: dict[str, bool] = {}
    for block in doc.blocks:
        _block_has_axis_content(doc, block.name, memo, set())
    return {name for name, found in memo.items() if found}


def _virtual_children(entity: Any) -> list[Any]:
    try:
        return list(entity.virtual_entities())
    except Exception:
        return []


def _segment_intersection(
    a1: tuple[float, float], a2: tuple[float, float],
    b1: tuple[float, float], b2: tuple[float, float],
) -> tuple[float, float] | None:
    """Return the finite intersection of two non-parallel line segments."""
    ax, ay = a2[0] - a1[0], a2[1] - a1[1]
    bx, by = b2[0] - b1[0], b2[1] - b1[1]
    denominator = ax * by - ay * bx
    if abs(denominator) <= 1e-12:
        return None
    cx, cy = b1[0] - a1[0], b1[1] - a1[1]
    ta = (cx * by - cy * bx) / denominator
    tb = (cx * ay - cy * ax) / denominator
    epsilon = 1e-7
    if -epsilon <= ta <= 1.0 + epsilon and -epsilon <= tb <= 1.0 + epsilon:
        return a1[0] + ta * ax, a1[1] + ta * ay
    return None


def _registration_acceptance_level(
    inliers: int,
    required: int,
    median_residual: float,
    p95_residual: float,
    tolerance: float,
    scale: float,
    registration_policy: str,
) -> str:
    strict = (
        inliers >= required
        and median_residual <= tolerance * 0.45
        and p95_residual <= tolerance
    )
    if strict:
        return "strict"
    approximate = (
        registration_policy == "navigation"
        and inliers >= max(required * 4, 24)
        and median_residual <= tolerance * 0.55
        and p95_residual <= tolerance * 0.65
        and 0.2 <= scale <= 5.0
    )
    return "approximate" if approximate else "rejected"


def _sheet_for_point(sheets: list[Sheet], point: tuple[float, float]) -> Sheet | None:
    containing = [sheet for sheet in sheets if sheet.contains(*point)]
    if not containing:
        return None
    return min(containing, key=lambda item: (item.max_x - item.min_x) * (item.max_y - item.min_y))


def _features(doc: ezdxf.document.Drawing, sheets: list[Sheet]) -> dict[str, list[Feature]]:
    result: dict[str, list[Feature]] = defaultdict(list)
    seen: dict[str, set[tuple[str, int, int, str]]] = defaultdict(set)
    axis_segments: dict[str, list[tuple[tuple[float, float], tuple[float, float], float]]] = defaultdict(list)
    axis_blocks = _axis_capable_blocks(doc)

    def add_feature(sheet: Sheet, key: str, x: float, y: float, family: str) -> None:
        diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
        quantum = max(diagonal * 1e-9, 1e-7)
        fingerprint = (key, round(x / quantum), round(y / quantum), family)
        if fingerprint in seen[sheet.sheet_id]:
            return
        seen[sheet.sheet_id].add(fingerprint)
        result[sheet.sheet_id].append((key, x, y, family))

    def add_axis_text(entity: Any, inherited_layer: str = "") -> None:
        point = entity_point(entity)
        if not point:
            return
        sheet = _sheet_for_point(sheets, point)
        if not sheet:
            return
        layer = _effective_layer(entity, inherited_layer)
        label = clean_text(entity_text(entity)).upper()
        if AXIS_LABEL_RE.fullmatch(label) and AXIS_LAYER_RE.search(layer):
            add_feature(sheet, f"AXIS:{label}", point[0], point[1], "axis")

    def add_axis_line(entity: Any, inherited_layer: str = "") -> bool:
        layer = _effective_layer(entity, inherited_layer)
        if not AXIS_GEOMETRY_LAYER_RE.search(layer) or AXIS_AUXILIARY_LAYER_RE.search(layer):
            return False
        try:
            start, end = entity.dxf.start, entity.dxf.end
            a = (float(start.x), float(start.y))
            b = (float(end.x), float(end.y))
        except Exception:
            return False
        midpoint = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        sheet = _sheet_for_point(sheets, midpoint)
        if not sheet:
            return False
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
        if length < diagonal * 0.03:
            return False
        relative_length = length / max(diagonal, 1e-9)
        length_bin = round(math.log10(max(relative_length, 1e-9)), 1)
        add_feature(sheet, f"AXISLINE:{length_bin:.1f}", midpoint[0], midpoint[1], "axis")
        axis_segments[sheet.sheet_id].append((a, b, length_bin))
        return True

    top_level_inserts: list[Any] = []
    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind not in {"INSERT", "TEXT", "MTEXT", "LINE"}:
            continue
        point = entity_point(entity)
        if not point:
            continue
        sheet = _sheet_for_point(sheets, point)
        layer = str(entity.dxf.get("layer", ""))
        # A floor/xref wrapper is commonly inserted at drawing origin (0, 0)
        # while all of its transformed children lie inside a distant physical
        # sheet.  It must enter the recursive walker even when its own insertion
        # point is outside every detected sheet.
        if kind == "INSERT":
            top_level_inserts.append(entity)
            for attrib in getattr(entity, "attribs", []):
                add_axis_text(attrib, layer)
            if not sheet:
                continue
            if not PROFESSIONAL_LAYER_RE.search(layer):
                name = str(entity.dxf.get("name", ""))
                if name and not name.startswith(("*D", "*A", "$MODEL_SPACE", "$PAPER_SPACE")):
                    add_feature(sheet, f"BLOCK:{name.upper()}", point[0], point[1], "common")
            continue
        if not sheet:
            continue
        if kind in {"TEXT", "MTEXT"}:
            label = clean_text(entity_text(entity)).upper()
            if AXIS_LABEL_RE.fullmatch(label) and AXIS_LAYER_RE.search(layer):
                add_feature(sheet, f"AXIS:{label}", point[0], point[1], "axis")
            elif AXIS_LABEL_RE.fullmatch(label) and not PROFESSIONAL_LAYER_RE.search(layer):
                add_feature(sheet, f"TEXT:{label}", point[0], point[1], "common")
        elif kind == "LINE" and not PROFESSIONAL_LAYER_RE.search(layer):
            start, end = entity.dxf.start, entity.dxf.end
            length = math.hypot(float(end.x - start.x), float(end.y - start.y))
            if length > 0:
                if add_axis_line(entity):
                    continue
                # Line length is only a validation feature. Coarse logarithmic
                # bins tolerate unit/scale differences and avoid project units.
                length_bin = round(math.log10(max(length, 1e-9)), 1)
                add_feature(sheet, f"LINEBIN:{length_bin:.1f}", point[0], point[1], "common")

    # Resolve nested axis entities to actual model-space coordinates. Only
    # blocks known to lead to axis content are expanded, avoiding equipment
    # symbol explosions and keeping this usable for large generic drawings.
    visited_entities = 0
    stack: list[tuple[Any, str, int]] = []
    for insert in top_level_inserts:
        layer = _effective_layer(insert)
        name = str(insert.dxf.get("name", "") or "")
        if AXIS_LAYER_RE.search(f"{layer}|{name}") or name in axis_blocks:
            stack.append((insert, layer, 0))
    while stack and visited_entities < MAX_NESTED_AXIS_ENTITIES:
        insert, inherited_layer, depth = stack.pop()
        if depth >= MAX_NESTED_AXIS_DEPTH:
            continue
        for child in _virtual_children(insert):
            visited_entities += 1
            if visited_entities > MAX_NESTED_AXIS_ENTITIES:
                break
            layer = _effective_layer(child, inherited_layer)
            kind = child.dxftype()
            if kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
                add_axis_text(child, inherited_layer)
            elif kind == "LINE":
                add_axis_line(child, inherited_layer)
            elif kind == "INSERT":
                for attrib in getattr(child, "attribs", []):
                    add_axis_text(attrib, layer)
                name = str(child.dxf.get("name", "") or "")
                if AXIS_LAYER_RE.search(f"{layer}|{name}") or name in axis_blocks:
                    stack.append((child, layer, depth + 1))

    # Grid intersections provide additional anchors when labels are absent.
    # Their key uses normalized line lengths, remaining independent of project
    # coordinates, drawing scale, translation, and absolute rotation.
    for sheet in sheets:
        values = axis_segments.get(sheet.sheet_id, [])
        unique_segments: dict[tuple[int, int, int, int], tuple[tuple[float, float], tuple[float, float], float]] = {}
        diagonal = math.hypot(sheet.max_x - sheet.min_x, sheet.max_y - sheet.min_y)
        quantum = max(diagonal * 1e-8, 1e-6)
        for a, b, length_bin in values:
            ends = sorted(((round(a[0] / quantum), round(a[1] / quantum)), (round(b[0] / quantum), round(b[1] / quantum))))
            unique_segments.setdefault((*ends[0], *ends[1]), (a, b, length_bin))
        segments = list(unique_segments.values())[:400]
        emitted = 0
        for index, (a1, a2, abin) in enumerate(segments):
            adx, ady = a2[0] - a1[0], a2[1] - a1[1]
            alen = max(math.hypot(adx, ady), 1e-12)
            for b1, b2, bbin in segments[index + 1:]:
                bdx, bdy = b2[0] - b1[0], b2[1] - b1[1]
                blen = max(math.hypot(bdx, bdy), 1e-12)
                if abs((adx * bdx + ady * bdy) / (alen * blen)) > 0.35:
                    continue
                point = _segment_intersection(a1, a2, b1, b2)
                if point is None or not sheet.contains(*point):
                    continue
                low, high = sorted((abin, bbin))
                add_feature(sheet, f"AXISNODE:{low:.1f}:{high:.1f}", point[0], point[1], "axis")
                emitted += 1
                if emitted >= 2000:
                    break
            if emitted >= 2000:
                break
    return result


def _transform_point(transform: Transform, x: float, y: float) -> tuple[float, float]:
    scale, rotation, dx, dy = transform
    cos_r, sin_r = math.cos(rotation), math.sin(rotation)
    return scale * (cos_r * x - sin_r * y) + dx, scale * (sin_r * x + cos_r * y) + dy


def _similarity_from_pairs(
    source_a: tuple[float, float], source_b: tuple[float, float],
    target_a: tuple[float, float], target_b: tuple[float, float],
) -> Transform | None:
    sdx, sdy = source_b[0] - source_a[0], source_b[1] - source_a[1]
    tdx, tdy = target_b[0] - target_a[0], target_b[1] - target_a[1]
    source_length, target_length = math.hypot(sdx, sdy), math.hypot(tdx, tdy)
    if source_length <= 1e-9 or target_length <= 1e-9:
        return None
    scale = target_length / source_length
    if not (0.01 <= scale <= 100.0):
        return None
    rotation = math.atan2(tdy, tdx) - math.atan2(sdy, sdx)
    rotation = (rotation + math.pi) % (2.0 * math.pi) - math.pi
    cos_r, sin_r = math.cos(rotation), math.sin(rotation)
    dx = target_a[0] - scale * (cos_r * source_a[0] - sin_r * source_a[1])
    dy = target_a[1] - scale * (sin_r * source_a[0] + cos_r * source_a[1])
    return scale, rotation, dx, dy


def _correspondences(source_features: list[Feature], target_features: list[Feature]) -> list[tuple[Feature, Feature]]:
    target_by_key: dict[str, list[Feature]] = defaultdict(list)
    for feature in target_features:
        target_by_key[feature[0]].append(feature)
    result: list[tuple[Feature, Feature]] = []
    for source in source_features:
        targets = target_by_key.get(source[0], [])
        if len(targets) <= 50:
            result.extend((source, target) for target in targets)
    return result


def _balanced_pairs(
    pairs: list[tuple[Feature, Feature]],
    limit: int,
    per_key: int = 8,
) -> list[tuple[Feature, Feature]]:
    """Keep several labels represented instead of taking a drawing-order slice."""
    by_key: dict[str, list[tuple[Feature, Feature]]] = defaultdict(list)
    for pair in pairs:
        by_key[pair[0][0]].append(pair)
    selected: list[tuple[Feature, Feature]] = []
    keys = sorted(by_key, key=lambda key: (not key.startswith("AXIS:"), key))
    offset = 0
    while len(selected) < limit:
        added = False
        for key in keys:
            values = by_key[key]
            if offset < min(len(values), per_key):
                selected.append(values[offset])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        offset += 1
    return selected


def _score_transform(
    transform: Transform,
    source_features: list[Feature],
    target_features: list[Feature],
    tolerance: float,
    target_by_key: dict[str, list[Feature]] | None = None,
) -> Score:
    if target_by_key is None:
        target_by_key = defaultdict(list)
        for feature in target_features:
            target_by_key[feature[0]].append(feature)
    residuals: list[float] = []
    axis_residuals: list[float] = []
    axis_matches = common_matches = 0
    unique_keys: set[str] = set()
    for key, sx, sy, family in source_features:
        targets = target_by_key.get(key, [])
        if not targets or len(targets) > 100:
            continue
        tx_expected, ty_expected = _transform_point(transform, sx, sy)
        best = min(math.hypot(tx - tx_expected, ty - ty_expected) for _, tx, ty, _ in targets)
        if best <= tolerance:
            residuals.append(best)
            unique_keys.add(key)
            if family == "axis":
                axis_matches += 1
                axis_residuals.append(best)
            else:
                common_matches += 1
    return (
        len(residuals), axis_matches, common_matches,
        median(residuals), percentile(residuals, 0.95), residuals, axis_residuals,
    )


def _transform_rank(score: Score, tolerance: float) -> tuple[float, int, int, int, int, float, float]:
    """Rank broad support with a continuous residual penalty.

    A few extra loose matches must not beat hundreds of exact matches. Axis-line
    support receives extra weight but is penalized by residual in the same way.
    """
    inliers, axis_matches, _, med, p95, residuals, axis_residuals = score

    def quality(values: list[float]) -> float:
        return sum((1.0 - min(value / tolerance, 1.0)) ** 2 for value in values)

    tight_matches = sum(value <= tolerance * 0.10 for value in residuals)
    tight_axis_matches = sum(value <= tolerance * 0.10 for value in axis_residuals)
    weighted_support = quality(residuals) + 3.0 * quality(axis_residuals)
    return (
        round(weighted_support, 9), tight_axis_matches, tight_matches,
        axis_matches, inliers, -med, -p95,
    )


def _refine_translation(
    transform: Transform,
    source_features: list[Feature],
    target_features: list[Feature],
    search_radius: float,
    target_by_key: dict[str, list[Feature]] | None = None,
) -> Transform:
    """Remove a consistent small sheet-origin offset after scale/rotation fit."""
    if target_by_key is None:
        target_by_key = defaultdict(list)
        for feature in target_features:
            target_by_key[feature[0]].append(feature)
    delta_x: list[float] = []
    delta_y: list[float] = []
    for key, sx, sy, _ in source_features:
        targets = target_by_key.get(key, [])
        if not targets or len(targets) > 100:
            continue
        expected_x, expected_y = _transform_point(transform, sx, sy)
        _, tx, ty, _ = min(
            targets,
            key=lambda item: math.hypot(item[1] - expected_x, item[2] - expected_y),
        )
        if math.hypot(tx - expected_x, ty - expected_y) <= search_radius:
            delta_x.append(tx - expected_x)
            delta_y.append(ty - expected_y)
    if len(delta_x) < 3:
        return transform
    scale, rotation, dx, dy = transform
    return scale, rotation, dx + median(delta_x), dy + median(delta_y)


def _candidate_transforms(source: Sheet, target: Sheet, pairs: list[tuple[Feature, Feature]]) -> list[Transform]:
    source_width, source_height = source.max_x - source.min_x, source.max_y - source.min_y
    target_width, target_height = target.max_x - target.min_x, target.max_y - target.min_y
    ratios = [
        target_width / source_width if source_width > 0 else 1.0,
        target_height / source_height if source_height > 0 else 1.0,
    ]
    bbox_scale = median([value for value in ratios if 0.01 <= value <= 100.0])
    title_dx = target.center_x - bbox_scale * source.center_x
    title_dy = target.center_y - bbox_scale * source.center_y
    candidates: list[Transform] = [(bbox_scale, 0.0, title_dx, title_dy)]

    translations = _balanced_pairs(pairs, limit=80, per_key=4)
    for source_feature, target_feature in translations:
        candidates.append((1.0, 0.0, target_feature[1] - source_feature[1], target_feature[2] - source_feature[2]))

    # Deterministic bounded RANSAC. Named axes receive first priority; balanced
    # sampling prevents one repeated anonymous block from consuming the budget.
    axis_pairs = [pair for pair in pairs if pair[0][3] == "axis"]
    axis_keys = {pair[0][0] for pair in axis_pairs}
    seed_pairs = axis_pairs if len(axis_keys) >= 2 else pairs
    useful = _balanced_pairs(seed_pairs, limit=96, per_key=8)
    source_diagonal = math.hypot(source_width, source_height)
    target_diagonal = math.hypot(target_width, target_height)
    generated = 0
    for index, (source_a, target_a) in enumerate(useful):
        for source_b, target_b in useful[index + 1:]:
            if source_a[0] == source_b[0]:
                continue
            if math.hypot(source_b[1] - source_a[1], source_b[2] - source_a[2]) < source_diagonal * 0.03:
                continue
            if math.hypot(target_b[1] - target_a[1], target_b[2] - target_a[2]) < target_diagonal * 0.03:
                continue
            transform = _similarity_from_pairs(
                (source_a[1], source_a[2]), (source_b[1], source_b[2]),
                (target_a[1], target_a[2]), (target_b[1], target_b[2]),
            )
            if transform:
                candidates.append(transform)
                generated += 1
            if generated >= 480:
                break
        if generated >= 480:
            break

    # Avoid rescoring numerically identical hypotheses produced by repeated axis
    # symbols at both ends of a grid line.
    unique: dict[tuple[float, float, float, float], Transform] = {}
    translation_quantum = max(target_diagonal * 1e-7, 1e-7)
    for scale, rotation, dx, dy in candidates:
        key = (
            round(scale, 8), round(rotation, 8),
            round(dx / translation_quantum), round(dy / translation_quantum),
        )
        unique.setdefault(key, (scale, rotation, dx, dy))
    return list(unique.values())


def register_sheet(
    source: Sheet,
    target: Sheet,
    source_features: list[Feature],
    target_features: list[Feature],
    registration_policy: str = "navigation",
) -> Registration:
    source_axis_keys = {feature[0] for feature in source_features if feature[3] == "axis"}
    target_axis_keys = {feature[0] for feature in target_features if feature[3] == "axis"}
    shared_axis_keys = source_axis_keys & target_axis_keys
    shared_named_axis_keys = {key for key in shared_axis_keys if key.startswith("AXIS:")}
    shared_axis_line_keys = {
        key for key in shared_axis_keys
        if key.startswith("AXISLINE:") or key.startswith("AXISNODE:")
    }
    axis_only_fit = len(shared_named_axis_keys) >= 2 or len(shared_axis_line_keys) >= 3
    fit_source_features = (
        [feature for feature in source_features if feature[3] == "axis"]
        if axis_only_fit else source_features
    )
    fit_target_features = (
        [feature for feature in target_features if feature[3] == "axis"]
        if axis_only_fit else target_features
    )
    pairs = _correspondences(fit_source_features, fit_target_features)
    target_by_key: dict[str, list[Feature]] = defaultdict(list)
    for feature in fit_target_features:
        target_by_key[feature[0]].append(feature)
    target_diagonal = math.hypot(target.max_x - target.min_x, target.max_y - target.min_y)
    tolerance = max(target_diagonal * 0.006, 1e-6)
    best_transform: Transform | None = None
    best_score: Score | None = None
    for transform in _candidate_transforms(source, target, pairs):
        score = _score_transform(
            transform, fit_source_features, fit_target_features, tolerance,
            target_by_key=target_by_key,
        )
        rank = _transform_rank(score, tolerance)
        if best_score is None:
            best_transform, best_score = transform, score
            best_rank = rank
        elif rank > best_rank:
            best_transform, best_score, best_rank = transform, score, rank

    if best_transform is not None and best_score is not None:
        refined = _refine_translation(
            best_transform, fit_source_features, fit_target_features, search_radius=tolerance * 2.0,
            target_by_key=target_by_key,
        )
        refined_score = _score_transform(
            refined, fit_source_features, fit_target_features, tolerance,
            target_by_key=target_by_key,
        )
        refined_rank = _transform_rank(refined_score, tolerance)
        if refined_rank > best_rank:
            best_transform, best_score, best_rank = refined, refined_score, refined_rank

    if best_transform is None or best_score is None:
        best_transform = (1.0, 0.0, target.center_x - source.center_x, target.center_y - source.center_y)
        best_score = (0, 0, 0, math.inf, math.inf, [], [])
    scale, rotation, dx, dy = best_transform
    inliers, axis_matches, common_matches, med, p95, _, _ = best_score
    if axis_matches >= 3 and len(shared_named_axis_keys) >= 2:
        method, required = "named_axis_similarity", 3
    elif axis_matches >= 4 and shared_axis_line_keys:
        method, required = "axis_line_geometry_similarity", 4
    else:
        method, required = "common_geometry_similarity", 8
    # 导航图只需给出巡检对象的大致位置，因此允许一档可审计的近似配准。
    # 仍要求大量公共几何内点、较小的相对残差及合理比例，绝不接受无锚点的图框中心硬套。
    acceptance_level = _registration_acceptance_level(
        inliers, required, med, p95, tolerance, scale, registration_policy,
    )
    strict_accepted = acceptance_level == "strict"
    approximate_accepted = acceptance_level == "approximate"
    accepted = acceptance_level != "rejected"
    confidence = (
        min(0.99, 0.45 + 0.06 * min(inliers, 8))
        if strict_accepted else
        max(0.50, 0.72 - 0.25 * (med / tolerance))
        if approximate_accepted else 0.0
    )
    reason = (
        f"通过：{method}，内点{inliers}，比例{scale:.8f}，旋转{math.degrees(rotation):.6f}°，"
        f"中位残差{med:.3f}，P95={p95:.3f}，阈值{tolerance:.3f}"
        if strict_accepted else
        f"近似通过（导航级）：{method}，内点{inliers}，比例{scale:.8f}，旋转{math.degrees(rotation):.6f}°，"
        f"中位残差{med:.3f}，P95={p95:.3f}，阈值{tolerance:.3f}；按导航级标准自动接受"
        if approximate_accepted else
        f"拒绝：配准证据不足或残差超限，方法{method}，内点{inliers}/{required}，"
        f"中位残差{med:.3f}，P95={p95:.3f}，阈值{tolerance:.3f}"
    )
    return Registration(
        source_drawing=source.drawing, source_kind=source.kind, floor=source.floor,
        target_drawing=target.drawing, method=method, accepted=accepted,
        scale_x=scale, scale_y=scale, translate_x=dx, translate_y=dy,
        title_translate_x=target.center_x - source.center_x,
        title_translate_y=target.center_y - source.center_y,
        axis_matches=axis_matches, common_matches=common_matches, inlier_matches=inliers,
        median_residual=med, p95_residual=p95, reason=reason,
        rotation_deg=math.degrees(rotation), source_sheet_id=source.sheet_id,
        target_sheet_id=target.sheet_id, source_building_ids=list(source.building_ids),
        target_building_ids=list(target.building_ids), source_floors=list(source.floors),
        target_floors=list(target.floors), fit_tolerance=tolerance, confidence=confidence,
        acceptance_level=acceptance_level,
    )


def _scope_compatible(source: Sheet, target: Sheet) -> bool:
    if source.floor_ids and target.floor_ids and not (source.floor_ids & target.floor_ids):
        return False
    if source.building_set and target.building_set and not (source.building_set & target.building_set):
        return False
    return True


def _frame_scale(source: Sheet, target: Sheet) -> tuple[float, float] | None:
    """Return an orientation-preserving frame scale and its relative disagreement."""
    source_width = source.max_x - source.min_x
    source_height = source.max_y - source.min_y
    target_width = target.max_x - target.min_x
    target_height = target.max_y - target.min_y
    if min(source_width, source_height, target_width, target_height) <= 0:
        return None
    scale_x, scale_y = target_width / source_width, target_height / source_height
    scale = (scale_x + scale_y) / 2.0
    disagreement = abs(scale_x - scale_y) / max(abs(scale), 1e-9)
    if not (0.05 <= scale <= 20.0) or disagreement > 0.005:
        return None
    return scale, disagreement


def _centered_frame_transform(source: Sheet, target: Sheet, scale: float, rotation: float = 0.0) -> Transform:
    cos_r, sin_r = math.cos(rotation), math.sin(rotation)
    mapped_center_x = scale * (cos_r * source.center_x - sin_r * source.center_y)
    mapped_center_y = scale * (sin_r * source.center_x + cos_r * source.center_y)
    return scale, rotation, target.center_x - mapped_center_x, target.center_y - mapped_center_y


def _apply_transform(
    registration: Registration,
    transform: Transform,
    method: str,
    acceptance_level: str,
    confidence: float,
    support: int,
    evidence: str,
) -> None:
    scale, rotation, dx, dy = transform
    registration.accepted = True
    registration.method = method
    registration.acceptance_level = acceptance_level
    registration.confidence = confidence
    registration.scale_x = scale
    registration.scale_y = scale
    registration.rotation_deg = math.degrees(rotation)
    registration.translate_x = dx
    registration.translate_y = dy
    registration.fallback_support = support
    registration.fallback_evidence = evidence
    registration.reason = (
        f"通过：{method}，比例{scale:.8f}，旋转{registration.rotation_deg:.6f}°，"
        f"平移({dx:.3f},{dy:.3f})；{evidence}"
    )


def _apply_manual_anchors(
    registrations: list[Registration],
    anchors_path: Path | None,
) -> int:
    if anchors_path is None:
        return 0
    rows = read_json(anchors_path.resolve())
    if not isinstance(rows, list):
        raise ValueError("人工锚点文件必须是JSON数组")
    applied = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("人工锚点数组中的每一项必须是对象")
        matches = [
            item for item in registrations
            if (
                (row.get("source_sheet_id") and item.source_sheet_id == row.get("source_sheet_id"))
                or (
                    not row.get("source_sheet_id")
                    and item.source_kind == row.get("source_kind")
                    and item.floor == row.get("floor")
                )
            )
        ]
        if len(matches) != 1:
            raise ValueError(f"人工锚点无法唯一匹配配准记录:{row}")
        source_points = row.get("source_points") or []
        target_points = row.get("target_points") or []
        if len(source_points) != len(target_points) or len(source_points) < 2:
            raise ValueError("每条人工锚点至少需要2组一一对应的source_points/target_points")
        transform = _similarity_from_pairs(
            (float(source_points[0][0]), float(source_points[0][1])),
            (float(source_points[1][0]), float(source_points[1][1])),
            (float(target_points[0][0]), float(target_points[0][1])),
            (float(target_points[1][0]), float(target_points[1][1])),
        )
        if transform is None:
            raise ValueError(f"人工锚点两点重合，无法计算变换:{row}")
        residuals = [
            math.hypot(
                _transform_point(transform, float(source[0]), float(source[1]))[0] - float(target[0]),
                _transform_point(transform, float(source[0]), float(source[1]))[1] - float(target[1]),
            )
            for source, target in zip(source_points, target_points)
        ]
        registration = matches[0]
        if max(residuals) > max(registration.fit_tolerance * 0.25, 1e-6):
            raise ValueError(f"人工锚点互相矛盾，最大残差{max(residuals):.3f}:{row}")
        registration.median_residual = median(residuals)
        registration.p95_residual = percentile(residuals, 0.95)
        _apply_transform(
            registration, transform, "manual_anchor_similarity", "manual", 0.99,
            len(source_points), f"人工提供{len(source_points)}组对应点，P95={registration.p95_residual:.3f}",
        )
        applied += 1
    return applied


def _apply_frame_consensus(
    registrations: list[Registration],
    sheets: list[Sheet],
    candidate_counts: dict[str, int],
    registration_policy: str,
) -> int:
    """Recover no-anchor sheets from repeated, high-confidence physical frames.

    The rule is project-independent: at least three floors in one source drawing,
    unambiguous floor/building pairing, high-confidence physical frames, and a
    consistent orientation-preserving scale are all required.
    """
    if registration_policy != "navigation":
        return 0
    sheet_map = {item.sheet_id: item for item in sheets}
    groups: dict[tuple[str, str], list[Registration]] = defaultdict(list)
    for item in registrations:
        groups[(item.source_drawing, item.source_kind)].append(item)
    applied = 0
    for group in groups.values():
        if len({item.floor for item in group}) < 3:
            continue
        frame_rows: list[tuple[Registration, Sheet, Sheet, float]] = []
        for item in group:
            source = sheet_map.get(item.source_sheet_id)
            target = sheet_map.get(item.target_sheet_id)
            if source is None or target is None or candidate_counts.get(item.source_sheet_id, 0) != 1:
                continue
            if source.confidence < 0.85 or target.confidence < 0.85:
                continue
            if "frame" not in source.detection_method or "frame" not in target.detection_method:
                continue
            scale_result = _frame_scale(source, target)
            if scale_result is None:
                continue
            frame_rows.append((item, source, target, scale_result[0]))
        if len(frame_rows) < 3:
            continue
        group_scale = median([row[3] for row in frame_rows])
        if max(abs(row[3] - group_scale) / max(group_scale, 1e-9) for row in frame_rows) > 0.005:
            continue
        reliable = [
            item for item in group
            if item.accepted and item.acceptance_level in {"strict", "manual"}
        ]
        reliable_scales = [item.scale_x for item in reliable]
        reliable_rotations = [math.radians(item.rotation_deg) for item in reliable]
        if len(reliable) >= 3:
            consensus_scale = median(reliable_scales)
            consensus_rotation = median(reliable_rotations)
            if max(abs(value - consensus_scale) / max(abs(consensus_scale), 1e-9) for value in reliable_scales) > 0.01:
                continue
            if max(abs(value - consensus_rotation) for value in reliable_rotations) > math.radians(0.5):
                continue
            method = "series_frame_consensus"
            evidence = f"同一专业文件{len(reliable)}张楼层已有可靠配准，当前物理图框比例与系列一致"
            confidence = 0.78
        else:
            consensus_scale = group_scale
            consensus_rotation = 0.0
            method = "multi_floor_frame_consensus"
            evidence = f"同一专业文件{len(frame_rows)}张楼层物理图框的比例和方向一致，楼层配对唯一"
            confidence = 0.66
        for item, source, target, frame_scale in frame_rows:
            if item.accepted:
                continue
            if abs(frame_scale - consensus_scale) / max(abs(consensus_scale), 1e-9) > 0.01:
                continue
            transform = _centered_frame_transform(source, target, consensus_scale, consensus_rotation)
            _apply_transform(
                item, transform, method, "approximate", confidence,
                len(reliable) if reliable else len(frame_rows), evidence,
            )
            applied += 1
    return applied


def run_stage(
    prepared_payload: dict[str, object],
    sheets_path: Path,
    stage_dir: Path,
    registration_policy: str = "navigation",
    manual_anchors_path: Path | None = None,
) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    sheets = [Sheet(**item) for item in read_json(sheets_path)]
    building_item = next(item for item in prepared_payload["drawings"] if item["discipline"] == "building")
    target_path = Path(str(building_item["dxf"])).resolve()
    target_sheets = [sheet for sheet in sheets if sheet.discipline == "building"]
    target_doc = ezdxf.readfile(target_path)
    target_feature_map = _features(target_doc, target_sheets)

    registrations: list[Registration] = []
    unmatched: list[dict[str, Any]] = []
    candidate_counts: dict[str, int] = {}
    for drawing in [
        item for item in prepared_payload["drawings"]
        if item["discipline"] in {"water", "electrical", "hvac"}
    ]:
        source_path = Path(str(drawing["dxf"])).resolve()
        source_sheets = [sheet for sheet in sheets if Path(sheet.drawing) == source_path]
        source_doc = ezdxf.readfile(source_path)
        source_feature_map = _features(source_doc, source_sheets)
        for source_sheet in source_sheets:
            candidates = [target for target in target_sheets if _scope_compatible(source_sheet, target)]
            candidate_counts[source_sheet.sheet_id] = len(candidates)
            if not candidates:
                unmatched.append(
                    {"source_drawing": str(source_path), "source_sheet_id": source_sheet.sheet_id,
                     "title": source_sheet.title, "floors": source_sheet.floors,
                     "building_ids": source_sheet.building_ids, "reason": "建筑图中没有楼栋/楼层范围相交的目标平面"}
                )
                continue
            attempts = [
                register_sheet(
                    source_sheet, target,
                    source_feature_map.get(source_sheet.sheet_id, []),
                    target_feature_map.get(target.sheet_id, []),
                    registration_policy=registration_policy,
                )
                for target in candidates
            ]
            registrations.append(
                max(
                    attempts,
                    key=lambda item: (item.accepted, item.confidence, item.axis_matches, item.inlier_matches, -item.median_residual),
                )
            )

    manual_accepted = _apply_manual_anchors(registrations, manual_anchors_path)
    frame_consensus_accepted = _apply_frame_consensus(
        registrations, sheets, candidate_counts, registration_policy,
    )

    rows = asdict_list(registrations)
    write_json(stage_dir / "registrations.json", rows)
    write_csv(stage_dir / "registrations.csv", rows)
    write_json(stage_dir / "unmatched_sheets.json", unmatched)
    write_csv(stage_dir / "unmatched_sheets.csv", unmatched)
    fallback_rows = [
        row for row in rows
        if row.get("fallback_support") or row.get("acceptance_level") == "manual"
    ]
    write_json(stage_dir / "fallback_registrations.json", fallback_rows)
    write_csv(stage_dir / "fallback_registrations.csv", fallback_rows)
    payload = {
        "stage": "04_register", "registration_count": len(registrations),
        "accepted": sum(item.accepted for item in registrations),
        "strict_accepted": sum(item.acceptance_level == "strict" for item in registrations),
        "approximate_accepted": sum(item.acceptance_level == "approximate" for item in registrations),
        "manual_accepted": sum(item.acceptance_level == "manual" for item in registrations),
        "frame_consensus_accepted": frame_consensus_accepted,
        "manual_anchor_records_applied": manual_accepted,
        "rejected": sum(not item.accepted for item in registrations),
        "unmatched": len(unmatched),
        "registration_policy": registration_policy,
        "registrations_json": str((stage_dir / "registrations.json").resolve()),
    }
    write_json(stage_dir / "stage_summary.json", payload)
    return payload
