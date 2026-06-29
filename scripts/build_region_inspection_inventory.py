# -*- coding: utf-8 -*-
"""
区域级巡检对象清单构建脚本。
功能边界：
1. 输入 cad_vector_inventory_agent.py 生成的 CAD 全量 inventory；
2. 输入 detect_dxf_sheets_floors.py 生成的图幅 / 楼层 / inspection_region 结果；
3. 按 inspection_region 将 CAD 语义对象切分为每层 / 每区域清单；
4. 使用本地规则库优先识别巡检对象；
5. 仅把本地规则无法确定的少量文本候选交给 LLM 兜底判断；
6. 输出区域级 inspection_objects.json 和全局 region_inspection_results.json。
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 本地巡检对象规则库：提供别名匹配、关键词模式匹配、文本归一化和说明性噪声过滤。
from agents.inspection_object_library import (
    is_non_object_explanatory_text,
    match_inspection_keyword_pattern,
    match_inspection_object,
    normalize_value,
)

# 输入 / 输出文件名约定。
FULL_INVENTORY_FILE = "cad_object_inventory.csv"
SEMANTIC_INVENTORY_FILE = "cad_semantic_inventory.csv"
SEMANTIC_ENTITY_TYPES = {"TEXT", "MTEXT", "ATTRIB", "INSERT"}
RESULT_FILE = "region_inspection_results.json"
LLM_INPUT_FILE = "region_llm_candidates.json"
LLM_OUTPUT_FILE = "region_llm_classified.json"
SCHEMA_VERSION = 1

# 通用 CAD 噪声词：这些词本身不能作为巡检对象名称。
GENERIC_TERMS = {
    "", "0", "DEFPOINTS", "TEXT", "PUB_TEXT", "PUB_DIM", "DIM", "AXIS",
    "NOTE", "ANNO", "HATCH", "SOLID", "LINE", "LWPOLYLINE", "POLYLINE",
    "CONTINUOUS", "BYLAYER", "MODEL", "图框", "标题栏", "图签", "说明",
    "标注", "轴线", "填充", "文字",
}
NORMALIZED_GENERIC_TERMS = {normalize_value(item) for item in GENERIC_TERMS}
# 通用 CAD 噪声词：这些词本身不能作为巡检对象名称。
GENERIC_TERMS = {normalize_value(item) for item in GENERIC_TERMS}
NOISE_PATTERN = re.compile(
    r"^(?:[-+]?\d+(?:\.\d+)?|\d+[xX×]\d+)$",
    re.IGNORECASE,
)
CONTEXT_FIELDS = ("layer", "parent_block_name", "entity_type", "geometry_kind")
# 必须依赖文本证据的对象类别：例如房间名称、井、开闭所等，仅靠 block 名不够可靠。
TEXT_EVIDENCE_ONLY_CLASSES = {
    "避难间",
    "消防水泵房/消防泵房",
    "开闭所",
    "用户变",
    "配电房",
    "消防控制室/消控室",
    "风机房",
    "进风机房",
    "补风机房",
    "强电间",
    "弱电间",
    "强弱电间",
    "电井",
    "储存装置间/灭火剂储存装置/驱动装置",
    "供水水源/消防水池",
    "消防水泵",
    "备用发电机/柴油发电机房",
    "变配电房",
}
TEXT_EVIDENCE_ONLY_CLASS_KEYS = {normalize_value(item) for item in TEXT_EVIDENCE_ONLY_CLASSES}
# 文本或 block 均可作为证据的对象类别：例如安全出口、灭火器、喷头等。
TEXT_OR_BLOCK_CLASSES = {
    "安全出口",
    "排烟机",
    "消防电梯",
    "防火卷帘",
    "灭火器",
    "供水装置",
    "报警阀组",
    "喷头",
    "消防水箱",
    "室外（内）消火栓",
    "灭火装置",
    "管网与喷头",
    "消防应急照明和疏散指示标志",
    "火灾探测器",
    "消防通讯",
    "布线",
    "应急广播及警报装置",
    "区域显示器",
    "手动报警按钮",
    "火灾报警控制器",
    "消防联动控制器及消防控制室图形显示装置",
}
TEXT_OR_BLOCK_CLASS_KEYS = {normalize_value(item) for item in TEXT_OR_BLOCK_CLASSES}
# 消防巡检相关语义提示词：用于判断短文本是否具有业务上下文。
INSPECTION_SEMANTIC_HINTS = (
    "消防", "消火", "灭火", "报警", "应急", "疏散", "安全出口", "避难",
    "排烟", "补风", "进风", "排风", "风机", "水泵", "喷头", "水箱", "水池",
    "卷帘", "配电", "变配电", "强电", "弱电", "电井", "控制室",
    "发电机", "柴油发电机", "火灾", "探测器", "广播", "联动",
    "电气", "取水", "给水", "排水", "雨水", "清水", "水井", "回收池", "集气",
    "燃气", "暖通", "通风", "风井", "空调", "管道", "设备", "设备间",
)
# 标题栏 / 图框块名标记：这些区域中的文本通常不是巡检对象。
TITLE_BLOCK_PARENT_MARKERS = (
    "PMSHEET", "TITLE", "TK_LABEL", "TK-LABEL", "图框", "图签", "标题栏",
    "会签", "DRAWINGTITLE", "SHEET", "BORDER",
)
# 明确属于图纸说明、轴网、尺寸、剖面、详图等非巡检对象的噪声词。
EXPLICIT_DRAWING_NOISE_TERMS = (
    "轴网", "轴线", "标注", "尺寸", "图框", "图例", "剖面", "剖面图",
    "剖面符号", "断面", "立面", "详图", "大样", "大样图", "设计说明",
    "施工说明", "说明", "索引", "图号", "比例", "标高", "材料表",
    "设备表", "目录", "会签", "地下车库", "平面图", "系统图", "原理图",
    "示意图", "节点", "节点图", "轴", "轴号", "轴圈", "定位轴",
    "深度", "宽度", "高度", "长度", "半径", "直径", "坡度", "面积",
    "容积", "基坑", "AXIS", "DIM", "DIMENSION", "NOTE",
    "ANNO", "ANNOTATION", "LEGEND", "SECTION", "ELEVATION", "DETAIL",
    "SCHEDULE", "DRAWING", "TITLE", "BORDER",
)
MEASUREMENT_OR_FLOOR_RANGE_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:m|mm|cm|kg|kw|kva|%|㎡|m2|m3)\b|[-+]?\d+\s*[fF]\s*(?:至|~|-|到)\s*[-+]?\d+\s*[fF])",
    re.IGNORECASE,
)
FLOOR_CODE_ONLY_PATTERN = re.compile(
    r"^(?:[bB]\d+(?:[-_~至到][bB]?\d+)?|\d+[fF]|[-+]?\d+(?:[-_~至到]\d+)?[fF]|[-+]?\d+[-_~至到]\d+[fF]?)$",
    re.IGNORECASE,
)
DISCIPLINE_TITLE_TERMS = (
    "WATER SUPPLY", "WATERSUPPLY", "DRAIN", "DRAINAGE", "ELECTRICAL",
    "HVAC", "FIRE PROTECTION", "FIREPROTECTION", "ARCHITECTURE",
    "STRUCTURE", "PLUMBING", "MECHANICAL", "给排水", "暖通", "电气",
    "建筑", "结构", "消防设计", "消防平面", "防火分区",
)
INSPECTION_HINTS = (
    "消防", "消火", "灭火", "报警", "应急", "疏散", "安全出口", "避难",
    "排烟", "补风", "进风", "排风", "风机", "水泵", "喷头", "水箱", "水池",
    "卷帘", "配电", "强电", "弱电", "电井", "控制室", "发电机",
    "电气", "取水", "给水", "排水", "雨水", "清水", "水井", "回收池", "集气",
    "燃气", "暖通", "通风", "风井", "空调", "管道", "设备", "设备间",
)
HARD_EXCLUSION_TERMS = (
    "图框", "图签", "标题栏", "设计说明", "施工说明", "材料表", "设备表",
    "详图", "大样图", "剖面图", "立面图", "系统图", "索引图", "平面图",
    "轴线", "尺寸", "标高", "比例", "做法", "图号", "编号", "日期",
)
EXCLUDED_TEXT_LAYER_MARKERS = (
    "TK_LABEL", "TK-LABEL", "TITLE", "NOPRINT", "PUB_DIM", "DIM_",
    "AXIS", "ANNO", "NOTE", "图框", "图签", "标题栏", "索引",
)
# 受保护编号前缀：这些编号可能对应消防 / 设备对象，不能按普通编号直接过滤。
PROTECTED_CODE_PREFIXES = ("FJ", "XF", "SB")
MEP_OBJECT_MARKERS = (
    "消防", "消火", "取水", "给水", "排水", "雨水", "清水", "回收池",
    "水池", "水箱", "水泵", "水", "电气", "配电", "强电", "弱电", "电", "集气",
    "燃气", "气", "暖通", "通风", "空调", "排烟", "补风", "排风", "送风", "风", "管道", "设备",
)
OBJECT_NOUN_MARKERS = ("间", "房", "室", "厅", "井", "口", "池", "泵", "阀", "机", "箱", "柜", "装置", "设备")


def read_json(path: Path) -> Any:
    """读取 UTF-8 JSON 文件并返回 Python 对象。"""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    """将 Python 对象写为 UTF-8 JSON 文件，自动创建父目录并保留中文。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_semantic_inventory(inventory_dir: Path) -> tuple[Path, bool]:
    """确保 inventory 目录中存在 cad_semantic_inventory.csv；旧缓存没有该文件时，从全量 inventory 中筛选语义实体生成。"""
    semantic_path = inventory_dir / SEMANTIC_INVENTORY_FILE
    if semantic_path.exists():
        return semantic_path, False

    full_path = inventory_dir / FULL_INVENTORY_FILE
    if not full_path.exists():
        raise FileNotFoundError(full_path)

    temp_path = semantic_path.with_name(f"{semantic_path.name}.{os.getpid()}.tmp")
    semantic_count = 0
    try:
        with full_path.open("r", encoding="utf-8-sig", newline="") as source, temp_path.open(
                "w", encoding="utf-8-sig", newline=""
        ) as target:
            reader = csv.DictReader(source)
            fieldnames = list(reader.fieldnames or [])
            writer = csv.DictWriter(target, fieldnames=fieldnames)
            writer.writeheader()
            for row in reader:
                if str(row.get("entity_type", "")).upper() in SEMANTIC_ENTITY_TYPES:
                    writer.writerow(row)
                    semantic_count += 1
        temp_path.replace(semantic_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    manifest_path = inventory_dir / "inventory_manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        manifest.setdefault("counts", {})["semantic_inventory_objects"] = semantic_count
        manifest.setdefault("output_files", {})["cad_semantic_inventory"] = str(semantic_path.resolve())
        write_json(manifest_path, manifest)
    return semantic_path, True


def safe_float(value: Any) -> float | None:
    """将输入安全转换为有限浮点数；无法转换或为 NaN/inf 时返回 None。"""
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def row_bbox(row: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """从 inventory 行中读取 bbox_minx/bbox_miny/bbox_maxx/bbox_maxy 并返回有效 bbox。"""
    values = [safe_float(row.get(key)) for key in ("bbox_minx", "bbox_miny", "bbox_maxx", "bbox_maxy")]
    if any(value is None for value in values):
        return None
    minx, miny, maxx, maxy = (float(value) for value in values)
    return (minx, miny, maxx, maxy) if maxx >= minx and maxy >= miny else None


def sheet_bbox_from_keys(sheet: dict[str, Any], prefix: str) -> tuple[float, float, float, float] | None:
    """从 sheet 字典的指定前缀字段中读取 bbox，例如 inspection_region_minx。"""
    values = [
        safe_float(sheet.get(f"{prefix}_minx")),
        safe_float(sheet.get(f"{prefix}_miny")),
        safe_float(sheet.get(f"{prefix}_maxx")),
        safe_float(sheet.get(f"{prefix}_maxy")),
    ]
    if any(value is None for value in values):
        return None
    minx, miny, maxx, maxy = (float(value) for value in values)
    return (minx, miny, maxx, maxy) if maxx > minx and maxy > miny else None


def sheet_bbox(sheet: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """优先读取 sheet 的 inspection_region_bbox；缺失时回退到 inspection_region_* 或 sheet bbox。"""
    raw = sheet.get("inspection_region_bbox")
    if isinstance(raw, list) and len(raw) == 4:
        values = [safe_float(value) for value in raw]
        if not any(value is None for value in values):
            minx, miny, maxx, maxy = (float(value) for value in values)
            if maxx > minx and maxy > miny:
                return minx, miny, maxx, maxy
    keyed = sheet_bbox_from_keys(sheet, "inspection_region")
    if keyed:
        return keyed

    raw = sheet.get("bbox")
    if isinstance(raw, list) and len(raw) == 4:
        values = [safe_float(value) for value in raw]
    else:
        values = [safe_float(sheet.get(key)) for key in ("bbox_minx", "bbox_miny", "bbox_maxx", "bbox_maxy")]
    if any(value is None for value in values):
        return None
    minx, miny, maxx, maxy = (float(value) for value in values)
    return (minx, miny, maxx, maxy) if maxx > minx and maxy > miny else None


def region_id_safe(value: Any, default: str) -> str:
    """将区域编号清洗成适合作为目录名 / 文件名的一段安全字符串。"""
    text = str(value or default).strip() or default
    text = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", text).strip("_")
    return text or default


def sheet_region_entries(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    """从单个 sheet 中提取 inspection_regions；如果不存在多区域结果，则回退为单个 sheet bbox 区域。"""
    raw_regions = sheet.get("inspection_regions")
    entries: list[dict[str, Any]] = []
    if isinstance(raw_regions, list):
        for index, region in enumerate(raw_regions, start=1):
            if not isinstance(region, dict):
                continue
            raw_bbox = region.get("bbox")
            if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
                continue
            values = [safe_float(value) for value in raw_bbox]
            if any(value is None for value in values):
                continue
            minx, miny, maxx, maxy = (float(value) for value in values)
            if maxx <= minx or maxy <= miny:
                continue
            entries.append(
                {
                    "region_id": region_id_safe(region.get("region_id"), f"R{index:02d}"),
                    "bbox": (minx, miny, maxx, maxy),
                    "source": str(region.get("source") or sheet.get("inspection_region_source") or "inspection_region"),
                    "confidence": safe_float(region.get("confidence")) or safe_float(
                        sheet.get("inspection_region_confidence")) or 0.0,
                    "evidence": str(region.get("evidence") or sheet.get("inspection_region_evidence") or ""),
                }
            )
    if entries:
        return entries

    bbox = sheet_bbox(sheet)
    if not bbox:
        return []
    return [
        {
            "region_id": "R01",
            "bbox": bbox,
            "source": str(sheet.get("inspection_region_source") or "sheet_bbox"),
            "confidence": safe_float(sheet.get("inspection_region_confidence")) or 0.0,
            "evidence": str(sheet.get("inspection_region_evidence") or ""),
        }
    ]


def intersection_area(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    """计算两个 bbox 的交集面积。"""
    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def assign_region(
        bbox: tuple[float, float, float, float],
        regions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """根据对象 bbox 中心点和交叠比例，将对象分配到最合适的 inspection region。"""
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    containing = [
        region for region in regions
        if region["_bbox"][0] <= cx <= region["_bbox"][2]
           and region["_bbox"][1] <= cy <= region["_bbox"][3]
    ]
    if containing:
        return min(containing, key=lambda item: item["_area"])
    object_area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 1.0)
    ranked = [(intersection_area(bbox, region["_bbox"]) / object_area, region) for region in regions]
    if not ranked:
        return None
    ratio, region = max(ranked, key=lambda item: item[0])
    return region if ratio >= 0.5 else None


def meaningful(value: Any) -> str:
    """过滤空值和通用 CAD 噪声词，返回有业务意义的文本。"""
    text = str(value or "").strip()
    return "" if normalize_value(text) in NORMALIZED_GENERIC_TERMS else text


def contains_any_marker(value: Any, markers: tuple[str, ...] | set[str]) -> bool:
    """判断文本是否包含任一标记词，兼容原文、大小写和归一化文本。"""
    text = str(value or "")
    if not text:
        return False
    upper_text = text.upper()
    compact_text = normalize_value(text)
    for marker in markers:
        raw_marker = str(marker or "")
        if not raw_marker:
            continue
        if raw_marker in text or raw_marker.upper() in upper_text:
            return True
        marker_compact = normalize_value(raw_marker)
        if marker_compact and marker_compact in compact_text:
            return True
    return False


def has_mep_object_semantic(term: Any, context: Any = "") -> bool:
    """判断候选是否像水、电、气、暖通、通风、给排水相关房间或设施。"""
    term_text = str(term or "")
    context_text = f"{term_text} {context or ''}"
    return contains_any_marker(context_text, MEP_OBJECT_MARKERS) and contains_any_marker(term_text, OBJECT_NOUN_MARKERS)


GENERIC_STANDALONE_OBJECT_TERMS = {
    normalize_value("机房"),
    normalize_value("房间"),
    normalize_value("房"),
    normalize_value("室"),
    normalize_value("间"),
    normalize_value("厅"),
}


def is_generic_standalone_object_term(term: Any) -> bool:
    """过滤“机房”这类脱离前缀后过于宽泛的房间泛词。"""
    return normalize_value(term) in GENERIC_STANDALONE_OBJECT_TERMS


def is_fm_exit_code(term: Any) -> bool:
    """FM甲/乙/数字通常是防火门编号，巡检语义上按安全出口处理。"""
    compact = normalize_value(term).upper()
    return bool(re.fullmatch(r"FM[甲乙丙丁]?\d{1,8}(?:[-_]\d+)?", compact))


def is_code_like_display_term(term: Any) -> bool:
    """判断是否为设备/门窗编号；编号不适合作为最终标注名。"""
    compact = normalize_value(term).upper()
    if not compact:
        return True
    if is_fm_exit_code(compact):
        return True
    return bool(
        re.fullmatch(r"[A-Z]{1,8}[甲乙丙丁]?\d{1,8}(?:[-_]\d+)?", compact)
        or re.fullmatch(r"\d+(?:[A-Z]+)?", compact)
    )


def preferred_display_class_name(item: dict[str, Any]) -> str:
    """最终展示优先保留 CAD 里的具体对象名，避免油烟井被泛化成风机房。"""
    class_name = str(item.get("class_name") or item.get("term") or "疑似巡检对象").strip()
    term = str(item.get("term") or "").strip()
    if (
            term
            and re.search(r"[\u4e00-\u9fff]", term)
            and not is_generic_standalone_object_term(term)
            and not is_code_like_display_term(term)
    ):
        return term
    return class_name


def canonical_class_name(value: Any) -> str:
    """将输入对象名映射为规则库中的标准巡检对象名称；无法匹配时返回原文本。"""
    text = str(value or "").strip()
    if not text:
        return ""
    matched = match_inspection_object([text])
    return str(matched.get("canonical") or text).strip() if matched else text


def has_text_annotation_evidence(candidate: dict[str, Any]) -> bool:
    """判断候选是否来自 TEXT / MTEXT / ATTRIB 等文本注记证据。"""
    entity_type = str(candidate.get("entity_type", "") or "").upper()
    source_type = str(candidate.get("source_type", "") or "").lower()
    return source_type == "text" and any(item in entity_type for item in ("TEXT", "MTEXT", "ATTRIB"))


def has_block_evidence(candidate: dict[str, Any]) -> bool:
    """判断候选是否来自 INSERT 块参照证据。"""
    entity_type = str(candidate.get("entity_type", "") or "").upper()
    source_type = str(candidate.get("source_type", "") or "").lower()
    return source_type == "block" and "INSERT" in entity_type


def evidence_allows_class(class_name: Any, candidate: dict[str, Any]) -> bool:
    """根据对象类别要求判断当前候选的证据类型是否可信。"""
    class_key = normalize_value(canonical_class_name(class_name))
    if class_key in TEXT_EVIDENCE_ONLY_CLASS_KEYS:
        return has_text_annotation_evidence(candidate)
    if class_key in TEXT_OR_BLOCK_CLASS_KEYS:
        return has_text_annotation_evidence(candidate) or has_block_evidence(candidate)
    return has_text_annotation_evidence(candidate) or has_block_evidence(candidate)


def is_protected_inspection_term(term: str) -> bool:
    """判断短词或编号是否命中巡检对象规则，命中则不能按普通噪声过滤。"""
    if match_inspection_object([term]):
        return True
    return bool(match_inspection_keyword_pattern([term]))


def short_word_decision(term: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """识别“强电、弱电、电井、泵、阀、电梯”等短标签，并结合上下文映射为巡检对象。"""
    compact = normalize_value(term)
    if not compact:
        return None
    context_text = " ".join(
        str(row.get(field, "") or "")
        for field in ("term", "layer", "parent_block_name", "entity_type", "geometry_kind")
    )
    context = normalize_value(f"{term} {context_text}")

    def build(class_name: str, reason: str, confidence: float = 0.78) -> dict[str, Any] | None:
        if not evidence_allows_class(class_name, row):
            return None
        return {
            "role": "inspection_object",
            "class_name": class_name,
            "confidence": confidence,
            "reason": reason,
            "stage": "short_word_rule",
        }

    exact_text_room_rules = {
        normalize_value("强电"): "强电间",
        normalize_value("弱电"): "弱电间",
        normalize_value("强弱电"): "强弱电间",
        normalize_value("电井"): "电井",
        normalize_value("消控"): "消防控制室/消控室",
        normalize_value("消防控制"): "消防控制室/消控室",
        normalize_value("消防泵"): "消防水泵",
        normalize_value("水泵"): "消防水泵",
    }
    if compact in exact_text_room_rules:
        return build(exact_text_room_rules[compact], "short_exact_text_label", 0.86)

    if "楼梯" in term or "扶梯" in term or "疏散楼梯" in term:
        return None
    if ("消防电梯" in term) or ("电梯" in term and contains_any_marker(context, ("消防", "ELEV", "FIRE"))):
        return build("消防电梯", "short_elevator_label", 0.82)

    room_markers = ("房", "室", "间", "所")
    if contains_any_marker(term, room_markers):
        if contains_any_marker(context, ("变配电", "高压配电", "低压配电")):
            return build("变配电房", "short_room_context_power", 0.82)
        if contains_any_marker(context, ("配电", "强电", "弱电")):
            return build("配电房", "short_room_context_power", 0.80)
        if contains_any_marker(context, ("消控", "消防控制", "报警主机", "控制室")):
            return build("消防控制室/消控室", "short_room_context_control", 0.82)
        if contains_any_marker(context, ("消防水泵", "消防泵", "水泵", "泵房", "PUMP")):
            return build("消防水泵房/消防泵房", "short_room_context_pump", 0.82)
        if contains_any_marker(context, ("风机", "送风", "排风", "暖通", "FAN")):
            return build("风机房", "short_room_context_fan", 0.78)
        if contains_any_marker(context, ("发电机", "柴油", "GENERATOR")):
            return build("备用发电机/柴油发电机房", "short_room_context_generator", 0.80)

    if "电井" in term or (compact == normalize_value("井") and contains_any_marker(context, ("强电", "弱电", "电气", "配电"))):
        return build("电井", "short_well_context", 0.80)
    if ("泵" in term or compact == normalize_value("泵")) and contains_any_marker(context,
                                                                                ("消防", "消火", "喷淋", "水泵", "给水", "PUMP")):
        class_name = "消防水泵房/消防泵房" if contains_any_marker(context, room_markers) else "消防水泵"
        return build(class_name, "short_pump_context", 0.78)
    if "阀" in term and contains_any_marker(context, ("消防", "报警", "喷淋", "水", "VALVE")):
        return build("报警阀组", "short_valve_context", 0.76)
    if "梯" in term and contains_any_marker(context, ("消防", "电梯", "ELEV", "LIFT")):
        return build("消防电梯", "short_lift_context", 0.76)
    return None


def candidate_term(row: dict[str, str]) -> tuple[str, str]:
    """从 inventory 行中提取候选语义词：文本实体取 norm_text，INSERT 取 parent_block_name。"""
    entity_type = str(row.get("entity_type", "") or "").upper()
    norm_text = meaningful(row.get("norm_text"))
    if norm_text and entity_type in {"TEXT", "MTEXT", "ATTRIB"}:
        return norm_text, "text"
    block = meaningful(row.get("parent_block_name"))
    if block and entity_type == "INSERT":
        return block, "block"
    # A layer name is useful evidence, but is never an inspection object by
    # itself. This prevents ordinary LINE/HATCH layers from entering the LLM.
    return "", "none"


def is_local_noise(term: str, source_type: str, row: dict[str, str]) -> tuple[bool, str]:
    """在进入本地规则和 LLM 前过滤图纸编号、尺寸、楼层码、标题栏、说明文字等非对象噪声。"""
    compact = normalize_value(term)
    if not compact:
        return True, "empty_term"
    if is_generic_standalone_object_term(term):
        return True, "generic_standalone_object_term"
    if is_non_object_explanatory_text(term):
        return True, "explanatory_text"
    if len(compact) == 1 and not re.search(r"[井泵阀梯]", compact):
        return True, "single_character"
    if source_type == "layer" and NOISE_PATTERN.fullmatch(term.strip()):
        return True, "layer_code_only"
    if source_type == "text" and NOISE_PATTERN.fullmatch(term.strip()) and not is_protected_inspection_term(term):
        return True, "dimension_or_number"
    if source_type == "text" and MEASUREMENT_OR_FLOOR_RANGE_PATTERN.search(term) and not is_protected_inspection_term(
            term):
        return True, "measurement_or_floor_range"
    if source_type in {"text", "block"} and FLOOR_CODE_ONLY_PATTERN.fullmatch(
            term.strip()) and not is_protected_inspection_term(term):
        return True, "floor_code_only"
    if source_type == "text":
        parent_block = str(row.get("parent_block_name", "") or "")
        evidence_text = f"{term} {parent_block}"
        has_inspection_hint = (
                any(hint in evidence_text for hint in INSPECTION_HINTS)
                or contains_any_marker(evidence_text, INSPECTION_SEMANTIC_HINTS)
        )
        mep_like = has_mep_object_semantic(term, evidence_text)
        if contains_any_marker(parent_block, TITLE_BLOCK_PARENT_MARKERS):
            return True, "title_block_text"
        if (
                contains_any_marker(parent_block, EXPLICIT_DRAWING_NOISE_TERMS)
                and not has_inspection_hint
                and not mep_like
        ):
            return True, "drawing_block_noise"
        if contains_any_marker(term, DISCIPLINE_TITLE_TERMS) and not is_protected_inspection_term(term) and not mep_like:
            return True, "discipline_title_text"
        if not has_inspection_hint and contains_any_marker(term, EXPLICIT_DRAWING_NOISE_TERMS):
            return True, "drawing_note_or_title"
        if not has_inspection_hint and any(marker in term for marker in HARD_EXCLUSION_TERMS):
            return True, "drawing_note_or_title"
        if not has_inspection_hint and len(compact) > 48:
            return True, "long_explanatory_text"
        ascii_code = re.fullmatch(r"([A-Z]{1,8})[-_]?\d{1,6}(?:\([^)]*\))?", compact, flags=re.I)
        if ascii_code and ascii_code.group(
                1).upper() not in PROTECTED_CODE_PREFIXES and not is_protected_inspection_term(term):
            return False, ""
    if source_type == "block":
        block_text = f"{term} {row.get('parent_block_name', '')}"
        if contains_any_marker(block_text, TITLE_BLOCK_PARENT_MARKERS):
            return True, "title_block_insert"
        if contains_any_marker(block_text, EXPLICIT_DRAWING_NOISE_TERMS) and not is_protected_inspection_term(term):
            return True, "drawing_symbol_block"
    if str(row.get("entity_type", "")).upper() in {"DIMENSION", "LEADER", "MLEADER"}:
        return True, "annotation_entity"
    return False, ""


def rule_decision(term: str, row: dict[str, str]) -> dict[str, Any] | None:
    """使用本地巡检对象别名库、关键词模式和短词规则，对候选词进行确定性识别。"""
    context_values = [term, *(row.get(field, "") for field in CONTEXT_FIELDS)]
    if is_fm_exit_code(term):
        return {
            "role": "inspection_object",
            "class_name": "安全出口",
            "confidence": 0.9,
            "reason": "fm_fire_door_code_as_exit",
            "stage": "code_rule",
        }
    if is_generic_standalone_object_term(term):
        return None
    matched = match_inspection_object([term], context_values=context_values)
    if matched:
        class_name = str(matched.get("canonical") or term)
        if not evidence_allows_class(class_name, row):
            return None
        return {
            "role": "inspection_object",
            "class_name": class_name,
            "confidence": float(matched.get("confidence") or 1.0),
            "reason": str(matched.get("reason") or "inspection_alias_rule"),
            "stage": "alias_rule",
        }
    pattern = match_inspection_keyword_pattern([term], context_values=context_values)
    if pattern:
        class_name = str(pattern.get("canonical") or term)
        if not evidence_allows_class(class_name, row):
            return None
        return {
            "role": "inspection_object",
            "class_name": class_name,
            "confidence": float(pattern.get("confidence") or 0.88),
            "reason": str(pattern.get("reason") or "inspection_keyword_pattern"),
            "stage": "keyword_rule",
        }
    short_match = short_word_decision(term, row)
    if short_match:
        return short_match
    return None


def preview_shape(
        row: dict[str, str],
        bbox: tuple[float, float, float, float],
        region_bbox: tuple[float, float, float, float],
) -> list[Any] | None:
    """将区域内非文本几何对象压缩为 0-1 归一化预览形状，用于前端或人工审查展示。"""
    entity_type = str(row.get("entity_type", "") or "").upper()
    geometry = str(row.get("geometry_kind", "") or "").lower()
    if entity_type in {"TEXT", "MTEXT", "ATTRIB", "DIMENSION", "HATCH"}:
        return None
    width = max(region_bbox[2] - region_bbox[0], 1.0)
    height = max(region_bbox[3] - region_bbox[1], 1.0)
    if (bbox[2] - bbox[0]) / width > 0.65 or (bbox[3] - bbox[1]) / height > 0.65:
        return None
    coords = [
        max(0.0, min(1.0, (bbox[0] - region_bbox[0]) / width)),
        max(0.0, min(1.0, (bbox[1] - region_bbox[1]) / height)),
        max(0.0, min(1.0, (bbox[2] - region_bbox[0]) / width)),
        max(0.0, min(1.0, (bbox[3] - region_bbox[1]) / height)),
    ]
    if entity_type in {"CIRCLE", "ARC", "ELLIPSE"}:
        kind = "curve"
    elif entity_type == "INSERT" or geometry == "block_insert":
        kind = "block"
    elif str(row.get("is_closed", "")).lower() in {"1", "true"}:
        kind = "closed"
    else:
        kind = "line"
    return [kind, *(round(value, 5) for value in coords)]


def load_regions(sheets_path: Path) -> list[dict[str, Any]]:
    """读取图幅楼层识别结果 JSON，筛选可用于路径规划的楼层区域，并构造内部 region 结构。"""
    payload = read_json(sheets_path)
    regions: list[dict[str, Any]] = []
    for index, sheet in enumerate(payload.get("sheets", []), start=1):
        confidence = safe_float(sheet.get("floor_confidence")) or 0.0
        usable = sheet.get("path_planning_usable")
        if usable is None:
            usable = sheet.get("sheet_role") in {"floor_plan_candidate", "path_planning_floor_plan"}
        region_entries = sheet_region_entries(sheet)
        if not usable or confidence <= 0.0 or not region_entries:
            continue
        sheet_id = str(sheet.get("sheet_id") or f"SHEET_{index:03d}")
        for region_entry in region_entries:
            bbox = region_entry["bbox"]
            child_region_id = region_entry["region_id"]
            child_sheet_id = sheet_id if len(region_entries) == 1 else f"{sheet_id}_{child_region_id}"
            display_name = str(sheet.get("floor_name") or sheet.get("floor_id") or sheet_id)
            if len(region_entries) > 1:
                display_name = f"{display_name} · {child_region_id}"
            regions.append(
                {
                    "sheet_id": child_sheet_id,
                    "parent_sheet_id": sheet_id,
                    "inspection_region_id": child_region_id,
                    "floor_id": str(sheet.get("floor_id") or "UNKNOWN"),
                    "floor_name": str(sheet.get("floor_name") or sheet.get("floor_id") or "未知楼层"),
                    "display_name": display_name,
                    "sheet_title": str(sheet.get("sheet_title") or ""),
                    "confidence": confidence,
                    "method": str(sheet.get("method") or ""),
                    "evidence": str(sheet.get("evidence") or ""),
                    "region_source": region_entry["source"],
                    "region_confidence": region_entry["confidence"] or confidence,
                    "region_evidence": region_entry["evidence"],
                    "bbox": {"minx": bbox[0], "miny": bbox[1], "maxx": bbox[2], "maxy": bbox[3]},
                    "_bbox": bbox,
                    "_area": (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]),
                    "object_count": 0,
                    "type_keys": set(),
                    "candidate_groups": {},
                    "preview_cells": defaultdict(list),
                }
            )
    duplicate_ids = Counter(region["floor_id"] for region in regions)
    for region in regions:
        if duplicate_ids[region["floor_id"]] > 1:
            suffix = region.get("inspection_region_id") or region["sheet_id"]
            region[
                "display_name"] = f"{region['floor_name']} · {region['sheet_title'] or region['parent_sheet_id']} · {suffix}"
    return regions


def call_llm(llm_script: Path, input_path: Path, output_path: Path, *, no_llm: bool) -> dict[str, Any]:
    """调用外部 LLM 分类脚本；LLM 调用失败时自动降级为 --no-llm 本地兜底模式。"""
    command = [sys.executable, str(llm_script), "--input", str(input_path), "--output", str(output_path),
               "--save-debug"]
    if no_llm:
        command.append("--no-llm")
    proc = subprocess.run(command, cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0 and not no_llm:
        fallback = subprocess.run([*command, "--no-llm"], cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        if fallback.returncode != 0:
            raise RuntimeError(
                (proc.stdout or "") + (proc.stderr or "") + (fallback.stdout or "") + (fallback.stderr or ""))
        result = read_json(output_path)
        result["pipeline_warning"] = "LLM unavailable; local-rule fallback was used."
        return result
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout or "") + (proc.stderr or ""))
    return read_json(output_path)


def llm_decision_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """将 LLM 输出转换为按归一化 term 索引的决策字典。"""
    decisions: dict[str, dict[str, Any]] = {}
    for item in payload.get("inspection_decisions", []) or []:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if term:
            decisions[normalize_value(term)] = item
    for term in payload.get("inspection_object", []) or []:
        key = normalize_value(term)
        decisions.setdefault(key, {"term": term, "role": "inspection_object", "class_name": term, "confidence": 0.8})
    return decisions


def aggregate_rows(decisions: list[dict[str, Any]], sheet_id: str) -> list[dict[str, Any]]:
    """按巡检对象标准名称聚合识别结果，生成区域级或全局 catalog_rows。"""
    groups: dict[str, dict[str, Any]] = {}
    for item in decisions:
        if item.get("role") != "inspection_object":
            continue
        name = str(item.get("display_class_name") or preferred_display_class_name(item)).strip()
        group = groups.setdefault(
            name,
            {
                "semantic_name": name,
                "standard_class_names": Counter(),
                "count": 0,
                "layers": Counter(),
                "blocks": Counter(),
                "entities": Counter(),
                "geometries": Counter(),
                "stages": Counter(),
                "confidence": 0.0,
            },
        )
        count = int(item.get("count") or 1)
        group["count"] += count
        standard_name = str(item.get("class_name") or "").strip()
        if standard_name:
            group["standard_class_names"][standard_name] += count
        for key, counter in (("layer", "layers"), ("parent_block_name", "blocks"), ("entity_type", "entities"),
                             ("geometry_kind", "geometries"), ("stage", "stages")):
            value = str(item.get(key) or "").strip()
            if value:
                group[counter][value] += count
        group["confidence"] = max(group["confidence"], float(item.get("confidence") or 0.0))

    rows: list[dict[str, Any]] = []
    for index, group in enumerate(sorted(groups.values(), key=lambda value: (-value["count"], value["semantic_name"])),
                                  start=1):
        top = lambda counter, n=3: " / ".join(key for key, _ in counter.most_common(n))
        rows.append(
            {
                "signature_id": f"{sheet_id}:INS_{index:04d}",
                "sheet_id": sheet_id,
                "semantic_name": group["semantic_name"],
                "display_name": f"{group['semantic_name']}({group['count']})",
                "standard_class_name": top(group["standard_class_names"], 2),
                "count": group["count"],
                "layer": top(group["layers"], 4),
                "parent_block_name": top(group["blocks"], 3),
                "entity_type": top(group["entities"], 3),
                "geometry_kind": top(group["geometries"], 3),
                "role": "inspection_object",
                "proposed_role": "inspection_object",
                "confidence": round(group["confidence"], 3),
                "reason": top(group["stages"], 2),
            }
        )
    return rows


def run_pipeline(
        inventory_dir: Path,
        sheets_json: Path,
        output_dir: Path,
        llm_script: Path,
        *,
        no_llm: bool = False,
) -> dict[str, Any]:
    """执行区域清单构建主流程：切分语义 inventory、生成候选、规则识别、LLM 兜底、写出结果。"""
    # 1. 准备语义 inventory，并读取可用于路径规划的楼层 / 区域范围。
    inventory_path, semantic_inventory_derived = ensure_semantic_inventory(inventory_dir)
    regions = load_regions(sheets_json)
    if not regions:
        raise RuntimeError("No usable floor regions were detected.")

    output_dir.mkdir(parents=True, exist_ok=True)
    handles: dict[str, Any] = {}
    writers: dict[str, csv.DictWriter] = {}
    try:
        # 2. 扫描语义 inventory，将每个对象按 bbox 分配到对应 inspection_region。
        with inventory_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            fieldnames = list(reader.fieldnames or []) + ["sheet_id", "floor_id", "floor_name"]
            for region in regions:
                region_dir = output_dir / region["sheet_id"]
                region_dir.mkdir(parents=True, exist_ok=True)
                handle = (region_dir / SEMANTIC_INVENTORY_FILE).open("w", encoding="utf-8-sig", newline="")
                handles[region["sheet_id"]] = handle
                writers[region["sheet_id"]] = csv.DictWriter(handle, fieldnames=fieldnames)
                writers[region["sheet_id"]].writeheader()

            for row in reader:
                bbox = row_bbox(row)
                if not bbox:
                    continue
                region = assign_region(bbox, regions)
                if not region:
                    continue
                region["object_count"] += 1
                signature = tuple(row.get(key, "") for key in (
                "layer", "entity_type", "geometry_kind", "color", "linetype", "is_closed", "parent_block_name",
                "norm_text"))
                region["type_keys"].add(signature)
                writers[region["sheet_id"]].writerow(
                    {**row, "sheet_id": region["sheet_id"], "floor_id": region["floor_id"],
                     "floor_name": region["floor_name"]})

                # 3. 从文本实体或 INSERT 块中提取候选词，并先做本地噪声过滤。
                term, source_type = candidate_term(row)
                if term:
                    key = (normalize_value(term), row.get("layer", ""), row.get("parent_block_name", ""),
                           row.get("entity_type", ""), row.get("geometry_kind", ""))
                    group = region["candidate_groups"].setdefault(
                        key,
                        {
                            "term": term,
                            "source_type": source_type,
                            "layer": row.get("layer", ""),
                            "parent_block_name": row.get("parent_block_name", ""),
                            "entity_type": row.get("entity_type", ""),
                            "geometry_kind": row.get("geometry_kind", ""),
                            "count": 0,
                            "sample_object_ids": [],
                        },
                    )
                    if source_type == "layer":
                        group["count"] = 1
                    else:
                        group["count"] += 1
                    if len(group["sample_object_ids"]) < 8:
                        group["sample_object_ids"].append(row.get("object_id", ""))

                shape = preview_shape(row, bbox, region["_bbox"])
                if shape:
                    cell = (
                    min(59, int(((shape[1] + shape[3]) / 2) * 60)), min(35, int(((shape[2] + shape[4]) / 2) * 36)))
                    if len(region["preview_cells"][cell]) < 3:
                        region["preview_cells"][cell].append(shape)
    finally:
        for handle in handles.values():
            handle.close()

    # 4. 对候选词优先使用本地规则识别；无法确定的候选再准备给 LLM。
    uncertain_by_term: dict[str, dict[str, Any]] = {}
    region_decisions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    local_counts = Counter()
    candidate_group_count = 0
    uncertain_candidate_group_count = 0
    rule_cache: dict[tuple[str, ...], dict[str, Any] | None] = {}
    for region in regions:
        candidates = list(region["candidate_groups"].values())
        candidate_group_count += len(candidates)
        for candidate in candidates:
            rule_key = tuple(
                normalize_value(candidate.get(field, ""))
                for field in ("term", *CONTEXT_FIELDS)
            )
            if rule_key not in rule_cache:
                rule_cache[rule_key] = rule_decision(candidate["term"], candidate)
            direct = rule_cache[rule_key]
            if direct:
                candidate["_direct_rule"] = True
                decision = {**candidate, **direct}
                decision["display_class_name"] = preferred_display_class_name(decision)
                region_decisions[region["sheet_id"]].append(decision)
                local_counts[direct["stage"]] += 1
                continue
            noise, noise_reason = is_local_noise(candidate["term"], candidate["source_type"], candidate)
            if noise:
                local_counts[f"noise_after_rules:{noise_reason}"] += 1
                continue
            uncertain_candidate_group_count += 1
            key = normalize_value(candidate["term"])
            merged = uncertain_by_term.setdefault(
                key,
                {
                    "term": candidate["term"],
                    "source_type": candidate["source_type"],
                    "source_types": [],
                    "layer": [],
                    "parent_block_name": [],
                    "entity_type": [],
                    "geometry_kind": [],
                    "context": [],
                },
            )
            if candidate.get("source_type") and candidate["source_type"] not in merged["source_types"]:
                merged["source_types"].append(candidate["source_type"])
            for field in CONTEXT_FIELDS:
                value = candidate.get(field, "")
                if value and value not in merged[field] and len(merged[field]) < 8:
                    merged[field].append(value)
            merged["context"].append(
                {"sheet_id": region["sheet_id"], "floor_id": region["floor_id"], "count": candidate["count"]})

    # 5. 只把文本来源的不确定候选发送给 LLM，避免把普通图层 / 块噪声放大。
    llm_items = []
    for item in uncertain_by_term.values():
        if "text" not in item.get("source_types", []):
            continue
        llm_items.append(
            {
                "term": item["term"],
                "source_type": " / ".join(item.get("source_types", []) or [item["source_type"]]),
                "layer": " / ".join(item["layer"]),
                "parent_block_name": " / ".join(item["parent_block_name"]),
                "entity_type": " / ".join(item["entity_type"]),
                "geometry_kind": " / ".join(item["geometry_kind"]),
                "reason": "uncertain_text_alias_after_local_rules",
            }
        )
    llm_input_path = output_dir / LLM_INPUT_FILE
    llm_output_path = output_dir / LLM_OUTPUT_FILE
    write_json(llm_input_path, {"uncertain_inspection_candidates": llm_items})
    if llm_items and no_llm:
        llm_payload = {
            "inspection_object": [],
            "IGNORE": [item["term"] for item in llm_items],
            "inspection_decisions": [],
            "model": "",
            "counts": {"inspection_object": 0, "IGNORE": len(llm_items)},
            "pipeline_warning": "LLM disabled; uncertain candidates were kept as IGNORE.",
        }
        write_json(llm_output_path, llm_payload)
    elif llm_items:
        llm_payload = call_llm(llm_script, llm_input_path, llm_output_path, no_llm=no_llm)
    else:
        llm_payload = {"inspection_object": [], "IGNORE": [], "inspection_decisions": [], "model": "", "counts": {}}
        write_json(llm_output_path, llm_payload)
    llm_map = llm_decision_map(llm_payload)

    # 6. 将 LLM 判定回填到各区域候选中，并再次校验证据类型是否可信。
    for region in regions:
        for candidate in region["candidate_groups"].values():
            if candidate.get("_direct_rule"):
                continue
            llm_item = llm_map.get(normalize_value(candidate["term"]))
            if not llm_item:
                continue
            role = str(llm_item.get("role") or "inspection_object")
            if role != "inspection_object":
                continue
            class_name = str(llm_item.get("class_name") or candidate["term"])
            if not evidence_allows_class(class_name, candidate):
                continue
            region_decisions[region["sheet_id"]].append(
                {
                    **candidate,
                    "role": "inspection_object",
                    "class_name": class_name,
                    "display_class_name": preferred_display_class_name({**candidate, "class_name": class_name}),
                    "confidence": float(llm_item.get("confidence") or 0.0),
                    "reason": str(llm_item.get("reason") or "llm_fallback"),
                    "stage": "llm_fallback",
                }
            )

    # 7. 汇总每个区域的巡检对象 catalog、预览几何和输出文件路径。
    floors: list[dict[str, Any]] = []
    combined_decisions: list[dict[str, Any]] = []
    for region in regions:
        decisions = region_decisions[region["sheet_id"]]
        combined_decisions.extend(decisions)
        catalog_rows = aggregate_rows(decisions, region["sheet_id"])
        preview_shapes = [shape for cell in sorted(region["preview_cells"]) for shape in region["preview_cells"][cell]][
                         :5000]
        region_payload = {
            key: value for key, value in region.items()
            if key not in {"_bbox", "_area", "type_keys", "candidate_groups", "preview_cells"}
        }
        region_payload.update(
            {
                "type_count": len(region["type_keys"]),
                "candidate_count": len(region["candidate_groups"]),
                "inspection_type_count": len(catalog_rows),
                "inspection_instance_count": sum(int(row["count"]) for row in catalog_rows),
                "catalog_rows": catalog_rows,
                "preview_shapes": preview_shapes,
                "inventory_csv": str((output_dir / region["sheet_id"] / SEMANTIC_INVENTORY_FILE).resolve()),
            }
        )
        floors.append(region_payload)
        write_json(output_dir / region["sheet_id"] / "inspection_objects.json",
                   {"catalog_rows": catalog_rows, "decisions": decisions})

    global_rows = aggregate_rows(combined_decisions, "ALL")
    # 8. 生成全局结果 JSON，包括区域结果、全局对象目录和中间产物路径。
    result = {
        "schema_version": SCHEMA_VERSION,
        "pipeline": [
            "full_cad_inventory",
            "sheet_floor_region_preprocess",
            "region_inventory",
            "region_candidate_generation",
            "deterministic_rules",
            "llm_binary_fallback",
        ],
        "inventory_dir": str(inventory_dir.resolve()),
        "sheets_json": str(sheets_json.resolve()),
        "region_count": len(floors),
        "local_rule_counts": dict(local_counts),
        "candidate_group_count": candidate_group_count,
        "llm_candidate_count": len(llm_items),
        "candidate_deduplicated_count": max(0, uncertain_candidate_group_count - len(llm_items)),
        "llm_model": llm_payload.get("model", ""),
        "llm_warning": llm_payload.get("pipeline_warning", ""),
        "semantic_inventory": {
            "path": str(inventory_path.resolve()),
            "derived_from_legacy_cache": semantic_inventory_derived,
            "entity_types": sorted(SEMANTIC_ENTITY_TYPES),
        },
        "catalog_rows": global_rows,
        "floors": floors,
        "artifacts": {
            "llm_input": str(llm_input_path.resolve()),
            "llm_output": str(llm_output_path.resolve()),
            "result_json": str((output_dir / RESULT_FILE).resolve()),
        },
    }
    write_json(output_dir / RESULT_FILE, result)
    write_json(output_dir / "regions_manifest.json", {"schema_version": SCHEMA_VERSION, "regions": [
        {key: value for key, value in floor.items() if key not in {"catalog_rows", "preview_shapes"}} for floor in
        floors]})
    return result


def main() -> None:
    """命令行入口：解析参数并调用 run_pipeline。"""
    parser = argparse.ArgumentParser(description="Build per-floor CAD inventories and identify inspection objects.")
    parser.add_argument("--inventory-dir", required=True)
    parser.add_argument("--sheets-json", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--llm-script", default=str(PROJECT_ROOT / "scripts" / "llm-deepseekv4.py"))
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    result = run_pipeline(
        Path(args.inventory_dir).resolve(),
        Path(args.sheets_json).resolve(),
        Path(args.output_dir).resolve(),
        Path(args.llm_script).resolve(),
        no_llm=args.no_llm,
    )
    print(json.dumps({"result": result["artifacts"]["result_json"], "region_count": result["region_count"],
                      "llm_candidate_count": result["llm_candidate_count"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
