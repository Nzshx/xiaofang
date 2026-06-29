from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.inspection_object_library import (  # type: ignore
    cached_library,
    inspection_canonical_names,
    match_inspection_keyword_pattern,
    match_inspection_object,
    normalize_value,
)

PROMPT_VERSION = "inspection_binary_mep_open_class_v6_specific_labels_flash_no_thinking"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache" / "llm_inspection"

NOISE_TERMS = (
    "图框",
    "图签",
    "标题栏",
    "轴网",
    "轴线",
    "尺寸",
    "标高",
    "比例",
    "材料表",
    "设备表",
    "说明",
    "设计说明",
    "图例",
    "目录",
    "剖面",
    "剖面图",
    "详图",
    "大样",
    "节点",
    "系统图",
    "原理图",
    "示意图",
)
NOISE_LAYER_MARKERS = (
    "TITLE",
    "FRAME",
    "AXIS",
    "DIM",
    "ANNO",
    "NOTE",
    "TEXT",
    "LEGEND",
    "TABLE",
    "图框",
    "图签",
    "轴网",
    "尺寸",
    "说明",
    "图例",
)
FIRE_CONTEXT_MARKERS = (
    "消防",
    "消火",
    "喷淋",
    "报警",
    "疏散",
    "防火",
    "排烟",
    "补风",
    "正压",
    "应急",
    "配电",
    "强电",
    "弱电",
    "电气",
    "水泵",
    "风机",
    "水池",
    "水箱",
    "给水",
    "排水",
    "雨水",
    "清水",
    "取水",
    "回收池",
    "暖通",
    "通风",
    "空调",
    "管道",
    "设备",
    "设备间",
    "电",
    "水",
    "气",
    "风",
    "井",
    "池",
    "间",
    "FIRE",
    "HYDRANT",
    "SPRINKLER",
    "ALARM",
    "SMOKE",
    "PUMP",
    "FAN",
)
MEP_OBJECT_MARKERS = (
    "消防",
    "消火",
    "取水",
    "给水",
    "排水",
    "雨水",
    "清水",
    "回收池",
    "水池",
    "水箱",
    "水泵",
    "电气",
    "配电",
    "强电",
    "弱电",
    "集气",
    "燃气",
    "暖通",
    "通风",
    "空调",
    "排烟",
    "补风",
    "送风",
    "管道",
    "设备",
)
OBJECT_NOUN_MARKERS = ("间", "房", "室", "厅", "井", "口", "池", "泵", "阀", "机", "箱", "柜", "装置", "设备")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compact_text(value: Any) -> str:
    return normalize_value(str(value or ""))


def has_any(value: Any, markers: tuple[str, ...]) -> bool:
    text = compact_text(value)
    return any(compact_text(marker) in text for marker in markers if compact_text(marker))


def candidate_text(item: dict[str, Any]) -> str:
    return str(item.get("term") or item.get("raw_text") or "").strip()


def candidate_context(item: dict[str, Any]) -> str:
    fields = ("term", "layer", "parent_block_name", "source_type", "entity_type", "geometry_kind")
    return " ".join(str(item.get(field, "") or "") for field in fields)


def is_plain_number_or_code(term: str) -> bool:
    clean = term.strip()
    if not clean:
        return True
    if re.fullmatch(r"[0-9]+(?:[.\-_:xX×][0-9]+)*", clean):
        return True
    if re.fullmatch(r"[A-Za-z]", clean):
        return True
    return False


def is_alphanumeric_code(term: str) -> bool:
    clean = term.strip()
    return bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,24}", clean)
        and re.search(r"[A-Za-z]", clean)
        and re.search(r"\d", clean)
    )


def is_mep_object_like(term: str, context: str) -> bool:
    """Return True for water/electric/HVAC/fire room or device-like CAD terms."""
    text = f"{term} {context}"
    return has_any(text, MEP_OBJECT_MARKERS) and has_any(term, OBJECT_NOUN_MARKERS)


GENERIC_STANDALONE_OBJECT_TERMS = {
    compact_text("机房"),
    compact_text("房间"),
    compact_text("房"),
    compact_text("室"),
    compact_text("间"),
    compact_text("厅"),
}


def is_generic_standalone_object_term(term: Any) -> bool:
    return compact_text(term) in GENERIC_STANDALONE_OBJECT_TERMS


def is_fm_exit_code(term: Any) -> bool:
    compact = compact_text(term).upper()
    return bool(re.fullmatch(r"FM[甲乙丙丁]?\d{1,8}(?:[-_]\d+)?", compact))


def is_protected_inspection_term(term: str) -> bool:
    return bool(match_inspection_object([term]) or match_inspection_keyword_pattern([term]))


def is_noise_candidate(item: dict[str, Any]) -> tuple[bool, str]:
    term = candidate_text(item)
    source_type = str(item.get("source_type") or "").lower()
    entity_type = str(item.get("entity_type") or "").upper()
    geometry_kind = str(item.get("geometry_kind") or "").lower()
    context = candidate_context(item)

    if "text" not in source_type and "block" not in source_type:
        return True, "not_text_or_block_semantics"
    if not term:
        return True, "empty_term"
    if is_generic_standalone_object_term(term):
        return True, "generic_standalone_object_term"
    if is_alphanumeric_code(term):
        return False, ""
    if is_plain_number_or_code(term) and not is_protected_inspection_term(term):
        return True, "plain_number_or_code"
    if has_any(term, NOISE_TERMS):
        return True, "noise_term"
    if entity_type in {"DIMENSION", "LEADER", "MLEADER"}:
        return True, "dimension_entity"
    if geometry_kind in {"dimension", "axis", "title_frame"}:
        return True, "noise_geometry"
    return False, ""


def result_item(
    item: dict[str, Any],
    *,
    role: str,
    class_name: str = "",
    confidence: float = 0.0,
    reason: str = "",
    evidence: list[str] | None = None,
    possible_alias: str = "",
) -> dict[str, Any]:
    term = candidate_text(item)
    return {
        "term": term,
        "role": role,
        "class_name": class_name if role == "inspection_object" else "IGNORE",
        "confidence": round(float(confidence or 0.0), 3),
        "reason": reason,
        "evidence": evidence or [],
        "possible_alias": possible_alias,
        "need_human_review": False,
    }


def local_library_decision(item: dict[str, Any]) -> dict[str, Any] | None:
    term = candidate_text(item)
    if is_fm_exit_code(term):
        return result_item(
            item,
            role="inspection_object",
            class_name="安全出口",
            confidence=0.9,
            reason="fm_fire_door_code_as_exit",
            evidence=[f"term={term}", "FM fire-door code treated as exit"],
            possible_alias=term,
        )
    if is_generic_standalone_object_term(term):
        return None
    context_values = [
        term,
        item.get("layer", ""),
        item.get("parent_block_name", ""),
        item.get("entity_type", ""),
        item.get("geometry_kind", ""),
    ]
    match = match_inspection_object([term], context_values=context_values)
    if match:
        return result_item(
            item,
            role="inspection_object",
            class_name=str(match.get("canonical") or term),
            confidence=float(match.get("confidence") or 0.92),
            reason=str(match.get("reason") or "inspection_library"),
            evidence=[f"matched_alias={match.get('matched_alias', '')}", "local_alias_library"],
            possible_alias=str(match.get("matched_alias") or term),
        )
    pattern = match_inspection_keyword_pattern([term], context_values=context_values)
    if pattern:
        return result_item(
            item,
            role="inspection_object",
            class_name=str(pattern.get("canonical") or term),
            confidence=float(pattern.get("confidence") or 0.82),
            reason=str(pattern.get("reason") or "inspection_keyword_pattern"),
            evidence=[f"matched_keywords={pattern.get('matched_alias', '')}", "local_keyword_pattern"],
            possible_alias=str(pattern.get("matched_alias") or term),
        )
    return None


def short_word_decision(item: dict[str, Any]) -> dict[str, Any] | None:
    term = candidate_text(item)
    context = candidate_context(item)
    norm_term = compact_text(term)
    if is_generic_standalone_object_term(term):
        return None

    def emit(class_name: str, reason: str, confidence: float) -> dict[str, Any]:
        return result_item(
            item,
            role="inspection_object",
            class_name=class_name,
            confidence=confidence,
            reason=reason,
            evidence=[f"term={term}", f"layer={item.get('layer', '')}"],
            possible_alias=term,
        )

    exact = {
        compact_text("强电"): "强电间",
        compact_text("弱电"): "弱电间",
        compact_text("强弱电"): "强弱电间",
        compact_text("电井"): "电井",
        compact_text("消防泵"): "消防水泵",
        compact_text("水泵"): "消防水泵",
        compact_text("消控"): "消防控制室/消控室",
        compact_text("消防控制"): "消防控制室/消控室",
    }
    if norm_term in exact:
        return emit(exact[norm_term], "short_word_exact_rule", 0.86)

    if "楼梯" in term or "疏散楼梯" in term or "扶梯" in term:
        return None
    if "电梯" in term and has_any(context, ("消防", "FIRE", "ELEV", "LIFT")):
        return emit("消防电梯", "short_lift_fire_context", 0.82)
    if "井" in term and has_any(context, ("电", "强电", "弱电", "配电", "电缆", "消防")):
        return emit("电井", "short_well_layer_context", 0.78)
    if "泵" in term and has_any(context, ("消防", "消火", "喷淋", "给水", "水泵", "PUMP")):
        if has_any(context, ("房", "室", "间", "泵房")):
            return emit("消防水泵房/消防泵房", "short_pump_room_context", 0.80)
        return emit("消防水泵", "short_pump_fire_context", 0.78)
    if "阀" in term and has_any(context, ("消防", "报警", "喷淋", "水", "VALVE")):
        return emit("报警阀组", "short_valve_fire_context", 0.76)
    if "梯" in term and has_any(context, ("消防", "电梯", "ELEV", "LIFT")):
        return emit("消防电梯", "short_lift_context", 0.76)
    if has_any(term, ("房", "室", "间", "所")):
        if has_any(context, ("消防水泵", "消防泵", "泵房", "PUMP")):
            return emit("消防水泵房/消防泵房", "short_room_pump_context", 0.80)
        if has_any(context, ("配电", "强电", "弱电", "变配电", "电气")):
            return emit("配电房", "short_room_power_context", 0.78)
        if has_any(context, ("风机", "送风", "排风", "补风", "FAN")):
            return emit("风机房", "short_room_fan_context", 0.78)
    return None


def alias_terms() -> list[tuple[str, str]]:
    terms: list[tuple[str, str]] = []
    for obj in cached_library("").get("objects", []) or []:
        canonical = str(obj.get("canonical") or "").strip()
        for value in [canonical, *(obj.get("aliases", []) or []), *(obj.get("abbreviations", []) or [])]:
            alias = str(value or "").strip()
            if canonical and alias:
                terms.append((canonical, alias))
    return terms


def fuzzy_decision(item: dict[str, Any]) -> dict[str, Any] | None:
    term = candidate_text(item)
    norm_term = compact_text(term)
    if is_generic_standalone_object_term(term):
        return None
    if len(norm_term) < 2:
        return None
    context = candidate_context(item)
    if not has_any(context, FIRE_CONTEXT_MARKERS) and len(norm_term) < 4:
        return None

    best: tuple[float, str, str] | None = None
    for canonical, alias in alias_terms():
        norm_alias = compact_text(alias)
        if len(norm_alias) < 2:
            continue
        if norm_alias in norm_term or norm_term in norm_alias:
            ratio = 0.88 if min(len(norm_alias), len(norm_term)) >= 2 else 0.0
        else:
            ratio = SequenceMatcher(None, norm_term, norm_alias).ratio()
        if ratio >= 0.78 and (best is None or ratio > best[0]):
            best = (ratio, canonical, alias)
    if not best:
        return None
    ratio, canonical, alias = best
    return result_item(
        item,
        role="inspection_object",
        class_name=canonical,
        confidence=min(0.84, max(0.62, ratio)),
        reason="local_fuzzy_alias_recall",
        evidence=[f"term={term}", f"similar_alias={alias}", f"layer={item.get('layer', '')}"],
        possible_alias=alias,
    )


def stable_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "term": candidate_text(item),
        "source_type": str(item.get("source_type") or ""),
        "layer": str(item.get("layer") or ""),
        "parent_block_name": str(item.get("parent_block_name") or ""),
        "entity_type": str(item.get("entity_type") or ""),
        "geometry_kind": str(item.get("geometry_kind") or ""),
        "prompt_version": PROMPT_VERSION,
    }


def file_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def candidate_fingerprint(item: dict[str, Any], *, model: str) -> str:
    payload = {
        **stable_item(item),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "library_hash": file_sha256(PROJECT_ROOT / "configs" / "inspection_object_aliases.json"),
        "pattern_hash": file_sha256(PROJECT_ROOT / "configs" / "inspection_object_keyword_patterns.json"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_path(cache_dir: Path, fingerprint: str) -> Path:
    return cache_dir / fingerprint[:2] / f"{fingerprint}.json"


def read_cache(cache_dir: Path, fingerprint: str) -> dict[str, Any] | None:
    path = cache_path(cache_dir, fingerprint)
    if not path.exists():
        return None
    try:
        payload = read_json(path)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def write_cache(cache_dir: Path, fingerprint: str, decision: dict[str, Any]) -> None:
    write_json(cache_path(cache_dir, fingerprint), decision)


def build_messages(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    classes = inspection_canonical_names()
    system = (
        "你是建筑消防 CAD 巡检对象二阶段判别器。"
        "本地规则已经过滤大部分噪声，你只需要判断候选是否属于消防巡检对象。"
        "只能输出 JSON，不要输出解释性自然语言。"
    )
    user_payload = {
        "task": "binary_classification",
        "allowed_roles": ["inspection_object", "IGNORE"],
        "reference_inspection_classes": classes,
        "judgement_rules": [
            "只在 inspection_object 和 IGNORE 两类之间判断。",
            "判断对象时可使用 term、source_type、layer、parent_block_name、entity_type、geometry_kind。",
            "layer 只能作为消防/水/电/暖通/风/报警语义上下文，不能单独作为对象名称。",
            "不要仅因为 layer 包含 ANNO、TABL、TEXT、PUB_TEXT 等注释/文字图层特征就输出 IGNORE；噪声判断必须主要依据 term 或 parent_block_name 本身是否为图签、图框、轴网、尺寸、剖面、详图、说明、图例等。",
            "图签、图框、轴网、尺寸、剖面、详图、说明、图例、纯编号、材料表、设备表应输出 IGNORE。",
            "数字和字母组合不能仅因为像编号就排除；如果 layer/block/term 具有消防、水、电、暖通、通风、给排水语义，应继续判断。",
            "FM甲/乙/丙/丁加数字的门编号应按安全出口理解，不要判为防火卷帘。",
            "单独的泛词如机房、房间、室、厅不能直接作为巡检对象；必须有热水机房、油烟井、空调机位等具体原词或明确语义。",
            "如果候选明显是消防背景下的设备、房间、井、泵、阀、梯、报警、疏散标志，或属于水、电、气、暖通、通风、给排水相关房间/设施，可输出 inspection_object。",
            "class_name 优先保留候选原词里的具体对象名；无法映射但确属巡检相关时，可以输出简洁的新对象名，例如消防取水口、集气间、管道设备间、雨水回收池、空调间、油烟井。",
            "证据不足或只是图纸说明/标题/图例/尺寸标注时输出 IGNORE。",
        ],
        "output_schema": {
            "inspection_decisions": [
                {
                    "term": "候选原词",
                    "role": "inspection_object 或 IGNORE",
                    "class_name": "标准巡检对象类别或简洁新对象名；IGNORE 时为 IGNORE",
                    "confidence": 0.0,
                    "reason": "简短原因",
                    "evidence": ["命中的文字/图层/块名证据"],
                    "possible_alias": "可能别名",
                    "need_human_review": False,
                }
            ]
        },
        "candidates": [stable_item(item) for item in items],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def parse_llm_json(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?", "", clean).strip()
        clean = re.sub(r"```$", "", clean).strip()
    try:
        payload = json.loads(clean)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            payload = json.loads(clean[start : end + 1])
            return payload if isinstance(payload, dict) else {}
    return {}


def request_deepseek(
    items: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int,
    temperature: float,
    max_retries: int,
) -> list[dict[str, Any]]:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": build_messages(items),
        "temperature": temperature,
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(max(1, max_retries)):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            content = response_payload["choices"][0]["message"]["content"]
            parsed = parse_llm_json(content)
            decisions = parsed.get("inspection_decisions", [])
            return [item for item in decisions if isinstance(item, dict)]
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < max_retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"DeepSeek request failed: {last_error}")


def normalize_llm_decision(raw: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    term = candidate_text(original)
    role = str(raw.get("role") or "").strip()
    class_name = str(raw.get("class_name") or "").strip()
    confidence = float(raw.get("confidence") or 0.0)
    if role != "inspection_object":
        return result_item(
            original,
            role="IGNORE",
            class_name="IGNORE",
            confidence=min(confidence, 0.5),
            reason=str(raw.get("reason") or "llm_ignore"),
            evidence=list(raw.get("evidence", []) or []),
            possible_alias=str(raw.get("possible_alias") or term),
        )
    if not class_name or class_name.upper() == "IGNORE":
        class_name = term
    return result_item(
        original,
        role="inspection_object",
        class_name=class_name,
        confidence=max(0.6, min(1.0, confidence)),
        reason=str(raw.get("reason") or "llm_binary_classification"),
        evidence=list(raw.get("evidence", []) or []),
        possible_alias=str(raw.get("possible_alias") or term),
    )


def deduplicate_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key_payload = {
            "term": compact_text(candidate_text(item)),
            "source_type": str(item.get("source_type") or ""),
            "layer": compact_text(item.get("layer", "")),
            "parent_block_name": compact_text(item.get("parent_block_name", "")),
            "entity_type": str(item.get("entity_type") or "").upper(),
            "geometry_kind": str(item.get("geometry_kind") or "").lower(),
        }
        key = hashlib.sha256(json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        groups.setdefault(key, []).append(item)
    unique = [values[0] for values in groups.values()]
    return unique, groups


def classify_items(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else DEFAULT_CACHE_DIR
    payload = read_json(input_path)
    raw_items = payload.get("uncertain_inspection_candidates", []) if isinstance(payload, dict) else []
    items = [item for item in raw_items if isinstance(item, dict)]
    unique_items, duplicate_groups = deduplicate_items(items)

    decisions_by_term: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    skipped_noise = 0
    local_hits = 0
    cache_hits = 0

    api_key = args.api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = args.base_url or os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL
    model = args.model or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL
    if model.strip().lower() == "deepseek-chat":
        model = DEFAULT_MODEL

    for item in unique_items:
        term_key = compact_text(candidate_text(item))
        local = local_library_decision(item) or short_word_decision(item) or fuzzy_decision(item)
        if local:
            local_hits += 1
            decisions_by_term[term_key] = local
            continue
        noise, noise_reason = is_noise_candidate(item)
        if noise:
            skipped_noise += 1
            decisions_by_term[term_key] = result_item(item, role="IGNORE", class_name="IGNORE", confidence=0.0, reason=noise_reason)
            continue
        fingerprint = candidate_fingerprint(item, model=model)
        cached = read_cache(cache_dir, fingerprint)
        if cached:
            cache_hits += 1
            decisions_by_term[term_key] = cached
            continue
        pending.append(item)

    llm_calls = 0
    llm_error = ""
    if pending and not args.no_llm and api_key:
        for start in range(0, len(pending), args.batch_size):
            batch = pending[start : start + args.batch_size]
            llm_calls += 1
            try:
                raw_decisions = request_deepseek(
                    batch,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    timeout=args.timeout,
                    temperature=args.temperature,
                    max_retries=args.max_retries,
                )
            except Exception as exc:
                llm_error = str(exc)
                break
            raw_by_term = {compact_text(item.get("term", "")): item for item in raw_decisions}
            for item in batch:
                term_key = compact_text(candidate_text(item))
                decision = normalize_llm_decision(raw_by_term.get(term_key, {}), item)
                decisions_by_term[term_key] = decision
                write_cache(cache_dir, candidate_fingerprint(item, model=model), decision)

    for item in pending:
        term_key = compact_text(candidate_text(item))
        if term_key in decisions_by_term:
            continue
        reason = "llm_disabled_or_missing_api_key" if args.no_llm or not api_key else "llm_unavailable_fallback_ignore"
        decisions_by_term[term_key] = result_item(item, role="IGNORE", class_name="IGNORE", confidence=0.0, reason=reason)

    # Expand decisions back to original candidate terms. The region pipeline
    # keys results by normalized term, so one decision per term is sufficient.
    decisions = list(decisions_by_term.values())
    inspection = [item["term"] for item in decisions if item.get("role") == "inspection_object"]
    ignored = [item["term"] for item in decisions if item.get("role") != "inspection_object"]
    result = {
        "description": "Second-stage binary inspection-object judgement for uncertain text/block CAD semantics.",
        "rule_version": PROMPT_VERSION,
        "source_json": str(input_path),
        "model": "" if args.no_llm else model,
        "counts": {
            "input_candidates": len(items),
            "unique_candidates": len(unique_items),
            "duplicate_groups": len(duplicate_groups),
            "noise_ignored": skipped_noise,
            "local_hits": local_hits,
            "cache_hits": cache_hits,
            "llm_calls": llm_calls,
            "inspection_object": len(inspection),
            "IGNORE": len(ignored),
        },
        "inspection_object": inspection,
        "IGNORE": ignored,
        "inspection_decisions": decisions,
    }
    if pending and (args.no_llm or not api_key):
        result["pipeline_warning"] = "LLM was not called; missing API key or --no-llm enabled. Local rules/fuzzy recall were used, remaining candidates were ignored."
    if llm_error:
        result["pipeline_warning"] = f"LLM call failed; remaining candidates were ignored. {llm_error}"
    if args.save_debug:
        result["debug"] = {
            "pending_terms": [candidate_text(item) for item in pending],
            "cache_dir": str(cache_dir),
            "base_url": base_url,
            "has_api_key": bool(api_key),
        }
    write_json(output_path, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeepSeek second-stage CAD inspection-object classifier.")
    parser.add_argument("--input", required=True, help="Path to region_llm_candidates.json")
    parser.add_argument("--output", required=True, help="Path to write classified JSON")
    parser.add_argument("--save-debug", action="store_true", help="Include debug metadata in output JSON")
    parser.add_argument("--no-llm", action="store_true", help="Disable remote LLM and use local fallback only")
    parser.add_argument("--api-key", default="sk-ef81c56cc5a5485a9c05ecaed9016794", help="DeepSeek API key. Defaults to DEEPSEEK_API_KEY or OPENAI_API_KEY")
    parser.add_argument("--base-url", default="https://api.deepseek.com", help="OpenAI-compatible base URL. Defaults to DEEPSEEK_BASE_URL or DeepSeek")
    parser.add_argument("--model", default="", help="Model name. Defaults to DEEPSEEK_MODEL or deepseek-v4-flash")
    parser.add_argument("--batch-size", type=int, default=24, help="Candidates per LLM request")
    parser.add_argument("--timeout", type=int, default=90, help="HTTP timeout seconds")
    parser.add_argument("--temperature", type=float, default=0.0, help="LLM temperature")
    parser.add_argument("--max-retries", type=int, default=2, help="HTTP retry count")
    parser.add_argument("--cache-dir", default="", help="Per-candidate LLM cache directory")
    return parser.parse_args()


def main() -> None:
    result = classify_items(parse_args())
    print(json.dumps(result.get("counts", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
