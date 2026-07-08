# -*- coding: utf-8 -*-
"""
Inspection object alias library.

This module is intentionally deterministic. It answers only two questions:
1. Does a CAD term match a confirmed inspection object canonical name?
2. Does a CAD term match a confirmed inspection object alias or abbreviation?

Only ``canonical``, ``aliases``, and ``abbreviations`` participate in the
deterministic rule library. Any hit is treated as an inspection object.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIBRARY_PATH = PROJECT_ROOT / "configs" / "inspection_object_aliases.json"
DEFAULT_KEYWORD_PATTERN_PATH = PROJECT_ROOT / "configs" / "inspection_object_keyword_patterns.json"

EXPLANATORY_NON_OBJECT_MARKERS = [
    "\u8be6\u56fe",  # 详图
    "\u8be6\u5efa\u65bd",  # 详建施
    "\u505a\u6cd5",  # 做法
    "\u8bf4\u660e",  # 说明
    "\u56fe\u4f8b",  # 图例
    "\u4e13\u9879\u8bbe\u8ba1",  # 专项设计
    "\u53c2\u7167",  # 参照
    "\u8981\u6c42\u8bbe\u7f6e",  # 要求设置
    "\u8ba1\u7b97",  # 计算
    "\u6807\u6ce8",  # 标注
    "\u5bbd\u5ea6",  # 宽度
    "\u758f\u6563\u5bbd\u5ea6",  # 疏散宽度
    "\u5c3a\u5bf8",  # 尺寸
    "\u7f16\u53f7",  # 编号
    "\u6807\u9ad8",  # 标高
    "\u8f74\u53f7",  # 轴号
]

SHORT_ROOM_NAME_RULES = {
    "\u5f3a\u7535": "\u5f3a\u7535\u95f4",  # 强电 -> 强电间
    "\u5f31\u7535": "\u5f31\u7535\u95f4",  # 弱电 -> 弱电间
}

SHORT_ROOM_CONTEXT_HINTS = [
    "TEXT",
    "MTEXT",
    "ATTRIB",
    "text",
    "\u623f\u95f4",  # 房间
    "\u623f\u540d",  # 房名
    "\u95e8",  # 门
    "\u5730\u4e0b\u4e00\u5c42",  # 地下一层
    "\u5730\u4e0b\u4e8c\u5c42",  # 地下二层
    "\u5730\u5e93",  # 地库
]


def normalize_value(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("　", " ")
    text = re.sub(r"\s+", "", text)
    return text.upper()


def compact_ascii(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def split_ascii_tokens(value: Any) -> set[str]:
    return {part for part in re.split(r"[^A-Z0-9]+", str(value or "").upper()) if part}


def has_chinese(value: Any) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(value or "")))


def is_non_object_explanatory_text(value: Any) -> bool:
    """Return True for notes/details that mention an object but are not objects."""
    text = normalize_value(value)
    if not text:
        return False
    return any(normalize_value(marker) in text for marker in EXPLANATORY_NON_OBJECT_MARKERS)


def load_library(path: Path | None = None) -> dict[str, Any]:
    library_path = path or DEFAULT_LIBRARY_PATH
    with library_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_keyword_patterns(path: Path | None = None) -> dict[str, Any]:
    pattern_path = path or DEFAULT_KEYWORD_PATTERN_PATH
    if not pattern_path.exists():
        return {"rules": []}
    with pattern_path.open("r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=4)
def cached_library(path_text: str = "") -> dict[str, Any]:
    return load_library(Path(path_text) if path_text else DEFAULT_LIBRARY_PATH)


@lru_cache(maxsize=4)
def cached_keyword_patterns(path_text: str = "") -> dict[str, Any]:
    return load_keyword_patterns(Path(path_text) if path_text else DEFAULT_KEYWORD_PATTERN_PATH)


def alias_matches_value(alias: str, value: Any, *, ambiguous: bool = False) -> bool:
    alias_norm = normalize_value(alias)
    value_norm = normalize_value(value)
    if not alias_norm or not value_norm:
        return False
    if is_non_object_explanatory_text(value):
        return False

    if has_chinese(alias_norm):
        if len(alias_norm) <= 1:
            return alias_norm == value_norm
        return alias_norm in value_norm

    alias_compact = compact_ascii(alias_norm)
    value_compact = compact_ascii(value_norm)
    value_tokens = split_ascii_tokens(value_norm)
    if not alias_compact or not value_compact:
        return False

    if alias_compact in value_tokens or alias_compact == value_compact:
        return True

    # Confirmed abbreviations such as FHJL should match FHJL-01 / FHJL_1.
    if len(alias_compact) <= 5:
        return (
            value_compact.startswith(alias_compact)
            or value_compact.endswith(alias_compact)
            or bool(re.fullmatch(rf"{re.escape(alias_compact)}\d+", value_compact))
        )

    if ambiguous:
        return False

    return alias_compact in value_compact


def any_alias_matches(aliases: Iterable[str], values: Iterable[Any], *, ambiguous: bool = False) -> str:
    for alias in aliases:
        for value in values:
            if alias_matches_value(alias, value, ambiguous=ambiguous):
                return str(alias)
    return ""


def matching_aliases(aliases: Iterable[str], values: Iterable[Any], *, ambiguous: bool = False) -> list[str]:
    matches: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        for value in values:
            if alias_matches_value(alias, value, ambiguous=ambiguous):
                clean = str(alias)
                key = normalize_value(clean)
                if key not in seen:
                    seen.add(key)
                    matches.append(clean)
                break
    return matches


def keyword_matches_value(keyword: Any, value: Any) -> bool:
    keyword_norm = normalize_value(keyword)
    value_norm = normalize_value(value)
    if not keyword_norm or not value_norm:
        return False
    if is_non_object_explanatory_text(value):
        return False

    if has_chinese(keyword_norm):
        return keyword_norm in value_norm

    keyword_compact = compact_ascii(keyword_norm)
    value_compact = compact_ascii(value_norm)
    value_tokens = split_ascii_tokens(value_norm)
    if not keyword_compact or not value_compact:
        return False
    return keyword_compact in value_tokens or keyword_compact in value_compact


def keyword_matches_any_value(keyword: Any, values: Iterable[Any]) -> bool:
    return any(keyword_matches_value(keyword, value) for value in values)


def pattern_group_matches(group: Any, values: Iterable[Any]) -> tuple[bool, str]:
    keywords = group if isinstance(group, list) else [group]
    for keyword in keywords:
        if keyword_matches_any_value(keyword, values):
            return True, str(keyword)
    return False, ""


def has_short_room_context(context_values: Iterable[Any] | None) -> bool:
    if not context_values:
        return False
    context = " ".join(str(value or "") for value in context_values if str(value or "").strip())
    context_norm = normalize_value(context)
    if not context_norm:
        return False
    return any(normalize_value(hint) in context_norm for hint in SHORT_ROOM_CONTEXT_HINTS)


def match_short_room_name(
    values: Iterable[Any],
    *,
    context_values: Iterable[Any] | None = None,
) -> dict[str, Any] | None:
    """Match short room labels like exactly '强电'/'弱电' only with CAD context."""
    if not has_short_room_context(context_values):
        return None
    for value in values:
        if is_non_object_explanatory_text(value):
            continue
        value_norm = normalize_value(value)
        for label, canonical in SHORT_ROOM_NAME_RULES.items():
            if value_norm == normalize_value(label):
                return {
                    "role": "inspection_object",
                    "canonical": canonical,
                    "matched_alias": label,
                    "confidence": 0.9,
                    "reason": "short_room_name_context_rule",
                    "needs_llm": False,
                }
    return None


def match_inspection_keyword_pattern(
    values: Iterable[Any],
    *,
    context_values: Iterable[Any] | None = None,
    pattern_path: Path | None = None,
) -> dict[str, Any] | None:
    value_list = [str(value or "") for value in values if str(value or "").strip()]
    if context_values:
        value_list.extend(str(value or "") for value in context_values if str(value or "").strip())
    if not value_list:
        return None

    canonical_set = set(inspection_canonical_names())
    pattern_library = cached_keyword_patterns(str(pattern_path) if pattern_path else "")
    candidates: list[tuple[float, int, int, dict[str, Any]]] = []

    for rule in pattern_library.get("rules", []) or []:
        canonical = str(rule.get("canonical", "") or "").strip()
        if canonical not in canonical_set:
            continue

        exclude_hits = [
            str(keyword)
            for keyword in rule.get("exclude_any", []) or []
            if keyword_matches_any_value(keyword, value_list)
        ]
        if exclude_hits:
            continue

        matched_keywords: list[str] = []
        failed = False
        for keyword in rule.get("must_all", []) or []:
            if keyword_matches_any_value(keyword, value_list):
                matched_keywords.append(str(keyword))
            else:
                failed = True
                break
        if failed:
            continue

        for group in rule.get("must_any", []) or []:
            ok, matched = pattern_group_matches(group, value_list)
            if ok:
                matched_keywords.append(matched)
            else:
                failed = True
                break
        if failed:
            continue

        if not matched_keywords:
            continue

        should_hits = [
            str(keyword)
            for keyword in rule.get("should_any", []) or []
            if keyword_matches_any_value(keyword, value_list)
        ]
        confidence = float(rule.get("confidence", 0.72) or 0.72)
        confidence = min(0.92, confidence + min(len(should_hits), 4) * 0.02)
        candidate = {
            "role": "inspection_object",
            "canonical": canonical,
            "matched_alias": " / ".join(dict.fromkeys(matched_keywords + should_hits)),
            "confidence": confidence,
            "reason": "inspection_keyword_pattern",
            "needs_llm": True,
        }
        candidates.append((confidence, len(matched_keywords), len(should_hits), candidate))

    if candidates:
        candidates.sort(key=lambda item: item[:3], reverse=True)
        return candidates[0][3]

    return None


def match_inspection_object(
    values: Iterable[Any],
    *,
    context_values: Iterable[Any] | None = None,
    library_path: Path | None = None,
) -> dict[str, Any] | None:
    """
    Return a match dict when values hit the standard inspection-object library.

    Only ``canonical``, ``aliases``, and ``abbreviations`` are used. The
    ``context_values`` argument is kept for call-site compatibility, but does
    not affect deterministic matching.
    """
    value_list = [str(value or "") for value in values if str(value or "").strip()]
    if not value_list:
        return None

    short_room_match = match_short_room_name(value_list, context_values=context_values)
    if short_room_match:
        return short_room_match

    library = cached_library(str(library_path) if library_path else "")
    deterministic_candidates: list[tuple[int, int, int, dict[str, Any]]] = []

    for obj in library.get("objects", []) or []:
        for field_priority, (field, confidence, reason) in enumerate(
            [
                ("canonical", 1.0, "inspection_library_canonical"),
                ("aliases", 1.0, "inspection_library_alias"),
                ("abbreviations", 0.96, "inspection_library_abbreviation"),
            ],
            start=1,
        ):
            raw_aliases = [obj.get(field, "")] if field == "canonical" else (obj.get(field, []) or [])
            for matched_alias in matching_aliases(raw_aliases, value_list):
                candidate = {
                    "role": "inspection_object",
                    "canonical": obj.get("canonical", matched_alias),
                    "matched_alias": matched_alias,
                    "confidence": confidence,
                    "reason": reason,
                    "needs_llm": False,
                }
                # Prefer the most specific confirmed alias. This prevents a
                # broad alias like "风机房" from swallowing "补风机房".
                deterministic_candidates.append(
                    (
                        len(normalize_value(matched_alias)),
                        len(normalize_value(obj.get("canonical", ""))),
                        4 - field_priority,
                        candidate,
                    )
                )

    if deterministic_candidates:
        deterministic_candidates.sort(key=lambda item: item[:3], reverse=True)
        return deterministic_candidates[0][3]

    return None


def inspection_library_terms(*, include_ambiguous: bool = False) -> list[str]:
    library = cached_library("")
    terms: list[str] = []
    seen: set[str] = set()
    for obj in library.get("objects", []) or []:
        groups = [
            [obj.get("canonical", "")],
            obj.get("aliases", []) or [],
            obj.get("abbreviations", []) or [],
        ]
        for group in groups:
            for term in group:
                clean = str(term or "").strip()
                key = normalize_value(clean)
                if clean and key not in seen:
                    seen.add(key)
                    terms.append(clean)
    return terms


def inspection_keyword_pattern_terms() -> list[str]:
    pattern_library = cached_keyword_patterns("")
    terms: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                add(item)
            return
        clean = str(value or "").strip()
        key = normalize_value(clean)
        if clean and key not in seen:
            seen.add(key)
            terms.append(clean)

    for rule in pattern_library.get("rules", []) or []:
        add(rule.get("canonical", ""))
        add(rule.get("must_all", []) or [])
        add(rule.get("must_any", []) or [])
        add(rule.get("should_any", []) or [])
    return terms


def inspection_canonical_names() -> list[str]:
    library = cached_library("")
    names: list[str] = []
    seen: set[str] = set()
    for obj in library.get("objects", []) or []:
        clean = str(obj.get("canonical", "") or "").strip()
        key = normalize_value(clean)
        if clean and key not in seen:
            seen.add(key)
            names.append(clean)
    return names
