"""Consolidated production stage.

The implementation below is migrated into this file and does not import the
legacy project Python sources at runtime.
"""

from __future__ import annotations

import sys
import types


def _register_embedded_module(name, namespace, *, aliases=()):
    module = types.ModuleType(name)
    module.__dict__.update(namespace)
    module.__name__ = name
    module.__package__ = name.rpartition(".")[0]
    sys.modules[name] = module
    for alias in aliases:
        sys.modules[alias] = module
    if "." in name:
        parent_name, child_name = name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is None:
            parent = types.ModuleType(parent_name)
            parent.__path__ = []
            sys.modules[parent_name] = parent
        setattr(parent, child_name, module)
    return module


def _register_stub_module(name, **symbols):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    return _register_embedded_module(name, symbols)

# -----------------------------------------------------------------------------
# Migrated implementation: agents/inspection_object_library.py
# -----------------------------------------------------------------------------
def _build_s04_library():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'agents/inspection_object_library.py'
    )
    __name__ = 'agents.inspection_object_library'
    __package__ = 'agents'
    """
    Inspection object alias library.

    This module is intentionally deterministic. It answers only two questions:
    1. Does a CAD term match a confirmed inspection object canonical name?
    2. Does a CAD term match a confirmed inspection object alias or abbreviation?

    Only ``canonical``, ``aliases``, and ``abbreviations`` participate in the
    deterministic rule library. Any hit is treated as an inspection object.
    """
    import json
    import re
    from functools import lru_cache
    from pathlib import Path
    from typing import Any, Iterable
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    DEFAULT_LIBRARY_PATH = PROJECT_ROOT / 'configs' / 'inspection_object_aliases.json'
    DEFAULT_KEYWORD_PATTERN_PATH = PROJECT_ROOT / 'configs' / 'inspection_object_keyword_patterns.json'
    EXPLANATORY_NON_OBJECT_MARKERS = ['详图', '详建施', '做法', '说明', '图例', '专项设计', '参照', '要求设置', '计算', '标注', '宽度', '疏散宽度', '尺寸', '编号', '标高', '轴号']
    SHORT_ROOM_NAME_RULES = {'强电': '强电间', '弱电': '弱电间'}
    SHORT_ROOM_CONTEXT_HINTS = ['TEXT', 'MTEXT', 'ATTRIB', 'text', '房间', '房名', '门', '地下一层', '地下二层', '地库']

    def normalize_value(value: Any) -> str:
        text = str(value or '').strip()
        text = text.replace('（', '(').replace('）', ')')
        text = text.replace('\u3000', ' ')
        text = re.sub('\\s+', '', text)
        return text.upper()

    def compact_ascii(value: Any) -> str:
        return re.sub('[^A-Z0-9]+', '', str(value or '').upper())

    def split_ascii_tokens(value: Any) -> set[str]:
        return {part for part in re.split('[^A-Z0-9]+', str(value or '').upper()) if part}

    def has_chinese(value: Any) -> bool:
        return bool(re.search('[\\u4e00-\\u9fff]', str(value or '')))

    def is_non_object_explanatory_text(value: Any) -> bool:
        """Return True for notes/details that mention an object but are not objects."""
        text = normalize_value(value)
        if not text:
            return False
        return any((normalize_value(marker) in text for marker in EXPLANATORY_NON_OBJECT_MARKERS))

    def load_library(path: Path | None=None) -> dict[str, Any]:
        library_path = path or DEFAULT_LIBRARY_PATH
        with library_path.open('r', encoding='utf-8') as f:
            return json.load(f)

    def load_keyword_patterns(path: Path | None=None) -> dict[str, Any]:
        pattern_path = path or DEFAULT_KEYWORD_PATTERN_PATH
        if not pattern_path.exists():
            return {'rules': []}
        with pattern_path.open('r', encoding='utf-8') as f:
            return json.load(f)

    @lru_cache(maxsize=4)
    def cached_library(path_text: str='') -> dict[str, Any]:
        return load_library(Path(path_text) if path_text else DEFAULT_LIBRARY_PATH)

    @lru_cache(maxsize=4)
    def cached_keyword_patterns(path_text: str='') -> dict[str, Any]:
        return load_keyword_patterns(Path(path_text) if path_text else DEFAULT_KEYWORD_PATTERN_PATH)

    def alias_matches_value(alias: str, value: Any, *, ambiguous: bool=False) -> bool:
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
        if len(alias_compact) <= 5:
            return value_compact.startswith(alias_compact) or value_compact.endswith(alias_compact) or bool(re.fullmatch(f'{re.escape(alias_compact)}\\d+', value_compact))
        if ambiguous:
            return False
        return alias_compact in value_compact

    def matching_aliases(aliases: Iterable[str], values: Iterable[Any], *, ambiguous: bool=False) -> list[str]:
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
        return any((keyword_matches_value(keyword, value) for value in values))

    def pattern_group_matches(group: Any, values: Iterable[Any]) -> tuple[bool, str]:
        keywords = group if isinstance(group, list) else [group]
        for keyword in keywords:
            if keyword_matches_any_value(keyword, values):
                return (True, str(keyword))
        return (False, '')

    def has_short_room_context(context_values: Iterable[Any] | None) -> bool:
        if not context_values:
            return False
        context = ' '.join((str(value or '') for value in context_values if str(value or '').strip()))
        context_norm = normalize_value(context)
        if not context_norm:
            return False
        return any((normalize_value(hint) in context_norm for hint in SHORT_ROOM_CONTEXT_HINTS))

    def match_short_room_name(values: Iterable[Any], *, context_values: Iterable[Any] | None=None) -> dict[str, Any] | None:
        """Match short room labels like exactly '强电'/'弱电' only with CAD context."""
        if not has_short_room_context(context_values):
            return None
        for value in values:
            if is_non_object_explanatory_text(value):
                continue
            value_norm = normalize_value(value)
            for label, canonical in SHORT_ROOM_NAME_RULES.items():
                if value_norm == normalize_value(label):
                    return {'role': 'inspection_object', 'canonical': canonical, 'matched_alias': label, 'confidence': 0.9, 'reason': 'short_room_name_context_rule', 'needs_llm': False}
        return None

    def match_inspection_keyword_pattern(values: Iterable[Any], *, context_values: Iterable[Any] | None=None, pattern_path: Path | None=None) -> dict[str, Any] | None:
        value_list = [str(value or '') for value in values if str(value or '').strip()]
        if context_values:
            value_list.extend((str(value or '') for value in context_values if str(value or '').strip()))
        if not value_list:
            return None
        canonical_set = set(inspection_canonical_names())
        pattern_library = cached_keyword_patterns(str(pattern_path) if pattern_path else '')
        candidates: list[tuple[float, int, int, dict[str, Any]]] = []
        for rule in pattern_library.get('rules', []) or []:
            canonical = str(rule.get('canonical', '') or '').strip()
            if canonical not in canonical_set:
                continue
            exclude_hits = [str(keyword) for keyword in rule.get('exclude_any', []) or [] if keyword_matches_any_value(keyword, value_list)]
            if exclude_hits:
                continue
            matched_keywords: list[str] = []
            failed = False
            for keyword in rule.get('must_all', []) or []:
                if keyword_matches_any_value(keyword, value_list):
                    matched_keywords.append(str(keyword))
                else:
                    failed = True
                    break
            if failed:
                continue
            for group in rule.get('must_any', []) or []:
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
            should_hits = [str(keyword) for keyword in rule.get('should_any', []) or [] if keyword_matches_any_value(keyword, value_list)]
            confidence = float(rule.get('confidence', 0.72) or 0.72)
            confidence = min(0.92, confidence + min(len(should_hits), 4) * 0.02)
            candidate = {'role': 'inspection_object', 'canonical': canonical, 'matched_alias': ' / '.join(dict.fromkeys(matched_keywords + should_hits)), 'confidence': confidence, 'reason': 'inspection_keyword_pattern', 'needs_llm': True}
            candidates.append((confidence, len(matched_keywords), len(should_hits), candidate))
        if candidates:
            candidates.sort(key=lambda item: item[:3], reverse=True)
            return candidates[0][3]
        return None

    def match_inspection_object(values: Iterable[Any], *, context_values: Iterable[Any] | None=None, library_path: Path | None=None) -> dict[str, Any] | None:
        """
        Return a match dict when values hit the standard inspection-object library.

        Only ``canonical``, ``aliases``, and ``abbreviations`` are used. The
        ``context_values`` argument is kept for call-site compatibility, but does
        not affect deterministic matching.
        """
        value_list = [str(value or '') for value in values if str(value or '').strip()]
        if not value_list:
            return None
        short_room_match = match_short_room_name(value_list, context_values=context_values)
        if short_room_match:
            return short_room_match
        library = cached_library(str(library_path) if library_path else '')
        deterministic_candidates: list[tuple[int, int, int, dict[str, Any]]] = []
        for obj in library.get('objects', []) or []:
            for field_priority, (field, confidence, reason) in enumerate([('canonical', 1.0, 'inspection_library_canonical'), ('aliases', 1.0, 'inspection_library_alias'), ('abbreviations', 0.96, 'inspection_library_abbreviation')], start=1):
                raw_aliases = [obj.get(field, '')] if field == 'canonical' else obj.get(field, []) or []
                for matched_alias in matching_aliases(raw_aliases, value_list):
                    candidate = {'role': 'inspection_object', 'canonical': obj.get('canonical', matched_alias), 'matched_alias': matched_alias, 'confidence': confidence, 'reason': reason, 'needs_llm': False}
                    deterministic_candidates.append((len(normalize_value(matched_alias)), len(normalize_value(obj.get('canonical', ''))), 4 - field_priority, candidate))
        if deterministic_candidates:
            deterministic_candidates.sort(key=lambda item: item[:3], reverse=True)
            return deterministic_candidates[0][3]
        return None

    def inspection_canonical_names() -> list[str]:
        library = cached_library('')
        names: list[str] = []
        seen: set[str] = set()
        for obj in library.get('objects', []) or []:
            clean = str(obj.get('canonical', '') or '').strip()
            key = normalize_value(clean)
            if clean and key not in seen:
                seen.add(key)
                names.append(clean)
        return names
    return dict(locals())

_s04_library = _register_embedded_module(
    'agents.inspection_object_library',
    _build_s04_library(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: agents/inspection_class_fuzzy_matcher.py
# -----------------------------------------------------------------------------
def _build_s04_fuzzy():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'agents/inspection_class_fuzzy_matcher.py'
    )
    __name__ = 'agents.inspection_class_fuzzy_matcher'
    __package__ = 'agents'
    """Closed-set fuzzy mapping from CAD object names to inspection classes."""
    import math
    from collections import Counter
    from dataclasses import asdict, dataclass
    from functools import lru_cache
    from typing import Any
    from agents.inspection_object_library import cached_library, normalize_value
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        TfidfVectorizer = None

    @dataclass(frozen=True)
    class StandardClassMatch:
        standard_class_name: str
        score: float
        levenshtein_similarity: float
        jaro_winkler_similarity: float
        semantic_vector_similarity: float
        matched_reference: str

        def to_dict(self) -> dict[str, Any]:
            return asdict(self)

    def levenshtein_similarity(left: Any, right: Any) -> float:
        a = normalize_value(left)
        b = normalize_value(right)
        if a == b:
            return 1.0
        if not a or not b:
            return 0.0
        if len(a) < len(b):
            a, b = (b, a)
        previous = list(range(len(b) + 1))
        for row, char_a in enumerate(a, start=1):
            current = [row]
            for column, char_b in enumerate(b, start=1):
                current.append(min(current[column - 1] + 1, previous[column] + 1, previous[column - 1] + (char_a != char_b)))
            previous = current
        distance = previous[-1]
        return max(0.0, 1.0 - distance / max(len(a), len(b)))

    def jaro_winkler_similarity(left: Any, right: Any, scaling: float=0.1) -> float:
        a = normalize_value(left)
        b = normalize_value(right)
        if a == b:
            return 1.0
        if not a or not b:
            return 0.0
        match_distance = max(len(a), len(b)) // 2 - 1
        match_distance = max(match_distance, 0)
        a_matches = [False] * len(a)
        b_matches = [False] * len(b)
        matches = 0
        for index_a, char_a in enumerate(a):
            start = max(0, index_a - match_distance)
            end = min(index_a + match_distance + 1, len(b))
            for index_b in range(start, end):
                if b_matches[index_b] or char_a != b[index_b]:
                    continue
                a_matches[index_a] = True
                b_matches[index_b] = True
                matches += 1
                break
        if not matches:
            return 0.0
        matched_a = [char for char, matched in zip(a, a_matches) if matched]
        matched_b = [char for char, matched in zip(b, b_matches) if matched]
        transpositions = sum((char_a != char_b for char_a, char_b in zip(matched_a, matched_b))) / 2.0
        jaro = (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3.0
        prefix = 0
        for char_a, char_b in zip(a, b):
            if char_a != char_b or prefix == 4:
                break
            prefix += 1
        return min(1.0, jaro + prefix * scaling * (1.0 - jaro))

    def _char_ngrams(value: Any, min_n: int=1, max_n: int=3) -> Counter[str]:
        text = normalize_value(value)
        grams: Counter[str] = Counter()
        for size in range(min_n, max_n + 1):
            for index in range(max(0, len(text) - size + 1)):
                grams[text[index:index + size]] += 1
        return grams

    def _counter_cosine(left: Counter[str], right: Counter[str]) -> float:
        if not left or not right:
            return 0.0
        dot = sum((value * right.get(key, 0) for key, value in left.items()))
        norm_left = math.sqrt(sum((value * value for value in left.values())))
        norm_right = math.sqrt(sum((value * value for value in right.values())))
        return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0

    @lru_cache(maxsize=1)
    def _class_profiles() -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], tuple[str, ...]]:
        names: list[str] = []
        references: list[tuple[str, ...]] = []
        documents: list[str] = []
        for item in cached_library('').get('objects', []) or []:
            canonical = str(item.get('canonical') or '').strip()
            if not canonical:
                continue
            values = [canonical, *(item.get('aliases', []) or []), *(item.get('abbreviations', []) or []), *(item.get('semantic_examples', []) or [])]
            clean = tuple(dict.fromkeys((str(value).strip() for value in values if str(value).strip())))
            names.append(canonical)
            references.append(clean)
            documents.append(' '.join(clean))
        return (tuple(names), tuple(references), tuple(documents))

    @lru_cache(maxsize=1)
    def _tfidf_index() -> tuple[Any, Any] | None:
        if TfidfVectorizer is None:
            return None
        _names, _references, documents = _class_profiles()
        vectorizer = TfidfVectorizer(analyzer='char', ngram_range=(1, 3), lowercase=False, norm='l2')
        matrix = vectorizer.fit_transform(documents)
        return (vectorizer, matrix)

    def _semantic_similarities(query: str) -> list[float]:
        names, _references, documents = _class_profiles()
        index = _tfidf_index()
        if index is not None:
            vectorizer, matrix = index
            query_vector = vectorizer.transform([query])
            return [float(value) for value in (matrix @ query_vector.T).toarray().ravel()]
        query_grams = _char_ngrams(query)
        return [_counter_cosine(query_grams, _char_ngrams(document)) for document in documents[:len(names)]]

    def match_standard_class(original_object_name: Any, *, proposed_class_name: Any='', possible_alias: Any='') -> StandardClassMatch:
        """Always return the best one of the configured canonical classes."""
        names, references, _documents = _class_profiles()
        if not names:
            raise RuntimeError('巡检对象标准分类库为空。')
        proposed = str(proposed_class_name or '').strip()
        canonical_by_key = {normalize_value(name): name for name in names}
        if normalize_value(proposed) in canonical_by_key:
            canonical = canonical_by_key[normalize_value(proposed)]
            return StandardClassMatch(canonical, 1.0, 1.0, 1.0, 1.0, canonical)
        source_values = tuple(dict.fromkeys((str(value).strip() for value in (proposed, possible_alias, original_object_name) if str(value).strip())))
        if not source_values:
            source_values = ('巡检对象',)
        semantic_scores = _semantic_similarities(' '.join(source_values))
        best: StandardClassMatch | None = None
        for index, canonical in enumerate(names):
            best_reference = canonical
            best_edit = 0.0
            best_jaro = 0.0
            best_reference_score = -1.0
            for reference in references[index]:
                edit = max((levenshtein_similarity(source, reference) for source in source_values))
                jaro = max((jaro_winkler_similarity(source, reference) for source in source_values))
                reference_score = 0.55 * edit + 0.45 * jaro
                if reference_score > best_reference_score:
                    best_reference_score = reference_score
                    best_reference = reference
                    best_edit = edit
                    best_jaro = jaro
            semantic = semantic_scores[index] if index < len(semantic_scores) else 0.0
            score = 0.35 * best_edit + 0.25 * best_jaro + 0.4 * semantic
            candidate = StandardClassMatch(standard_class_name=canonical, score=round(score, 6), levenshtein_similarity=round(best_edit, 6), jaro_winkler_similarity=round(best_jaro, 6), semantic_vector_similarity=round(semantic, 6), matched_reference=best_reference)
            if best is None or candidate.score > best.score:
                best = candidate
        if best is None:
            raise RuntimeError('无法生成巡检对象标准分类。')
        return best

    def standard_class_names() -> tuple[str, ...]:
        return _class_profiles()[0]
    return dict(locals())

_s04_fuzzy = _register_embedded_module(
    'agents.inspection_class_fuzzy_matcher',
    _build_s04_fuzzy(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: agents/inspection_binary_classifier.py
# -----------------------------------------------------------------------------
def _build_s04_binary():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'agents/inspection_binary_classifier.py'
    )
    __name__ = 'agents.inspection_binary_classifier'
    __package__ = 'agents'
    import json
    from collections import Counter, defaultdict
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any, Iterable
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    DEFAULT_TRAINING_RUN_DIR = PROJECT_ROOT / 'outputs' / 'fire_inspection_pipeline' / '邻里中心1_2_3_楼平面剖面图0620_t3_20260711135015'

    def _text(value: Any) -> str:
        return ' '.join(str(value or '').strip().split())

    def candidate_feature_text(item: dict[str, Any]) -> str:
        """Build one character-model feature string with explicit CAD context fields."""
        original_name = _text(item.get('original_object_name') or item.get('term') or item.get('raw_text'))
        return ' '.join((f'NAME={original_name}', f"LAYER={_text(item.get('layer'))}", f"BLOCK={_text(item.get('parent_block_name'))}", f"ENTITY={_text(item.get('entity_type')).upper()}"))

    def _read_json(path: Path) -> Any:
        with path.open('r', encoding='utf-8') as handle:
            return json.load(handle)

    def _label_from_decision(decision: dict[str, Any]) -> int | None:
        role = str(decision.get('role') or '').strip()
        if role == 'inspection_object':
            return 1
        if role == 'IGNORE':
            return 0
        return None

    def _cache_training_samples(cache_dir: Path) -> list[tuple[str, int]]:
        samples: list[tuple[str, int]] = []
        if not cache_dir.exists():
            return samples
        for prefix_dir in cache_dir.iterdir():
            if not prefix_dir.is_dir() or len(prefix_dir.name) != 2:
                continue
            for path in prefix_dir.glob('*.json'):
                try:
                    decision = _read_json(path)
                except (OSError, ValueError, TypeError):
                    continue
                if not isinstance(decision, dict):
                    continue
                label = _label_from_decision(decision)
                if label is None or not _text(decision.get('original_object_name') or decision.get('term')):
                    continue
                samples.append((candidate_feature_text(decision), label))
        return samples

    def _run_training_samples(run_dir: Path) -> list[tuple[str, int]]:
        inspection_dir = run_dir / 'inspection_objects'
        candidates_path = inspection_dir / 'region_llm_candidates.json'
        decisions_path = inspection_dir / 'region_llm_classified.json'
        if not candidates_path.exists() or not decisions_path.exists():
            return []
        try:
            candidate_payload = _read_json(candidates_path)
            decision_payload = _read_json(decisions_path)
        except (OSError, ValueError, TypeError):
            return []
        candidates = candidate_payload.get('uncertain_inspection_candidates', []) if isinstance(candidate_payload, dict) else []
        decisions = decision_payload.get('inspection_decisions', []) if isinstance(decision_payload, dict) else []
        contexts: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            key = _text(candidate.get('original_object_name') or candidate.get('term')).casefold()
            if key:
                contexts[key] = candidate
        samples: list[tuple[str, int]] = []
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            label = _label_from_decision(decision)
            name = _text(decision.get('original_object_name') or decision.get('term'))
            if label is None or not name:
                continue
            context = dict(contexts.get(name.casefold(), {}))
            context['original_object_name'] = name
            samples.append((candidate_feature_text(context), label))
        return samples

    @dataclass(frozen=True)
    class TrainingSummary:
        available: bool
        sample_count: int
        positive_count: int
        negative_count: int
        cache_sample_count: int
        run_sample_count: int
        reason: str = ''

    @dataclass(frozen=True)
    class BinaryPrediction:
        inspection_probability: float

    class InspectionBinaryClassifier:

        def __init__(self, vectorizer: Any, estimator: Any, summary: TrainingSummary) -> None:
            self.vectorizer = vectorizer
            self.estimator = estimator
            self.summary = summary

        def predict_many(self, items: Iterable[dict[str, Any]]) -> list[BinaryPrediction]:
            rows = list(items)
            if not rows:
                return []
            features = self.vectorizer.transform([candidate_feature_text(item) for item in rows])
            probabilities = self.estimator.predict_proba(features)[:, 1]
            return [BinaryPrediction(inspection_probability=max(0.0, min(1.0, float(probability)))) for probability in probabilities]

    def _deduplicate_samples(samples: Iterable[tuple[str, int]]) -> tuple[list[str], list[int]]:
        votes: dict[str, Counter[int]] = defaultdict(Counter)
        for feature, label in samples:
            if feature:
                votes[feature][label] += 1
        features: list[str] = []
        labels: list[int] = []
        for feature in sorted(votes):
            counts = votes[feature]
            label = 1 if counts[1] > counts[0] else 0
            features.append(feature)
            labels.append(label)
        return (features, labels)

    def train_inspection_binary_classifier(cache_dir: Path, training_run_dirs: Iterable[Path]=()) -> tuple[InspectionBinaryClassifier | None, TrainingSummary]:
        cache_samples = _cache_training_samples(cache_dir)
        run_samples: list[tuple[str, int]] = []
        seen_runs: set[Path] = set()
        for run_dir in training_run_dirs:
            resolved = Path(run_dir).expanduser().resolve()
            if resolved in seen_runs:
                continue
            seen_runs.add(resolved)
            run_samples.extend(_run_training_samples(resolved))
        features, labels = _deduplicate_samples([*cache_samples, *run_samples])
        counts = Counter(labels)
        base_summary = TrainingSummary(available=False, sample_count=len(labels), positive_count=counts[1], negative_count=counts[0], cache_sample_count=len(cache_samples), run_sample_count=len(run_samples))
        if len(labels) < 50 or min(counts[0], counts[1]) < 10:
            return (None, TrainingSummary(**{**base_summary.__dict__, 'reason': 'insufficient_binary_training_samples'}))
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression
        except ImportError:
            return (None, TrainingSummary(**{**base_summary.__dict__, 'reason': 'scikit_learn_unavailable'}))
        vectorizer = TfidfVectorizer(analyzer='char', ngram_range=(2, 5), min_df=2, max_features=60000, sublinear_tf=True)
        matrix = vectorizer.fit_transform(features)
        estimator = LogisticRegression(C=20.0, class_weight='balanced', max_iter=1000, random_state=0, solver='liblinear')
        estimator.fit(matrix, labels)
        summary = TrainingSummary(**{**base_summary.__dict__, 'available': True})
        return (InspectionBinaryClassifier(vectorizer, estimator, summary), summary)
    return dict(locals())

_s04_binary = _register_embedded_module(
    'agents.inspection_binary_classifier',
    _build_s04_binary(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: scripts/llm-deepseekv4.py
# -----------------------------------------------------------------------------
def _build_s04_llm():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'scripts/llm-deepseekv4.py'
    )
    __name__ = 'scripts.llm_deepseekv4'
    __package__ = 'scripts'
    import argparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import hashlib
    import json
    import os
    import re
    import sys
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path
    from typing import Any
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from agents.inspection_object_library import match_inspection_keyword_pattern, match_inspection_object, normalize_value
    from agents.inspection_class_fuzzy_matcher import match_standard_class, standard_class_names
    from agents.inspection_binary_classifier import DEFAULT_TRAINING_RUN_DIR, train_inspection_binary_classifier
    PROMPT_VERSION = 'inspection_binary_closed_39_v7_hybrid_fuzzy_no_review'
    DEFAULT_BASE_URL = 'https://api.deepseek.com'
    DEFAULT_MODEL = 'deepseek-v4-flash'
    DEFAULT_CACHE_DIR = PROJECT_ROOT / '.cache' / 'llm_inspection'
    LLM_CONFIG_PATH = PROJECT_ROOT / 'fire_inspection_system' / 'configs' / 'llm_api.json'
    NOISE_TERMS = ('图框', '图签', '标题栏', '轴网', '轴线', '尺寸', '标高', '比例', '材料表', '设备表', '说明', '设计说明', '图例', '目录', '剖面', '剖面图', '详图', '大样', '节点', '系统图', '原理图', '示意图')
    NOISE_LAYER_MARKERS = ('TITLE', 'FRAME', 'AXIS', 'DIM', 'ANNO', 'NOTE', 'TEXT', 'LEGEND', 'TABLE', '图框', '图签', '轴网', '尺寸', '说明', '图例')
    FIRE_CONTEXT_MARKERS = ('消防', '消火', '喷淋', '报警', '疏散', '防火', '排烟', '补风', '正压', '应急', '配电', '强电', '弱电', '电气', '水泵', '风机', '水池', '水箱', '给水', '排水', '雨水', '清水', '取水', '回收池', '暖通', '通风', '空调', '管道', '设备', '设备间', '电', '水', '气', '风', '井', '池', '间', 'FIRE', 'HYDRANT', 'SPRINKLER', 'ALARM', 'SMOKE', 'PUMP', 'FAN')
    MEP_OBJECT_MARKERS = ('消防', '消火', '取水', '给水', '排水', '雨水', '清水', '回收池', '水池', '水箱', '水泵', '电气', '配电', '强电', '弱电', '集气', '燃气', '暖通', '通风', '空调', '排烟', '补风', '送风', '管道', '设备')
    OBJECT_NOUN_MARKERS = ('间', '房', '室', '厅', '井', '口', '池', '泵', '阀', '机', '箱', '柜', '装置', '设备')

    def read_json(path: Path) -> Any:
        with path.open('r', encoding='utf-8') as handle:
            return json.load(handle)

    def write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def llm_api_config_defaults() -> dict[str, str]:
        if not LLM_CONFIG_PATH.is_file():
            return {}
        try:
            payload = read_json(LLM_CONFIG_PATH)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            key: str(payload.get(key) or '').strip()
            for key in ('api_key', 'base_url', 'model')
        }

    def compact_text(value: Any) -> str:
        return normalize_value(str(value or ''))

    def has_any(value: Any, markers: tuple[str, ...]) -> bool:
        text = compact_text(value)
        return any((compact_text(marker) in text for marker in markers if compact_text(marker)))

    def candidate_text(item: dict[str, Any]) -> str:
        return str(item.get('term') or item.get('raw_text') or '').strip()

    def candidate_context(item: dict[str, Any]) -> str:
        fields = ('term', 'layer', 'parent_block_name', 'source_type', 'entity_type', 'geometry_kind')
        return ' '.join((str(item.get(field, '') or '') for field in fields))

    def is_plain_number_or_code(term: str) -> bool:
        clean = term.strip()
        if not clean:
            return True
        if re.fullmatch('[0-9]+(?:[.\\-_:xX×][0-9]+)*', clean):
            return True
        if re.fullmatch('[A-Za-z]', clean):
            return True
        return False

    def is_alphanumeric_code(term: str) -> bool:
        clean = term.strip()
        return bool(re.fullmatch('[A-Za-z0-9][A-Za-z0-9_\\-]{1,24}', clean) and re.search('[A-Za-z]', clean) and re.search('\\d', clean))
    GENERIC_STANDALONE_OBJECT_TERMS = {compact_text('机房'), compact_text('房间'), compact_text('房'), compact_text('室'), compact_text('间'), compact_text('厅')}

    def is_generic_standalone_object_term(term: Any) -> bool:
        return compact_text(term) in GENERIC_STANDALONE_OBJECT_TERMS

    def is_fm_exit_code(term: Any) -> bool:
        compact = compact_text(term).upper()
        return bool(re.fullmatch('FM[甲乙丙丁]?\\d{1,8}(?:[-_]\\d+)?', compact))

    def is_protected_inspection_term(term: str) -> bool:
        return bool(match_inspection_object([term]) or match_inspection_keyword_pattern([term]))

    def is_noise_candidate(item: dict[str, Any]) -> tuple[bool, str]:
        term = candidate_text(item)
        source_type = str(item.get('source_type') or '').lower()
        entity_type = str(item.get('entity_type') or '').upper()
        geometry_kind = str(item.get('geometry_kind') or '').lower()
        context = candidate_context(item)
        if 'text' not in source_type and 'block' not in source_type:
            return (True, 'not_text_or_block_semantics')
        if not term:
            return (True, 'empty_term')
        if is_generic_standalone_object_term(term):
            return (True, 'generic_standalone_object_term')
        if is_alphanumeric_code(term):
            return (False, '')
        if is_plain_number_or_code(term) and (not is_protected_inspection_term(term)):
            return (True, 'plain_number_or_code')
        if has_any(term, NOISE_TERMS):
            return (True, 'noise_term')
        if entity_type in {'DIMENSION', 'LEADER', 'MLEADER'}:
            return (True, 'dimension_entity')
        if geometry_kind in {'dimension', 'axis', 'title_frame'}:
            return (True, 'noise_geometry')
        return (False, '')

    def result_item(item: dict[str, Any], *, role: str, class_name: str='', confidence: float=0.0, reason: str='', evidence: list[str] | None=None, possible_alias: str='') -> dict[str, Any]:
        term = candidate_text(item)
        standard_name = class_name if role == 'inspection_object' else 'IGNORE'
        return {'term': term, 'original_object_name': term, 'role': role, 'class_name': standard_name, 'standard_class_name': standard_name, 'confidence': round(float(confidence or 0.0), 3), 'reason': reason, 'evidence': evidence or []}

    def local_library_decision(item: dict[str, Any]) -> dict[str, Any] | None:
        term = candidate_text(item)
        if is_fm_exit_code(term):
            return result_item(item, role='inspection_object', class_name='安全出口', confidence=0.9, reason='fm_fire_door_code_as_exit', evidence=[f'term={term}', 'FM fire-door code treated as exit'], possible_alias=term)
        if is_generic_standalone_object_term(term):
            return None
        context_values = [term, item.get('layer', ''), item.get('parent_block_name', ''), item.get('entity_type', ''), item.get('geometry_kind', '')]
        match = match_inspection_object([term], context_values=context_values)
        if match:
            return result_item(item, role='inspection_object', class_name=str(match.get('canonical') or term), confidence=float(match.get('confidence') or 0.92), reason=str(match.get('reason') or 'inspection_library'), evidence=[f"matched_alias={match.get('matched_alias', '')}", 'local_alias_library'], possible_alias=str(match.get('matched_alias') or term))
        pattern = match_inspection_keyword_pattern([term], context_values=context_values)
        if pattern:
            return result_item(item, role='inspection_object', class_name=str(pattern.get('canonical') or term), confidence=float(pattern.get('confidence') or 0.82), reason=str(pattern.get('reason') or 'inspection_keyword_pattern'), evidence=[f"matched_keywords={pattern.get('matched_alias', '')}", 'local_keyword_pattern'], possible_alias=str(pattern.get('matched_alias') or term))
        return None

    def short_word_decision(item: dict[str, Any]) -> dict[str, Any] | None:
        term = candidate_text(item)
        context = candidate_context(item)
        norm_term = compact_text(term)
        if is_generic_standalone_object_term(term):
            return None

        def emit(class_name: str, reason: str, confidence: float) -> dict[str, Any]:
            return result_item(item, role='inspection_object', class_name=class_name, confidence=confidence, reason=reason, evidence=[f'term={term}', f"layer={item.get('layer', '')}"], possible_alias=term)
        exact = {compact_text('强电'): '强电间', compact_text('弱电'): '弱电间', compact_text('强弱电'): '强弱电间', compact_text('电井'): '电井', compact_text('消防泵'): '消防水泵', compact_text('水泵'): '消防水泵', compact_text('消控'): '消防控制室/消控室', compact_text('消防控制'): '消防控制室/消控室'}
        if norm_term in exact:
            return emit(exact[norm_term], 'short_word_exact_rule', 0.86)
        if '楼梯' in term or '疏散楼梯' in term or '扶梯' in term:
            return None
        if '电梯' in term and has_any(context, ('消防', 'FIRE', 'ELEV', 'LIFT')):
            return emit('消防电梯', 'short_lift_fire_context', 0.82)
        if '井' in term and has_any(context, ('电', '强电', '弱电', '配电', '电缆', '消防')):
            return emit('电井', 'short_well_layer_context', 0.78)
        if '泵' in term and has_any(context, ('消防', '消火', '喷淋', '给水', '水泵', 'PUMP')):
            if has_any(context, ('房', '室', '间', '泵房')):
                return emit('消防水泵房/消防泵房', 'short_pump_room_context', 0.8)
            return emit('消防水泵', 'short_pump_fire_context', 0.78)
        if '阀' in term and has_any(context, ('消防', '报警', '喷淋', '水', 'VALVE')):
            return emit('报警阀组', 'short_valve_fire_context', 0.76)
        if '梯' in term and has_any(context, ('消防', '电梯', 'ELEV', 'LIFT')):
            return emit('消防电梯', 'short_lift_context', 0.76)
        if has_any(term, ('房', '室', '间', '所')):
            if has_any(context, ('消防水泵', '消防泵', '泵房', 'PUMP')):
                return emit('消防水泵房/消防泵房', 'short_room_pump_context', 0.8)
            if has_any(context, ('配电', '强电', '弱电', '变配电', '电气')):
                return emit('配电房', 'short_room_power_context', 0.78)
            if has_any(context, ('风机', '送风', '排风', '补风', 'FAN')):
                return emit('风机房', 'short_room_fan_context', 0.78)
        return None

    def stable_item(item: dict[str, Any]) -> dict[str, Any]:
        return {'term': candidate_text(item), 'source_type': str(item.get('source_type') or ''), 'layer': str(item.get('layer') or ''), 'parent_block_name': str(item.get('parent_block_name') or ''), 'entity_type': str(item.get('entity_type') or ''), 'geometry_kind': str(item.get('geometry_kind') or ''), 'prompt_version': PROMPT_VERSION}

    def file_sha256(path: Path) -> str:
        if not path.exists():
            return ''
        h = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()

    def candidate_fingerprint(item: dict[str, Any], *, model: str) -> str:
        payload = {**stable_item(item), 'model': model, 'prompt_version': PROMPT_VERSION, 'library_hash': file_sha256(PROJECT_ROOT / 'configs' / 'inspection_object_aliases.json'), 'pattern_hash': file_sha256(PROJECT_ROOT / 'configs' / 'inspection_object_keyword_patterns.json')}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def cache_path(cache_dir: Path, fingerprint: str) -> Path:
        return cache_dir / fingerprint[:2] / f'{fingerprint}.json'

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
        classes = list(standard_class_names())
        system = '你是建筑消防 CAD 巡检对象二阶段封闭分类器。本地规则已经过滤大部分噪声。你需要判断候选是否属于消防巡检对象；如果属于，standard_class_name 必须且只能从给定的39个标准分类中选择。只能输出 JSON，不要输出解释性自然语言。'
        user_payload = {'task': 'binary_classification', 'allowed_roles': ['inspection_object', 'IGNORE'], 'reference_inspection_classes': classes, 'judgement_rules': ['只在 inspection_object 和 IGNORE 两类之间判断。', 'inspection_object 的 standard_class_name 必须从 standard_classes 原样选择，禁止创建新分类。', '无法直接确定时也必须选择语义最接近的标准分类，不输出 OTHER、REVIEW 或待人工复核。', '判断对象时可使用 term、source_type、layer、parent_block_name、entity_type、geometry_kind。', 'layer 只能作为消防/水/电/暖通/风/报警语义上下文，不能单独作为对象名称。', '不要仅因为 layer 包含 ANNO、TABL、TEXT、PUB_TEXT 等注释/文字图层特征就输出 IGNORE；噪声判断必须主要依据 term 或 parent_block_name 本身是否为图签、图框、轴网、尺寸、剖面、详图、说明、图例等。', '图签、图框、轴网、尺寸、剖面、详图、说明、图例、纯编号、材料表、设备表应输出 IGNORE。', '数字和字母组合不能仅因为像编号就排除；如果 layer/block/term 具有消防、水、电、暖通、通风、给排水语义，应继续判断。', 'FM甲/乙/丙/丁加数字的门编号应按安全出口理解，不要判为防火卷帘。', '单独的泛词如机房、房间、室、厅不能直接作为巡检对象；必须有热水机房、油烟井、空调机位等具体原词或明确语义。', '如果候选明显是消防背景下的设备、房间、井、泵、阀、梯、报警、疏散标志，或属于水、电、气、暖通、通风、给排水相关房间/设施，可输出 inspection_object。', 'original_object_name 必须保留候选原词 term，不得改写为标准分类名。', '防火门属于安全出口巡检语义时，standard_class_name 选择安全出口。', '证据不足或只是图纸说明/标题/图例/尺寸标注时输出 IGNORE。'], 'standard_classes': classes, 'output_schema': {'inspection_decisions': [{'original_object_name': '候选原词', 'role': 'inspection_object 或 IGNORE', 'standard_class_name': '必须为standard_classes中的一个；IGNORE时为IGNORE', 'confidence': 0.0, 'reason': '简短原因', 'evidence': ['命中的文字/图层/块名证据']}]}, 'candidates': [stable_item(item) for item in items]}
        return [{'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps(user_payload, ensure_ascii=False)}]

    def parse_llm_json(text: str) -> dict[str, Any]:
        clean = text.strip()
        if clean.startswith('```'):
            clean = re.sub('^```(?:json)?', '', clean).strip()
            clean = re.sub('```$', '', clean).strip()
        try:
            payload = json.loads(clean)
            return payload if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            start = clean.find('{')
            end = clean.rfind('}')
            if start >= 0 and end > start:
                payload = json.loads(clean[start:end + 1])
                return payload if isinstance(payload, dict) else {}
        return {}

    def request_deepseek(items: list[dict[str, Any]], *, api_key: str, base_url: str, model: str, timeout: int, temperature: float, max_retries: int) -> list[dict[str, Any]]:
        url = base_url.rstrip('/') + '/chat/completions'
        body = {'model': model, 'messages': build_messages(items), 'temperature': temperature, 'enable_thinking': False, 'response_format': {'type': 'json_object'}}
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        last_error: Exception | None = None
        for attempt in range(max(0, max_retries) + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method='POST')
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    response_payload = json.loads(response.read().decode('utf-8'))
                content = response_payload['choices'][0]['message']['content']
                parsed = parse_llm_json(content)
                decisions = parsed.get('inspection_decisions', [])
                valid_decisions = [item for item in decisions if isinstance(item, dict)]
                expected_terms = {compact_text(candidate_text(item)) for item in items}
                returned_terms = {compact_text(item.get('original_object_name') or item.get('term')) for item in valid_decisions}
                missing_terms = expected_terms - returned_terms
                if missing_terms:
                    raise ValueError(f'LLM response omitted {len(missing_terms)} candidates')
                return valid_decisions
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt >= max_retries:
                    break
                time.sleep(float(2 ** attempt))
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                time.sleep(float(2 ** attempt))
        raise RuntimeError(f'DeepSeek request failed: {last_error}')

    def request_llm_batches(batches: list[tuple[int, list[dict[str, Any]]]], *, api_key: str, base_url: str, model: str, timeout: int, temperature: float, max_retries: int, max_concurrency: int) -> tuple[dict[int, list[dict[str, Any]]], dict[int, str]]:
        """Run network-only batch workers and retain deterministic batch identifiers."""
        results: dict[int, list[dict[str, Any]]] = {}
        errors: dict[int, str] = {}
        if not batches:
            return (results, errors)
        worker_count = max(1, min(max_concurrency, len(batches)))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix='inspection-llm') as executor:
            future_to_batch = {executor.submit(request_deepseek, batch, api_key=api_key, base_url=base_url, model=model, timeout=timeout, temperature=temperature, max_retries=max_retries): batch_id for batch_id, batch in batches}
            for future in as_completed(future_to_batch):
                batch_id = future_to_batch[future]
                try:
                    results[batch_id] = future.result()
                except Exception as exc:
                    errors[batch_id] = str(exc)
        return (results, errors)

    def normalize_llm_decision(raw: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
        term = candidate_text(original)
        role = str(raw.get('role') or '').strip()
        proposed_class = str(raw.get('standard_class_name') or raw.get('class_name') or '').strip()
        confidence = float(raw.get('confidence') or 0.0)
        if role != 'inspection_object':
            return result_item(original, role='IGNORE', class_name='IGNORE', confidence=min(confidence, 0.5), reason=str(raw.get('reason') or 'llm_ignore'), evidence=list(raw.get('evidence', []) or []), possible_alias=str(raw.get('possible_alias') or term))
        possible_alias = str(raw.get('possible_alias') or term)
        matched = match_standard_class(term, proposed_class_name=proposed_class, possible_alias=possible_alias)
        evidence = list(raw.get('evidence', []) or [])
        canonical_set = set(standard_class_names())
        if proposed_class not in canonical_set:
            evidence.append(f'hybrid_class_mapping={matched.standard_class_name};score={matched.score:.3f};levenshtein={matched.levenshtein_similarity:.3f};jaro_winkler={matched.jaro_winkler_similarity:.3f};semantic_vector={matched.semantic_vector_similarity:.3f};reference={matched.matched_reference}')
        return result_item(original, role='inspection_object', class_name=matched.standard_class_name, confidence=max(0.6, min(1.0, 0.7 * confidence + 0.3 * matched.score)), reason=str(raw.get('reason') or 'llm_closed_set_classification'), evidence=evidence, possible_alias=possible_alias)

    def deduplicate_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            key_payload = {'term': compact_text(candidate_text(item)), 'source_type': str(item.get('source_type') or ''), 'layer': compact_text(item.get('layer', '')), 'parent_block_name': compact_text(item.get('parent_block_name', '')), 'entity_type': str(item.get('entity_type') or '').upper(), 'geometry_kind': str(item.get('geometry_kind') or '').lower()}
            key = hashlib.sha256(json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
            groups.setdefault(key, []).append(item)
        unique = [values[0] for values in groups.values()]
        return (unique, groups)

    def classify_items(args: argparse.Namespace) -> dict[str, Any]:
        input_path = Path(args.input).resolve()
        output_path = Path(args.output).resolve()
        cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else DEFAULT_CACHE_DIR
        payload = read_json(input_path)
        raw_items = payload.get('uncertain_inspection_candidates', []) if isinstance(payload, dict) else []
        items = [item for item in raw_items if isinstance(item, dict)]
        unique_items, duplicate_groups = deduplicate_items(items)
        decisions_by_term: dict[str, dict[str, Any]] = {}
        model_candidates: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        skipped_noise = 0
        local_hits = 0
        cache_hits = 0
        defaults = llm_api_config_defaults()
        api_key = args.api_key or os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_API_KEY') or defaults.get('api_key', '')
        base_url = args.base_url or os.getenv('DEEPSEEK_BASE_URL') or defaults.get('base_url') or DEFAULT_BASE_URL
        model = args.model or os.getenv('DEEPSEEK_MODEL') or defaults.get('model') or DEFAULT_MODEL
        if model.strip().lower() == 'deepseek-chat':
            model = DEFAULT_MODEL
        for item in unique_items:
            term_key = compact_text(candidate_text(item))
            local = local_library_decision(item) or short_word_decision(item)
            if local:
                local_hits += 1
                decisions_by_term[term_key] = local
                continue
            noise, noise_reason = is_noise_candidate(item)
            if noise:
                skipped_noise += 1
                decisions_by_term[term_key] = result_item(item, role='IGNORE', class_name='IGNORE', confidence=0.0, reason=noise_reason)
                continue
            fingerprint = candidate_fingerprint(item, model=model)
            cached = read_cache(cache_dir, fingerprint)
            if cached:
                cache_hits += 1
                decisions_by_term[term_key] = cached
                continue
            model_candidates.append(item)
        classifier_ignored = 0
        classifier_positive = 0
        classifier_uncertain = len(model_candidates)
        classifier_summary: dict[str, Any] = {'available': False, 'sample_count': 0, 'positive_count': 0, 'negative_count': 0, 'cache_sample_count': 0, 'run_sample_count': 0, 'reason': 'local_classifier_disabled'}
        if model_candidates and (not getattr(args, 'no_local_classifier', False)):
            training_runs = [DEFAULT_TRAINING_RUN_DIR]
            training_runs.extend((Path(value) for value in getattr(args, 'local_classifier_training_run', []) if value))
            classifier, summary = train_inspection_binary_classifier(cache_dir, training_runs)
            classifier_summary = dict(summary.__dict__)
            if classifier:
                classifier_uncertain = 0
                negative_threshold = float(getattr(args, 'local_negative_threshold', 0.01))
                positive_threshold = float(getattr(args, 'local_positive_threshold', 0.99))
                predictions = classifier.predict_many(model_candidates)
                for item, prediction in zip(model_candidates, predictions):
                    probability = prediction.inspection_probability
                    term_key = compact_text(candidate_text(item))
                    if probability < negative_threshold:
                        classifier_ignored += 1
                        decisions_by_term[term_key] = result_item(item, role='IGNORE', class_name='IGNORE', confidence=1.0 - probability, reason='local_tfidf_logistic_ignore', evidence=[f'inspection_probability={probability:.6f}'])
                        continue
                    if probability > positive_threshold:
                        classifier_positive += 1
                        matched = match_standard_class(candidate_text(item))
                        decisions_by_term[term_key] = result_item(item, role='inspection_object', class_name=matched.standard_class_name, confidence=max(0.6, min(1.0, 0.7 * probability + 0.3 * matched.score)), reason='local_tfidf_positive_hybrid_class_mapping', evidence=[f'inspection_probability={probability:.6f}', f'hybrid_class_score={matched.score:.6f}'], possible_alias=candidate_text(item))
                        continue
                    classifier_uncertain += 1
                    pending.append(item)
            else:
                pending.extend(model_candidates)
        else:
            pending.extend(model_candidates)
        llm_calls = 0
        llm_batch_errors: dict[int, str] = {}
        if pending and (not args.no_llm) and api_key:
            batches = [(batch_id, pending[start:start + args.batch_size]) for batch_id, start in enumerate(range(0, len(pending), args.batch_size))]
            llm_calls = len(batches)
            raw_results, llm_batch_errors = request_llm_batches(batches, api_key=api_key, base_url=base_url, model=model, timeout=args.timeout, temperature=args.temperature, max_retries=args.max_retries, max_concurrency=args.max_concurrency)
            for batch_id, batch in batches:
                if batch_id not in raw_results:
                    continue
                raw_decisions = raw_results[batch_id]
                raw_by_term = {compact_text(item.get('original_object_name') or item.get('term', '')): item for item in raw_decisions}
                for item in batch:
                    term_key = compact_text(candidate_text(item))
                    decision = normalize_llm_decision(raw_by_term.get(term_key, {}), item)
                    decisions_by_term[term_key] = decision
                    write_cache(cache_dir, candidate_fingerprint(item, model=model), decision)
        for item in pending:
            term_key = compact_text(candidate_text(item))
            if term_key in decisions_by_term:
                continue
            reason = 'llm_disabled_or_missing_api_key' if args.no_llm or not api_key else 'llm_unavailable_fallback_ignore'
            decisions_by_term[term_key] = result_item(item, role='IGNORE', class_name='IGNORE', confidence=0.0, reason=reason)
        canonical_set = set(standard_class_names())
        hybrid_mapped_count = 0
        for decision in decisions_by_term.values():
            decision.pop('need_human_review', None)
            original_name = str(decision.get('original_object_name') or decision.get('term') or '').strip()
            decision['term'] = original_name
            decision['original_object_name'] = original_name
            decision.pop('possible_alias', None)
            decision.pop('display_class_name', None)
            if decision.get('role') != 'inspection_object':
                decision['class_name'] = 'IGNORE'
                decision['standard_class_name'] = 'IGNORE'
                continue
            proposed = str(decision.get('standard_class_name') or decision.get('class_name') or '').strip()
            if proposed not in canonical_set:
                matched = match_standard_class(original_name, proposed_class_name=proposed, possible_alias=decision.get('possible_alias', ''))
                proposed = matched.standard_class_name
                hybrid_mapped_count += 1
            decision['class_name'] = proposed
            decision['standard_class_name'] = proposed
        decisions = list(decisions_by_term.values())
        inspection = [item['term'] for item in decisions if item.get('role') == 'inspection_object']
        ignored = [item['term'] for item in decisions if item.get('role') != 'inspection_object']
        result = {'description': 'Second-stage binary inspection-object judgement for uncertain text/block CAD semantics.', 'rule_version': PROMPT_VERSION, 'source_json': str(input_path), 'model': '' if args.no_llm else model, 'counts': {'input_candidates': len(items), 'unique_candidates': len(unique_items), 'duplicate_groups': len(duplicate_groups), 'noise_ignored': skipped_noise, 'local_hits': local_hits, 'cache_hits': cache_hits, 'local_classifier_ignored': classifier_ignored, 'local_classifier_positive': classifier_positive, 'local_classifier_uncertain': classifier_uncertain, 'local_classifier_training_samples': classifier_summary['sample_count'], 'llm_calls': llm_calls, 'llm_failed_batches': len(llm_batch_errors), 'llm_batch_size': args.batch_size, 'llm_max_concurrency': args.max_concurrency, 'standard_class_count': len(canonical_set), 'hybrid_mapped_count': hybrid_mapped_count, 'inspection_object': len(inspection), 'IGNORE': len(ignored)}, 'inspection_object': inspection, 'IGNORE': ignored, 'inspection_decisions': decisions}
        if pending and (args.no_llm or not api_key):
            result['pipeline_warning'] = 'LLM was not called; missing API key or --no-llm enabled. Local rules were used, remaining candidates were ignored.'
        if llm_batch_errors:
            failed = '; '.join((f'batch {batch_id}: {message}' for batch_id, message in sorted(llm_batch_errors.items())))
            result['pipeline_warning'] = f'Some LLM batches failed; only those batches used fallback IGNORE. {failed}'
        if args.save_debug:
            result['debug'] = {'pending_terms': [candidate_text(item) for item in pending], 'cache_dir': str(cache_dir), 'base_url': base_url, 'has_api_key': bool(api_key), 'llm_batch_errors': llm_batch_errors, 'local_classifier': classifier_summary}
        write_json(output_path, result)
        return result
    return dict(locals())

_s04_llm = _register_embedded_module(
    'scripts.llm_deepseekv4',
    _build_s04_llm(),
    aliases=('llm_deepseekv4',),
)

# -----------------------------------------------------------------------------
# Migrated implementation: scripts/build_region_inspection_inventory.py
# -----------------------------------------------------------------------------
def _build_s04_region():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'scripts/build_region_inspection_inventory.py'
    )
    __name__ = 'scripts.build_region_inspection_inventory'
    __package__ = 'scripts'
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
    from agents.inspection_object_library import inspection_canonical_names, is_non_object_explanatory_text, match_inspection_keyword_pattern, match_inspection_object, normalize_value
    from agents.inspection_class_fuzzy_matcher import match_standard_class
    FULL_INVENTORY_FILE = 'cad_object_inventory.csv'
    SEMANTIC_INVENTORY_FILE = 'cad_semantic_inventory.csv'
    SEMANTIC_ENTITY_TYPES = {'TEXT', 'MTEXT', 'ATTRIB', 'INSERT'}
    RESULT_FILE = 'region_inspection_results.json'
    LLM_INPUT_FILE = 'region_llm_candidates.json'
    LLM_OUTPUT_FILE = 'region_llm_classified.json'
    SCHEMA_VERSION = 2
    GENERIC_TERMS = {'', '0', 'DEFPOINTS', 'TEXT', 'PUB_TEXT', 'PUB_DIM', 'DIM', 'AXIS', 'NOTE', 'ANNO', 'HATCH', 'SOLID', 'LINE', 'LWPOLYLINE', 'POLYLINE', 'CONTINUOUS', 'BYLAYER', 'MODEL', '图框', '标题栏', '图签', '说明', '标注', '轴线', '填充', '文字'}
    NORMALIZED_GENERIC_TERMS = {normalize_value(item) for item in GENERIC_TERMS}
    GENERIC_TERMS = {normalize_value(item) for item in GENERIC_TERMS}
    NOISE_PATTERN = re.compile('^(?:[-+]?\\d+(?:\\.\\d+)?|\\d+[xX×]\\d+)$', re.IGNORECASE)
    CONTEXT_FIELDS = ('layer', 'parent_block_name', 'entity_type', 'geometry_kind')
    TEXT_EVIDENCE_ONLY_CLASSES = {'避难间', '消防水泵房/消防泵房', '开闭所', '用户变', '配电房', '消防控制室/消控室', '风机房', '进风机房', '补风机房', '强电间', '弱电间', '强弱电间', '电井', '储存装置间/灭火剂储存装置/驱动装置', '供水水源/消防水池', '消防水泵', '备用发电机/柴油发电机房', '变配电房'}
    TEXT_EVIDENCE_ONLY_CLASS_KEYS = {normalize_value(item) for item in TEXT_EVIDENCE_ONLY_CLASSES}
    TEXT_OR_BLOCK_CLASSES = {'安全出口', '排烟机', '消防电梯', '防火卷帘', '灭火器', '供水装置', '报警阀组', '喷头', '消防水箱', '室外（内）消火栓', '灭火装置', '管网与喷头', '消防应急照明和疏散指示标志', '火灾探测器', '消防通讯', '布线', '应急广播及警报装置', '区域显示器', '手动报警按钮', '火灾报警控制器', '消防联动控制器及消防控制室图形显示装置'}
    TEXT_OR_BLOCK_CLASS_KEYS = {normalize_value(item) for item in TEXT_OR_BLOCK_CLASSES}
    INSPECTION_SEMANTIC_HINTS = ('消防', '消火', '灭火', '报警', '应急', '疏散', '安全出口', '避难', '排烟', '补风', '进风', '排风', '风机', '水泵', '喷头', '水箱', '水池', '卷帘', '配电', '变配电', '强电', '弱电', '电井', '控制室', '发电机', '柴油发电机', '火灾', '探测器', '广播', '联动', '电气', '取水', '给水', '排水', '雨水', '清水', '水井', '回收池', '集气', '燃气', '暖通', '通风', '风井', '空调', '管道', '设备', '设备间')
    TITLE_BLOCK_PARENT_MARKERS = ('PMSHEET', 'TITLE', 'TK_LABEL', 'TK-LABEL', '图框', '图签', '标题栏', '会签', 'DRAWINGTITLE', 'SHEET', 'BORDER')
    EXPLICIT_DRAWING_NOISE_TERMS = ('轴网', '轴线', '标注', '尺寸', '图框', '图例', '剖面', '剖面图', '剖面符号', '断面', '立面', '详图', '大样', '大样图', '设计说明', '施工说明', '说明', '索引', '图号', '比例', '标高', '材料表', '设备表', '目录', '会签', '地下车库', '平面图', '系统图', '原理图', '示意图', '节点', '节点图', '轴', '轴号', '轴圈', '定位轴', '深度', '宽度', '高度', '长度', '半径', '直径', '坡度', '面积', '容积', '基坑', 'AXIS', 'DIM', 'DIMENSION', 'NOTE', 'ANNO', 'ANNOTATION', 'LEGEND', 'SECTION', 'ELEVATION', 'DETAIL', 'SCHEDULE', 'DRAWING', 'TITLE', 'BORDER')
    MEASUREMENT_OR_FLOOR_RANGE_PATTERN = re.compile('(?:\\d+(?:\\.\\d+)?\\s*(?:m|mm|cm|kg|kw|kva|%|㎡|m2|m3)\\b|[-+]?\\d+\\s*[fF]\\s*(?:至|~|-|到)\\s*[-+]?\\d+\\s*[fF])', re.IGNORECASE)
    FLOOR_CODE_ONLY_PATTERN = re.compile('^(?:[bB]\\d+(?:[-_~至到][bB]?\\d+)?|\\d+[fF]|[-+]?\\d+(?:[-_~至到]\\d+)?[fF]|[-+]?\\d+[-_~至到]\\d+[fF]?)$', re.IGNORECASE)
    DISCIPLINE_TITLE_TERMS = ('WATER SUPPLY', 'WATERSUPPLY', 'DRAIN', 'DRAINAGE', 'ELECTRICAL', 'HVAC', 'FIRE PROTECTION', 'FIREPROTECTION', 'ARCHITECTURE', 'STRUCTURE', 'PLUMBING', 'MECHANICAL', '给排水', '暖通', '电气', '建筑', '结构', '消防设计', '消防平面', '防火分区')
    INSPECTION_HINTS = ('消防', '消火', '灭火', '报警', '应急', '疏散', '安全出口', '避难', '排烟', '补风', '进风', '排风', '风机', '水泵', '喷头', '水箱', '水池', '卷帘', '配电', '强电', '弱电', '电井', '控制室', '发电机', '电气', '取水', '给水', '排水', '雨水', '清水', '水井', '回收池', '集气', '燃气', '暖通', '通风', '风井', '空调', '管道', '设备', '设备间')
    HARD_EXCLUSION_TERMS = ('图框', '图签', '标题栏', '设计说明', '施工说明', '材料表', '设备表', '详图', '大样图', '剖面图', '立面图', '系统图', '索引图', '平面图', '轴线', '尺寸', '标高', '比例', '做法', '图号', '编号', '日期')
    EXCLUDED_TEXT_LAYER_MARKERS = ('TK_LABEL', 'TK-LABEL', 'TITLE', 'NOPRINT', 'PUB_DIM', 'DIM_', 'AXIS', 'ANNO', 'NOTE', '图框', '图签', '标题栏', '索引')
    PROTECTED_CODE_PREFIXES = ('FJ', 'XF', 'SB')
    MEP_OBJECT_MARKERS = ('消防', '消火', '取水', '给水', '排水', '雨水', '清水', '回收池', '水池', '水箱', '水泵', '水', '电气', '配电', '强电', '弱电', '电', '集气', '燃气', '气', '暖通', '通风', '空调', '排烟', '补风', '排风', '送风', '风', '管道', '设备')
    OBJECT_NOUN_MARKERS = ('间', '房', '室', '厅', '井', '口', '池', '泵', '阀', '机', '箱', '柜', '装置', '设备')

    def read_json(path: Path) -> Any:
        """读取 UTF-8 JSON 文件并返回 Python 对象。"""
        with path.open('r', encoding='utf-8') as handle:
            return json.load(handle)

    def write_json(path: Path, payload: Any) -> None:
        """将 Python 对象写为 UTF-8 JSON 文件，自动创建父目录并保留中文。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def ensure_semantic_inventory(inventory_dir: Path) -> tuple[Path, bool]:
        """确保 inventory 目录中存在 cad_semantic_inventory.csv；旧缓存没有该文件时，从全量 inventory 中筛选语义实体生成。"""
        semantic_path = inventory_dir / SEMANTIC_INVENTORY_FILE
        if semantic_path.exists():
            return (semantic_path, False)
        full_path = inventory_dir / FULL_INVENTORY_FILE
        if not full_path.exists():
            raise FileNotFoundError(full_path)
        temp_path = semantic_path.with_name(f'{semantic_path.name}.{os.getpid()}.tmp')
        semantic_count = 0
        try:
            with full_path.open('r', encoding='utf-8-sig', newline='') as source, temp_path.open('w', encoding='utf-8-sig', newline='') as target:
                reader = csv.DictReader(source)
                fieldnames = list(reader.fieldnames or [])
                writer = csv.DictWriter(target, fieldnames=fieldnames)
                writer.writeheader()
                for row in reader:
                    if str(row.get('entity_type', '')).upper() in SEMANTIC_ENTITY_TYPES:
                        writer.writerow(row)
                        semantic_count += 1
            temp_path.replace(semantic_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        manifest_path = inventory_dir / 'inventory_manifest.json'
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            manifest.setdefault('counts', {})['semantic_inventory_objects'] = semantic_count
            manifest.setdefault('output_files', {})['cad_semantic_inventory'] = str(semantic_path.resolve())
            write_json(manifest_path, manifest)
        return (semantic_path, True)

    def safe_float(value: Any) -> float | None:
        """将输入安全转换为有限浮点数；无法转换或为 NaN/inf 时返回 None。"""
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except Exception:
            return None

    def row_bbox(row: dict[str, Any]) -> tuple[float, float, float, float] | None:
        """从 inventory 行中读取 bbox_minx/bbox_miny/bbox_maxx/bbox_maxy 并返回有效 bbox。"""
        values = [safe_float(row.get(key)) for key in ('bbox_minx', 'bbox_miny', 'bbox_maxx', 'bbox_maxy')]
        if any((value is None for value in values)):
            return None
        minx, miny, maxx, maxy = (float(value) for value in values)
        return (minx, miny, maxx, maxy) if maxx >= minx and maxy >= miny else None

    def sheet_bbox_from_keys(sheet: dict[str, Any], prefix: str) -> tuple[float, float, float, float] | None:
        """从 sheet 字典的指定前缀字段中读取 bbox，例如 inspection_region_minx。"""
        values = [safe_float(sheet.get(f'{prefix}_minx')), safe_float(sheet.get(f'{prefix}_miny')), safe_float(sheet.get(f'{prefix}_maxx')), safe_float(sheet.get(f'{prefix}_maxy'))]
        if any((value is None for value in values)):
            return None
        minx, miny, maxx, maxy = (float(value) for value in values)
        return (minx, miny, maxx, maxy) if maxx > minx and maxy > miny else None

    def sheet_bbox(sheet: dict[str, Any]) -> tuple[float, float, float, float] | None:
        """优先读取 sheet 的 inspection_region_bbox；缺失时回退到 inspection_region_* 或 sheet bbox。"""
        raw = sheet.get('inspection_region_bbox')
        if isinstance(raw, list) and len(raw) == 4:
            values = [safe_float(value) for value in raw]
            if not any((value is None for value in values)):
                minx, miny, maxx, maxy = (float(value) for value in values)
                if maxx > minx and maxy > miny:
                    return (minx, miny, maxx, maxy)
        keyed = sheet_bbox_from_keys(sheet, 'inspection_region')
        if keyed:
            return keyed
        raw = sheet.get('bbox')
        if isinstance(raw, list) and len(raw) == 4:
            values = [safe_float(value) for value in raw]
        else:
            values = [safe_float(sheet.get(key)) for key in ('bbox_minx', 'bbox_miny', 'bbox_maxx', 'bbox_maxy')]
        if any((value is None for value in values)):
            return None
        minx, miny, maxx, maxy = (float(value) for value in values)
        return (minx, miny, maxx, maxy) if maxx > minx and maxy > miny else None

    def region_id_safe(value: Any, default: str) -> str:
        """将区域编号清洗成适合作为目录名 / 文件名的一段安全字符串。"""
        text = str(value or default).strip() or default
        text = re.sub('[^0-9A-Za-z_\\-\\u4e00-\\u9fff]+', '_', text).strip('_')
        return text or default

    def sheet_region_entries(sheet: dict[str, Any]) -> list[dict[str, Any]]:
        """从单个 sheet 中提取 inspection_regions；如果不存在多区域结果，则回退为单个 sheet bbox 区域。"""
        raw_regions = sheet.get('inspection_regions')
        entries: list[dict[str, Any]] = []
        if isinstance(raw_regions, list):
            for index, region in enumerate(raw_regions, start=1):
                if not isinstance(region, dict):
                    continue
                raw_bbox = region.get('bbox')
                if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
                    continue
                values = [safe_float(value) for value in raw_bbox]
                if any((value is None for value in values)):
                    continue
                minx, miny, maxx, maxy = (float(value) for value in values)
                if maxx <= minx or maxy <= miny:
                    continue
                entries.append({'region_id': region_id_safe(region.get('region_id'), f'R{index:02d}'), 'bbox': (minx, miny, maxx, maxy), 'source': str(region.get('source') or sheet.get('inspection_region_source') or 'inspection_region'), 'confidence': safe_float(region.get('confidence')) or safe_float(sheet.get('inspection_region_confidence')) or 0.0, 'evidence': str(region.get('evidence') or sheet.get('inspection_region_evidence') or '')})
        if entries:
            return entries
        bbox = sheet_bbox(sheet)
        if not bbox:
            return []
        return [{'region_id': 'R01', 'bbox': bbox, 'source': str(sheet.get('inspection_region_source') or 'sheet_bbox'), 'confidence': safe_float(sheet.get('inspection_region_confidence')) or 0.0, 'evidence': str(sheet.get('inspection_region_evidence') or '')}]

    def intersection_area(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
        """计算两个 bbox 的交集面积。"""
        width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
        height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
        return width * height

    def assign_region(bbox: tuple[float, float, float, float], regions: list[dict[str, Any]]) -> dict[str, Any] | None:
        """根据对象 bbox 中心点和交叠比例，将对象分配到最合适的 inspection region。"""
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        containing = [region for region in regions if region['_bbox'][0] <= cx <= region['_bbox'][2] and region['_bbox'][1] <= cy <= region['_bbox'][3]]
        if containing:
            return min(containing, key=lambda item: item['_area'])
        object_area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 1.0)
        ranked = [(intersection_area(bbox, region['_bbox']) / object_area, region) for region in regions]
        if not ranked:
            return None
        ratio, region = max(ranked, key=lambda item: item[0])
        return region if ratio >= 0.5 else None

    def meaningful(value: Any) -> str:
        """过滤空值和通用 CAD 噪声词，返回有业务意义的文本。"""
        text = str(value or '').strip()
        return '' if normalize_value(text) in NORMALIZED_GENERIC_TERMS else text

    def contains_any_marker(value: Any, markers: tuple[str, ...] | set[str]) -> bool:
        """判断文本是否包含任一标记词，兼容原文、大小写和归一化文本。"""
        text = str(value or '')
        if not text:
            return False
        upper_text = text.upper()
        compact_text = normalize_value(text)
        for marker in markers:
            raw_marker = str(marker or '')
            if not raw_marker:
                continue
            if raw_marker in text or raw_marker.upper() in upper_text:
                return True
            marker_compact = normalize_value(raw_marker)
            if marker_compact and marker_compact in compact_text:
                return True
        return False

    def has_mep_object_semantic(term: Any, context: Any='') -> bool:
        """判断候选是否像水、电、气、暖通、通风、给排水相关房间或设施。"""
        term_text = str(term or '')
        context_text = f"{term_text} {context or ''}"
        return contains_any_marker(context_text, MEP_OBJECT_MARKERS) and contains_any_marker(term_text, OBJECT_NOUN_MARKERS)
    GENERIC_STANDALONE_OBJECT_TERMS = {normalize_value('机房'), normalize_value('房间'), normalize_value('房'), normalize_value('室'), normalize_value('间'), normalize_value('厅')}

    def is_generic_standalone_object_term(term: Any) -> bool:
        """过滤“机房”这类脱离前缀后过于宽泛的房间泛词。"""
        return normalize_value(term) in GENERIC_STANDALONE_OBJECT_TERMS

    def is_fm_exit_code(term: Any) -> bool:
        """FM甲/乙/数字通常是防火门编号，巡检语义上按安全出口处理。"""
        compact = normalize_value(term).upper()
        return bool(re.fullmatch('FM[甲乙丙丁]?\\d{1,8}(?:[-_]\\d+)?', compact))

    def is_code_like_display_term(term: Any) -> bool:
        """判断是否为设备/门窗编号；编号不适合作为最终标注名。"""
        compact = normalize_value(term).upper()
        if not compact:
            return True
        if is_fm_exit_code(compact):
            return True
        return bool(re.fullmatch('[A-Z]{1,8}[甲乙丙丁]?\\d{1,8}(?:[-_]\\d+)?', compact) or re.fullmatch('\\d+(?:[A-Z]+)?', compact))

    def preferred_display_class_name(item: dict[str, Any]) -> str:
        """最终展示优先保留 CAD 里的具体对象名，避免油烟井被泛化成风机房。"""
        class_name = str(item.get('class_name') or item.get('term') or '疑似巡检对象').strip()
        term = str(item.get('term') or '').strip()
        if term and re.search('[\\u4e00-\\u9fff]', term) and (not is_generic_standalone_object_term(term)) and (not is_code_like_display_term(term)):
            return term
        return class_name

    def canonical_class_name(value: Any) -> str:
        """将输入对象名映射为规则库中的标准巡检对象名称；无法匹配时返回原文本。"""
        text = str(value or '').strip()
        if not text:
            return ''
        matched = match_inspection_object([text])
        return str(matched.get('canonical') or text).strip() if matched else text

    def has_text_annotation_evidence(candidate: dict[str, Any]) -> bool:
        """判断候选是否来自 TEXT / MTEXT / ATTRIB 等文本注记证据。"""
        entity_type = str(candidate.get('entity_type', '') or '').upper()
        source_type = str(candidate.get('source_type', '') or '').lower()
        return source_type == 'text' and any((item in entity_type for item in ('TEXT', 'MTEXT', 'ATTRIB')))

    def has_block_evidence(candidate: dict[str, Any]) -> bool:
        """判断候选是否来自 INSERT 块参照证据。"""
        entity_type = str(candidate.get('entity_type', '') or '').upper()
        source_type = str(candidate.get('source_type', '') or '').lower()
        return source_type == 'block' and 'INSERT' in entity_type

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
        context_text = ' '.join((str(row.get(field, '') or '') for field in ('term', 'layer', 'parent_block_name', 'entity_type', 'geometry_kind')))
        context = normalize_value(f'{term} {context_text}')

        def build(class_name: str, reason: str, confidence: float=0.78) -> dict[str, Any] | None:
            if not evidence_allows_class(class_name, row):
                return None
            return {'role': 'inspection_object', 'class_name': class_name, 'confidence': confidence, 'reason': reason, 'stage': 'short_word_rule'}
        exact_text_room_rules = {normalize_value('强电'): '强电间', normalize_value('弱电'): '弱电间', normalize_value('强弱电'): '强弱电间', normalize_value('电井'): '电井', normalize_value('消控'): '消防控制室/消控室', normalize_value('消防控制'): '消防控制室/消控室', normalize_value('消防泵'): '消防水泵', normalize_value('水泵'): '消防水泵'}
        if compact in exact_text_room_rules:
            return build(exact_text_room_rules[compact], 'short_exact_text_label', 0.86)
        if '楼梯' in term or '扶梯' in term or '疏散楼梯' in term:
            return None
        if '消防电梯' in term or ('电梯' in term and contains_any_marker(context, ('消防', 'ELEV', 'FIRE'))):
            return build('消防电梯', 'short_elevator_label', 0.82)
        room_markers = ('房', '室', '间', '所')
        if contains_any_marker(term, room_markers):
            if contains_any_marker(context, ('变配电', '高压配电', '低压配电')):
                return build('变配电房', 'short_room_context_power', 0.82)
            if contains_any_marker(context, ('配电', '强电', '弱电')):
                return build('配电房', 'short_room_context_power', 0.8)
            if contains_any_marker(context, ('消控', '消防控制', '报警主机', '控制室')):
                return build('消防控制室/消控室', 'short_room_context_control', 0.82)
            if contains_any_marker(context, ('消防水泵', '消防泵', '水泵', '泵房', 'PUMP')):
                return build('消防水泵房/消防泵房', 'short_room_context_pump', 0.82)
            if contains_any_marker(context, ('风机', '送风', '排风', '暖通', 'FAN')):
                return build('风机房', 'short_room_context_fan', 0.78)
            if contains_any_marker(context, ('发电机', '柴油', 'GENERATOR')):
                return build('备用发电机/柴油发电机房', 'short_room_context_generator', 0.8)
        if '电井' in term or (compact == normalize_value('井') and contains_any_marker(context, ('强电', '弱电', '电气', '配电'))):
            return build('电井', 'short_well_context', 0.8)
        if ('泵' in term or compact == normalize_value('泵')) and contains_any_marker(context, ('消防', '消火', '喷淋', '水泵', '给水', 'PUMP')):
            class_name = '消防水泵房/消防泵房' if contains_any_marker(context, room_markers) else '消防水泵'
            return build(class_name, 'short_pump_context', 0.78)
        if '阀' in term and contains_any_marker(context, ('消防', '报警', '喷淋', '水', 'VALVE')):
            return build('报警阀组', 'short_valve_context', 0.76)
        if '梯' in term and contains_any_marker(context, ('消防', '电梯', 'ELEV', 'LIFT')):
            return build('消防电梯', 'short_lift_context', 0.76)
        return None

    def candidate_term(row: dict[str, str]) -> tuple[str, str]:
        """从 inventory 行中提取候选语义词：文本实体取 norm_text，INSERT 取 parent_block_name。"""
        entity_type = str(row.get('entity_type', '') or '').upper()
        norm_text = meaningful(row.get('norm_text'))
        if norm_text and entity_type in {'TEXT', 'MTEXT', 'ATTRIB'}:
            return (norm_text, 'text')
        block = meaningful(row.get('parent_block_name'))
        if block and entity_type == 'INSERT':
            return (block, 'block')
        return ('', 'none')

    def is_local_noise(term: str, source_type: str, row: dict[str, str]) -> tuple[bool, str]:
        """在进入本地规则和 LLM 前过滤图纸编号、尺寸、楼层码、标题栏、说明文字等非对象噪声。"""
        compact = normalize_value(term)
        if not compact:
            return (True, 'empty_term')
        if is_generic_standalone_object_term(term):
            return (True, 'generic_standalone_object_term')
        if is_non_object_explanatory_text(term):
            return (True, 'explanatory_text')
        if len(compact) == 1 and (not re.search('[井泵阀梯]', compact)):
            return (True, 'single_character')
        if source_type == 'layer' and NOISE_PATTERN.fullmatch(term.strip()):
            return (True, 'layer_code_only')
        if source_type == 'text' and NOISE_PATTERN.fullmatch(term.strip()) and (not is_protected_inspection_term(term)):
            return (True, 'dimension_or_number')
        if source_type == 'text' and MEASUREMENT_OR_FLOOR_RANGE_PATTERN.search(term) and (not is_protected_inspection_term(term)):
            return (True, 'measurement_or_floor_range')
        if source_type in {'text', 'block'} and FLOOR_CODE_ONLY_PATTERN.fullmatch(term.strip()) and (not is_protected_inspection_term(term)):
            return (True, 'floor_code_only')
        if source_type == 'text':
            parent_block = str(row.get('parent_block_name', '') or '')
            evidence_text = f'{term} {parent_block}'
            has_inspection_hint = any((hint in evidence_text for hint in INSPECTION_HINTS)) or contains_any_marker(evidence_text, INSPECTION_SEMANTIC_HINTS)
            mep_like = has_mep_object_semantic(term, evidence_text)
            if contains_any_marker(parent_block, TITLE_BLOCK_PARENT_MARKERS):
                return (True, 'title_block_text')
            if contains_any_marker(parent_block, EXPLICIT_DRAWING_NOISE_TERMS) and (not has_inspection_hint) and (not mep_like):
                return (True, 'drawing_block_noise')
            if contains_any_marker(term, DISCIPLINE_TITLE_TERMS) and (not is_protected_inspection_term(term)) and (not mep_like):
                return (True, 'discipline_title_text')
            if not has_inspection_hint and contains_any_marker(term, EXPLICIT_DRAWING_NOISE_TERMS):
                return (True, 'drawing_note_or_title')
            if not has_inspection_hint and any((marker in term for marker in HARD_EXCLUSION_TERMS)):
                return (True, 'drawing_note_or_title')
            if not has_inspection_hint and len(compact) > 48:
                return (True, 'long_explanatory_text')
            ascii_code = re.fullmatch('([A-Z]{1,8})[-_]?\\d{1,6}(?:\\([^)]*\\))?', compact, flags=re.I)
            if ascii_code and ascii_code.group(1).upper() not in PROTECTED_CODE_PREFIXES and (not is_protected_inspection_term(term)):
                return (False, '')
        if source_type == 'block':
            block_text = f"{term} {row.get('parent_block_name', '')}"
            if contains_any_marker(block_text, TITLE_BLOCK_PARENT_MARKERS):
                return (True, 'title_block_insert')
            if contains_any_marker(block_text, EXPLICIT_DRAWING_NOISE_TERMS) and (not is_protected_inspection_term(term)):
                return (True, 'drawing_symbol_block')
        if str(row.get('entity_type', '')).upper() in {'DIMENSION', 'LEADER', 'MLEADER'}:
            return (True, 'annotation_entity')
        return (False, '')

    def rule_decision(term: str, row: dict[str, str]) -> dict[str, Any] | None:
        """使用本地巡检对象别名库、关键词模式和短词规则，对候选词进行确定性识别。"""
        context_values = [term, *(row.get(field, '') for field in CONTEXT_FIELDS)]
        if is_fm_exit_code(term):
            return {'role': 'inspection_object', 'class_name': '安全出口', 'confidence': 0.9, 'reason': 'fm_fire_door_code_as_exit', 'stage': 'code_rule'}
        if is_generic_standalone_object_term(term):
            return None
        matched = match_inspection_object([term], context_values=context_values)
        if matched:
            class_name = str(matched.get('canonical') or term)
            if not evidence_allows_class(class_name, row):
                return None
            return {'role': 'inspection_object', 'class_name': class_name, 'confidence': float(matched.get('confidence') or 1.0), 'reason': str(matched.get('reason') or 'inspection_alias_rule'), 'stage': 'alias_rule'}
        pattern = match_inspection_keyword_pattern([term], context_values=context_values)
        if pattern:
            class_name = str(pattern.get('canonical') or term)
            if not evidence_allows_class(class_name, row):
                return None
            return {'role': 'inspection_object', 'class_name': class_name, 'confidence': float(pattern.get('confidence') or 0.88), 'reason': str(pattern.get('reason') or 'inspection_keyword_pattern'), 'stage': 'keyword_rule'}
        short_match = short_word_decision(term, row)
        if short_match:
            return short_match
        return None

    def preview_shape(row: dict[str, str], bbox: tuple[float, float, float, float], region_bbox: tuple[float, float, float, float]) -> list[Any] | None:
        """将区域内非文本几何对象压缩为 0-1 归一化预览形状，用于前端或人工审查展示。"""
        entity_type = str(row.get('entity_type', '') or '').upper()
        geometry = str(row.get('geometry_kind', '') or '').lower()
        if entity_type in {'TEXT', 'MTEXT', 'ATTRIB', 'DIMENSION', 'HATCH'}:
            return None
        width = max(region_bbox[2] - region_bbox[0], 1.0)
        height = max(region_bbox[3] - region_bbox[1], 1.0)
        if (bbox[2] - bbox[0]) / width > 0.65 or (bbox[3] - bbox[1]) / height > 0.65:
            return None
        coords = [max(0.0, min(1.0, (bbox[0] - region_bbox[0]) / width)), max(0.0, min(1.0, (bbox[1] - region_bbox[1]) / height)), max(0.0, min(1.0, (bbox[2] - region_bbox[0]) / width)), max(0.0, min(1.0, (bbox[3] - region_bbox[1]) / height))]
        if entity_type in {'CIRCLE', 'ARC', 'ELLIPSE'}:
            kind = 'curve'
        elif entity_type == 'INSERT' or geometry == 'block_insert':
            kind = 'block'
        elif str(row.get('is_closed', '')).lower() in {'1', 'true'}:
            kind = 'closed'
        else:
            kind = 'line'
        return [kind, *(round(value, 5) for value in coords)]

    def load_regions(sheets_path: Path) -> list[dict[str, Any]]:
        """读取图幅楼层识别结果 JSON，筛选可用于路径规划的楼层区域，并构造内部 region 结构。"""
        payload = read_json(sheets_path)
        regions: list[dict[str, Any]] = []
        for index, sheet in enumerate(payload.get('sheets', []), start=1):
            confidence = safe_float(sheet.get('floor_confidence')) or 0.0
            usable = sheet.get('path_planning_usable')
            if usable is None:
                usable = sheet.get('sheet_role') in {'floor_plan_candidate', 'path_planning_floor_plan'}
            region_entries = sheet_region_entries(sheet)
            if not usable or confidence <= 0.0 or (not region_entries):
                continue
            sheet_id = str(sheet.get('sheet_id') or f'SHEET_{index:03d}')
            for region_entry in region_entries:
                bbox = region_entry['bbox']
                child_region_id = region_entry['region_id']
                child_sheet_id = sheet_id if len(region_entries) == 1 else f'{sheet_id}_{child_region_id}'
                display_name = str(sheet.get('floor_name') or sheet.get('floor_id') or sheet_id)
                if len(region_entries) > 1:
                    display_name = f'{display_name} · {child_region_id}'
                regions.append({'sheet_id': child_sheet_id, 'parent_sheet_id': sheet_id, 'inspection_region_id': child_region_id, 'floor_id': str(sheet.get('floor_id') or 'UNKNOWN'), 'floor_name': str(sheet.get('floor_name') or sheet.get('floor_id') or '未知楼层'), 'display_name': display_name, 'sheet_title': str(sheet.get('sheet_title') or ''), 'confidence': confidence, 'method': str(sheet.get('method') or ''), 'evidence': str(sheet.get('evidence') or ''), 'region_source': region_entry['source'], 'region_confidence': region_entry['confidence'] or confidence, 'region_evidence': region_entry['evidence'], 'bbox': {'minx': bbox[0], 'miny': bbox[1], 'maxx': bbox[2], 'maxy': bbox[3]}, '_bbox': bbox, '_area': (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 'object_count': 0, 'type_keys': set(), 'candidate_groups': {}, 'preview_cells': defaultdict(list)})
        duplicate_ids = Counter((region['floor_id'] for region in regions))
        for region in regions:
            if duplicate_ids[region['floor_id']] > 1:
                suffix = region.get('inspection_region_id') or region['sheet_id']
                region['display_name'] = f"{region['floor_name']} · {region['sheet_title'] or region['parent_sheet_id']} · {suffix}"
        return regions

    def call_llm(llm_script: Path, input_path: Path, output_path: Path, *, no_llm: bool) -> dict[str, Any]:
        """调用外部 LLM 分类脚本；LLM 调用失败时自动降级为 --no-llm 本地兜底模式。"""
        command = [sys.executable, str(llm_script), '--input', str(input_path), '--output', str(output_path), '--save-debug']
        if no_llm:
            command.append('--no-llm')
        proc = subprocess.run(command, cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding='utf-8', errors='replace')
        if proc.returncode != 0 and (not no_llm):
            fallback = subprocess.run([*command, '--no-llm'], cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding='utf-8', errors='replace')
            if fallback.returncode != 0:
                raise RuntimeError((proc.stdout or '') + (proc.stderr or '') + (fallback.stdout or '') + (fallback.stderr or ''))
            result = read_json(output_path)
            result['pipeline_warning'] = 'LLM unavailable; local-rule fallback was used.'
            return result
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout or '') + (proc.stderr or ''))
        return read_json(output_path)

    def llm_decision_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """将 LLM 输出转换为按归一化 term 索引的决策字典。"""
        decisions: dict[str, dict[str, Any]] = {}
        for item in payload.get('inspection_decisions', []) or []:
            if not isinstance(item, dict):
                continue
            term = str(item.get('original_object_name') or item.get('term') or '').strip()
            if term:
                standard_name = str(item.get('standard_class_name') or item.get('class_name') or '').strip()
                decisions[normalize_value(term)] = {**item, 'term': term, 'original_object_name': term, 'class_name': standard_name, 'standard_class_name': standard_name}
        for term in payload.get('inspection_object', []) or []:
            key = normalize_value(term)
            matched = match_standard_class(term)
            decisions.setdefault(key, {'term': term, 'original_object_name': term, 'role': 'inspection_object', 'class_name': matched.standard_class_name, 'standard_class_name': matched.standard_class_name, 'confidence': max(0.6, matched.score), 'reason': 'legacy_llm_list_hybrid_class_mapping'})
        return decisions

    def aggregate_rows(decisions: list[dict[str, Any]], sheet_id: str) -> list[dict[str, Any]]:
        """按巡检对象标准名称聚合识别结果，生成区域级或全局 catalog_rows。"""
        groups: dict[str, dict[str, Any]] = {}
        for item in decisions:
            if item.get('role') != 'inspection_object':
                continue
            name = str(item.get('original_object_name') or item.get('term') or item.get('display_class_name') or preferred_display_class_name(item)).strip()
            group = groups.setdefault(name, {'semantic_name': name, 'standard_class_names': Counter(), 'count': 0, 'layers': Counter(), 'blocks': Counter(), 'entities': Counter(), 'geometries': Counter(), 'stages': Counter(), 'confidence': 0.0})
            count = int(item.get('count') or 1)
            group['count'] += count
            standard_name = str(item.get('standard_class_name') or item.get('class_name') or '').strip()
            if standard_name:
                group['standard_class_names'][standard_name] += count
            for key, counter in (('layer', 'layers'), ('parent_block_name', 'blocks'), ('entity_type', 'entities'), ('geometry_kind', 'geometries'), ('stage', 'stages')):
                value = str(item.get(key) or '').strip()
                if value:
                    group[counter][value] += count
            group['confidence'] = max(group['confidence'], float(item.get('confidence') or 0.0))
        rows: list[dict[str, Any]] = []
        for index, group in enumerate(sorted(groups.values(), key=lambda value: (-value['count'], value['semantic_name'])), start=1):
            top = lambda counter, n=3: ' / '.join((key for key, _ in counter.most_common(n)))
            rows.append({'signature_id': f'{sheet_id}:INS_{index:04d}', 'sheet_id': sheet_id, 'semantic_name': group['semantic_name'], 'original_object_name': group['semantic_name'], 'display_name': f"{group['semantic_name']}({group['count']})", 'standard_class_name': top(group['standard_class_names'], 2), 'count': group['count'], 'layer': top(group['layers'], 4), 'parent_block_name': top(group['blocks'], 3), 'entity_type': top(group['entities'], 3), 'geometry_kind': top(group['geometries'], 3), 'role': 'inspection_object', 'proposed_role': 'inspection_object', 'confidence': round(group['confidence'], 3), 'reason': top(group['stages'], 2)})
        return rows

    def run_pipeline(inventory_dir: Path, sheets_json: Path, output_dir: Path, llm_script: Path, *, no_llm: bool=False) -> dict[str, Any]:
        """执行区域清单构建主流程：切分语义 inventory、生成候选、规则识别、LLM 兜底、写出结果。"""
        inventory_path, semantic_inventory_derived = ensure_semantic_inventory(inventory_dir)
        regions = load_regions(sheets_json)
        if not regions:
            raise RuntimeError('No usable floor regions were detected.')
        output_dir.mkdir(parents=True, exist_ok=True)
        handles: dict[str, Any] = {}
        writers: dict[str, csv.DictWriter] = {}
        try:
            with inventory_path.open('r', encoding='utf-8-sig', newline='') as source:
                reader = csv.DictReader(source)
                fieldnames = list(reader.fieldnames or []) + ['sheet_id', 'floor_id', 'floor_name']
                for region in regions:
                    region_dir = output_dir / region['sheet_id']
                    region_dir.mkdir(parents=True, exist_ok=True)
                    handle = (region_dir / SEMANTIC_INVENTORY_FILE).open('w', encoding='utf-8-sig', newline='')
                    handles[region['sheet_id']] = handle
                    writers[region['sheet_id']] = csv.DictWriter(handle, fieldnames=fieldnames)
                    writers[region['sheet_id']].writeheader()
                for row in reader:
                    bbox = row_bbox(row)
                    if not bbox:
                        continue
                    region = assign_region(bbox, regions)
                    if not region:
                        continue
                    region['object_count'] += 1
                    signature = tuple((row.get(key, '') for key in ('layer', 'entity_type', 'geometry_kind', 'color', 'linetype', 'is_closed', 'parent_block_name', 'norm_text')))
                    region['type_keys'].add(signature)
                    writers[region['sheet_id']].writerow({**row, 'sheet_id': region['sheet_id'], 'floor_id': region['floor_id'], 'floor_name': region['floor_name']})
                    term, source_type = candidate_term(row)
                    if term:
                        key = (normalize_value(term), row.get('layer', ''), row.get('parent_block_name', ''), row.get('entity_type', ''), row.get('geometry_kind', ''))
                        group = region['candidate_groups'].setdefault(key, {'term': term, 'source_type': source_type, 'layer': row.get('layer', ''), 'parent_block_name': row.get('parent_block_name', ''), 'entity_type': row.get('entity_type', ''), 'geometry_kind': row.get('geometry_kind', ''), 'count': 0, 'sample_object_ids': []})
                        if source_type == 'layer':
                            group['count'] = 1
                        else:
                            group['count'] += 1
                        if len(group['sample_object_ids']) < 8:
                            group['sample_object_ids'].append(row.get('object_id', ''))
                    shape = preview_shape(row, bbox, region['_bbox'])
                    if shape:
                        cell = (min(59, int((shape[1] + shape[3]) / 2 * 60)), min(35, int((shape[2] + shape[4]) / 2 * 36)))
                        if len(region['preview_cells'][cell]) < 3:
                            region['preview_cells'][cell].append(shape)
        finally:
            for handle in handles.values():
                handle.close()
        uncertain_by_term: dict[str, dict[str, Any]] = {}
        region_decisions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        local_counts = Counter()
        candidate_group_count = 0
        uncertain_candidate_group_count = 0
        rule_cache: dict[tuple[str, ...], dict[str, Any] | None] = {}
        for region in regions:
            candidates = list(region['candidate_groups'].values())
            candidate_group_count += len(candidates)
            for candidate in candidates:
                rule_key = tuple((normalize_value(candidate.get(field, '')) for field in ('term', *CONTEXT_FIELDS)))
                if rule_key not in rule_cache:
                    rule_cache[rule_key] = rule_decision(candidate['term'], candidate)
                direct = rule_cache[rule_key]
                if direct:
                    candidate['_direct_rule'] = True
                    decision = {**candidate, **direct}
                    decision['original_object_name'] = str(candidate.get('term') or '').strip()
                    decision['standard_class_name'] = str(direct.get('class_name') or '').strip()
                    region_decisions[region['sheet_id']].append(decision)
                    local_counts[direct['stage']] += 1
                    continue
                noise, noise_reason = is_local_noise(candidate['term'], candidate['source_type'], candidate)
                if noise:
                    local_counts[f'noise_after_rules:{noise_reason}'] += 1
                    continue
                uncertain_candidate_group_count += 1
                key = normalize_value(candidate['term'])
                merged = uncertain_by_term.setdefault(key, {'term': candidate['term'], 'source_type': candidate['source_type'], 'source_types': [], 'layer': [], 'parent_block_name': [], 'entity_type': [], 'geometry_kind': [], 'context': []})
                if candidate.get('source_type') and candidate['source_type'] not in merged['source_types']:
                    merged['source_types'].append(candidate['source_type'])
                for field in CONTEXT_FIELDS:
                    value = candidate.get(field, '')
                    if value and value not in merged[field] and (len(merged[field]) < 8):
                        merged[field].append(value)
                merged['context'].append({'sheet_id': region['sheet_id'], 'floor_id': region['floor_id'], 'count': candidate['count']})
        llm_items = []
        for item in uncertain_by_term.values():
            if 'text' not in item.get('source_types', []):
                continue
            llm_items.append({'term': item['term'], 'source_type': ' / '.join(item.get('source_types', []) or [item['source_type']]), 'layer': ' / '.join(item['layer']), 'parent_block_name': ' / '.join(item['parent_block_name']), 'entity_type': ' / '.join(item['entity_type']), 'geometry_kind': ' / '.join(item['geometry_kind']), 'reason': 'uncertain_text_alias_after_local_rules'})
        llm_input_path = output_dir / LLM_INPUT_FILE
        llm_output_path = output_dir / LLM_OUTPUT_FILE
        write_json(llm_input_path, {'uncertain_inspection_candidates': llm_items})
        if llm_items and no_llm:
            llm_payload = {'inspection_object': [], 'IGNORE': [item['term'] for item in llm_items], 'inspection_decisions': [], 'model': '', 'counts': {'inspection_object': 0, 'IGNORE': len(llm_items)}, 'pipeline_warning': 'LLM disabled; uncertain candidates were kept as IGNORE.'}
            write_json(llm_output_path, llm_payload)
        elif llm_items:
            llm_payload = call_llm(llm_script, llm_input_path, llm_output_path, no_llm=no_llm)
        else:
            llm_payload = {'inspection_object': [], 'IGNORE': [], 'inspection_decisions': [], 'model': '', 'counts': {}}
            write_json(llm_output_path, llm_payload)
        llm_map = llm_decision_map(llm_payload)
        for region in regions:
            for candidate in region['candidate_groups'].values():
                if candidate.get('_direct_rule'):
                    continue
                llm_item = llm_map.get(normalize_value(candidate['term']))
                if not llm_item:
                    continue
                role = str(llm_item.get('role') or 'inspection_object')
                if role != 'inspection_object':
                    continue
                proposed_class_name = str(llm_item.get('standard_class_name') or llm_item.get('class_name') or '').strip()
                canonical_names = set(inspection_canonical_names())
                if proposed_class_name in canonical_names:
                    class_name = proposed_class_name
                    fuzzy_match = None
                else:
                    fuzzy_match = match_standard_class(candidate['term'], proposed_class_name=proposed_class_name, possible_alias=llm_item.get('possible_alias', ''))
                    class_name = fuzzy_match.standard_class_name
                if not evidence_allows_class(class_name, candidate):
                    continue
                reason = str(llm_item.get('reason') or 'llm_fallback')
                if fuzzy_match is not None:
                    reason += ':hybrid_closed_set_mapping'
                region_decisions[region['sheet_id']].append({**candidate, 'role': 'inspection_object', 'class_name': class_name, 'standard_class_name': class_name, 'original_object_name': str(candidate.get('term') or '').strip(), 'confidence': float(llm_item.get('confidence') or 0.0), 'reason': reason, 'stage': 'llm_fallback'})
        floors: list[dict[str, Any]] = []
        combined_decisions: list[dict[str, Any]] = []
        for region in regions:
            decisions = region_decisions[region['sheet_id']]
            combined_decisions.extend(decisions)
            catalog_rows = aggregate_rows(decisions, region['sheet_id'])
            preview_shapes = [shape for cell in sorted(region['preview_cells']) for shape in region['preview_cells'][cell]][:5000]
            region_payload = {key: value for key, value in region.items() if key not in {'_bbox', '_area', 'type_keys', 'candidate_groups', 'preview_cells'}}
            region_payload.update({'type_count': len(region['type_keys']), 'candidate_count': len(region['candidate_groups']), 'inspection_type_count': len(catalog_rows), 'inspection_instance_count': sum((int(row['count']) for row in catalog_rows)), 'catalog_rows': catalog_rows, 'preview_shapes': preview_shapes, 'inventory_csv': str((output_dir / region['sheet_id'] / SEMANTIC_INVENTORY_FILE).resolve())})
            floors.append(region_payload)
            write_json(output_dir / region['sheet_id'] / 'inspection_objects.json', {'catalog_rows': catalog_rows, 'decisions': decisions})
        global_rows = aggregate_rows(combined_decisions, 'ALL')
        result = {'schema_version': SCHEMA_VERSION, 'pipeline': ['full_cad_inventory', 'sheet_floor_region_preprocess', 'region_inventory', 'region_candidate_generation', 'deterministic_rules', 'llm_binary_fallback'], 'inventory_dir': str(inventory_dir.resolve()), 'sheets_json': str(sheets_json.resolve()), 'region_count': len(floors), 'local_rule_counts': dict(local_counts), 'candidate_group_count': candidate_group_count, 'llm_candidate_count': len(llm_items), 'candidate_deduplicated_count': max(0, uncertain_candidate_group_count - len(llm_items)), 'llm_model': llm_payload.get('model', ''), 'llm_warning': llm_payload.get('pipeline_warning', ''), 'semantic_inventory': {'path': str(inventory_path.resolve()), 'derived_from_legacy_cache': semantic_inventory_derived, 'entity_types': sorted(SEMANTIC_ENTITY_TYPES)}, 'catalog_rows': global_rows, 'floors': floors, 'artifacts': {'llm_input': str(llm_input_path.resolve()), 'llm_output': str(llm_output_path.resolve()), 'result_json': str((output_dir / RESULT_FILE).resolve())}}
        write_json(output_dir / RESULT_FILE, result)
        write_json(output_dir / 'regions_manifest.json', {'schema_version': SCHEMA_VERSION, 'regions': [{key: value for key, value in floor.items() if key not in {'catalog_rows', 'preview_shapes'}} for floor in floors]})
        return result

    def call_llm(llm_script, input_path, output_path, *, no_llm):
        classifier = sys.modules["scripts.llm_deepseekv4"]

        def classify(disabled):
            arguments = argparse.Namespace(
                input=str(input_path),
                output=str(output_path),
                save_debug=True,
                no_llm=disabled,
                api_key=os.getenv("DEEPSEEK_API_KEY", ""),
                base_url=os.getenv("DEEPSEEK_BASE_URL", ""),
                model=os.getenv("DEEPSEEK_MODEL", ""),
                batch_size=36,
                max_concurrency=3,
                timeout=90,
                temperature=0.0,
                max_retries=3,
                cache_dir="",
                no_local_classifier=False,
                local_classifier_training_run=[],
                local_negative_threshold=0.01,
                local_positive_threshold=0.99,
            )
            return classifier.classify_items(arguments)

        try:
            return classify(no_llm)
        except Exception:
            if no_llm:
                raise
            result = classify(True)
            result["pipeline_warning"] = "LLM unavailable; local-rule fallback was used."
            return result
    return dict(locals())

_s04_region = _register_embedded_module(
    'scripts.build_region_inspection_inventory',
    _build_s04_region(),
    aliases=('build_region_inspection_inventory',),
)

# -----------------------------------------------------------------------------
# Migrated implementation: web/server.py
# -----------------------------------------------------------------------------
def _build_s04_review_server():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'web/server.py'
    )
    __name__ = 'web.server'
    __package__ = 'web'
    import argparse
    import csv
    import hashlib
    import json
    import mimetypes
    import os
    import re
    import shutil
    import subprocess
    import sys
    import threading
    import time
    import traceback
    import uuid
    from collections import Counter, defaultdict
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from typing import Any
    from urllib.parse import unquote, urlparse
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    WEB_ROOT = Path(__file__).resolve().parent
    RUNTIME_ROOT = WEB_ROOT / 'runtime'
    JOBS_ROOT = RUNTIME_ROOT / 'jobs'
    LOGS_ROOT = RUNTIME_ROOT / 'logs'
    SERVER_LOG_FILE = LOGS_ROOT / 'server.log'
    DEMO_INVENTORY_DIR = PROJECT_ROOT / 'outputs' / 'test_single_floor' / 'inventory'
    INVENTORY_CACHE_ROOT = PROJECT_ROOT / '.cache' / 'inventory'
    PIPELINE_CACHE_ROOT = PROJECT_ROOT / '.cache' / 'pipeline'
    CAD_AGENT_SCRIPT = PROJECT_ROOT / 'agents' / 'cad_vector_inventory_agent.py'
    LLM_CLASSIFIER_SCRIPT = PROJECT_ROOT / 'scripts' / 'llm-deepseekv4.py'
    FLOOR_DETECTOR_SCRIPT = PROJECT_ROOT / 'scripts' / 'detect_dxf_sheets_floors.py'
    REGION_PIPELINE_SCRIPT = PROJECT_ROOT / 'scripts' / 'build_region_inspection_inventory.py'
    INSPECTION_LIBRARY_SCRIPT = PROJECT_ROOT / 'agents' / 'inspection_object_library.py'
    INSPECTION_ALIASES_CONFIG = PROJECT_ROOT / 'configs' / 'inspection_object_aliases.json'
    INSPECTION_PATTERNS_CONFIG = PROJECT_ROOT / 'configs' / 'inspection_object_keyword_patterns.json'
    REGION_RESULT_FILE = 'region_inspection_results.json'
    FLOOR_RESULT_FILE = 'drawing_sheets_floors.json'
    FLOOR_OBJECT_SUMMARY_FILE = 'floor_object_summary.json'
    _CATALOG_DECISION_CACHE: dict[str, dict[tuple[str, ...], tuple[str, str, str]]] = {}
    JOB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='fire-route-job')
    JOB_STATE_LOCK = threading.Lock()
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from agents.cad_vector_inventory_agent import run_inventory
    except Exception as exc:
        run_inventory = None
        INVENTORY_IMPORT_ERROR = repr(exc)
    else:
        INVENTORY_IMPORT_ERROR = ''
    from agents.inspection_object_library import inspection_canonical_names, is_non_object_explanatory_text, match_inspection_keyword_pattern, match_inspection_object
    ROLE_OPTIONS = [{'value': 'inspection_object', 'label': '参与巡检', 'tone': 'inspection'}, {'value': 'not_route_related', 'label': '已排除', 'tone': 'muted'}]
    INSPECTION_KEYWORDS = ['消防泵房', '消防控制', '消控', '消防器材', '消火栓', '消防电源', '消防', '配电', '强电', '弱电', '变电', '电源', '电气火灾', '报警', 'FAS', 'IBP', '应急', '巡检', '监控', '通信', '信号', '环控', '风机房', '排烟机房', '防烟机房', '通风空调机房', '设备房', '设备间', '水泵', '泵控制', '稳压', '潜污泵']
    PASSABLE_KEYWORDS = ['安全出口', '疏散楼梯', '消防楼梯', '楼梯', '走道', '通道', '连通', '出入口', '出口', '入口', '坡道', '前室', '电梯厅', '门洞', '门口', 'DOOR', 'OPEN', 'STAIR', 'CORRIDOR']
    OBSTACLE_KEYWORDS = ['墙', '墙体', '剪力墙', '结构墙', '砼墙', '砖墙', '隔墙', '柱', '柱子', '结构柱', '构造柱', 'WALL', 'A-WALL', 'COLUMN', 'COLU', 'PILLAR']
    IGNORE_HINTS = ['DIM', 'AXIS', 'TEXT', '标注', '轴线', '图框', '说明', '编号', 'DATE', 'DRAWING', 'SIGNATURE']



    def read_json(path: Path, fallback: Any=None) -> Any:
        if not path.exists():
            return fallback
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)





    def as_int(value: Any, default: int=0) -> int:
        try:
            return int(float(str(value or '0')))
        except Exception:
            return default

    def as_float(value: Any) -> float | None:
        try:
            number = float(str(value))
        except Exception:
            return None
        if number != number:
            return None
        return number


    def parse_sample(value: Any) -> str:
        raw = str(value or '').strip()
        if not raw or raw == '[]':
            return ''
        try:
            decoded = json.loads(raw)
        except Exception:
            return raw
        if isinstance(decoded, list):
            return ' / '.join((str(item) for item in decoded if str(item).strip()))
        return str(decoded)

    CLASSIFIED_RESULT_FILE = 'llm_text_terms_for_review_llm_classified_filtered.json'
    BUSINESS_ROLE_ORDER = ['inspection_object']
    ALL_ROLE_ORDER = ['inspection_object', 'not_route_related']
    INSPECTION_CLASSES = set(inspection_canonical_names())
    SAFETY_EXIT_TERMS = ['安全出口', '疏散出口', '安全疏散出口']
    INSPECTION_FORCE_TERMS = ['安全出口', '疏散出口', '强弱电', '强电', '弱电', '配电', '消防泵', '消防控制', '消控', '消防电源', '消防器材', '消火栓', '报警主机', 'FAS', 'IBP', '应急箱', '巡检柜', '电气火灾', '智能照明', '智能疏散', '通信设备', '信号设备', '环控', '风机房', '排烟机房', '防烟机房', '设备房', '设备室', '设备间', '主机柜', '水泵']
    PASSABLE_FORCE_TERMS = ['楼梯', '疏散楼梯', '消防楼梯', '走道', '通道', '连通道', '出入口', '坡道', '前室', '电梯厅', '门洞', '门口', '扶梯', 'STAIR', 'EVAC_PATH', 'DOOR', 'WALL_OPEN']
    STRUCTURAL_OBSTACLE_TERMS = ['WALL', 'A-WALL', 'COLUMN', 'COLU', 'COL', 'PILLAR', '墙', '墙体', '剪力墙', '结构墙', '砼墙', '砖墙', '隔墙', '柱', '柱子', '结构柱', '构造柱']
    NON_STRUCTURAL_OBSTACLE_TERMS = ['电梯', '扶梯', '楼梯', '电梯厅', '风井', '排烟井', '排风井', '新风井', '集水坑', '集水井', '水沟', '排水沟', '截水沟', '防火卷帘', '卷帘', '孔洞', '洞口', '留洞', '门窗洞', 'HATCH', '填充', '剖面线', '坑位', '设备', '机房']
    STRICT_NON_STRUCTURAL_OBSTACLE_TERMS = ['HATCH', '填充', '剖面线', '开洞', '留洞', '孔洞', '洞口', '门窗洞', '套管']
    ANNOTATION_TERMS = ['DIM', 'AXIS', 'TEXT', '标注', '轴线', '图框', '说明', '编号', 'LABEL']
    NON_OBJECT_TEXT_TERMS = ['未注明', '详见', '参见', '说明', '门槛', '标高', '尺寸', '编号', '图纸', '施工', 'MM']
    ASCII_NAME_HINTS = ['WALL', 'COLUMN', 'DOOR', 'STAIR', 'PIPE', 'EQUIP', 'HATCH', 'FAS', 'IBP', 'PUMP']

    def classified_result_path(inventory_dir: Path) -> Path:
        return inventory_dir / CLASSIFIED_RESULT_FILE






    def text_has_any(value: Any, terms: list[str]) -> bool:
        text = str(value or '').upper()
        return any((term.upper() in text for term in terms))

    def is_text_like_entity(entity_type: Any, geometry_kind: Any) -> bool:
        entity_tokens = [token.strip().upper() for token in re.split('\\s*/\\s*|\\s*,\\s*', str(entity_type or '')) if token.strip()]
        geometry_tokens = [token.strip().lower() for token in re.split('\\s*/\\s*|\\s*,\\s*', str(geometry_kind or '')) if token.strip()]
        text_entities = {'TEXT', 'MTEXT', 'ATTRIB'}
        text_geometries = {'text', 'classified_text_term'}
        if entity_tokens:
            return all((token in text_entities for token in entity_tokens))
        if geometry_tokens:
            return all((token in text_geometries for token in geometry_tokens))
        return False

    def is_obstacle_geometry_entity(entity_type: Any, geometry_kind: Any) -> bool:
        if is_text_like_entity(entity_type, geometry_kind):
            return False
        combined = f"{entity_type or ''} {geometry_kind or ''}"
        if text_has_any(combined, ['HATCH', 'hatch_area', 'DIMENSION', 'LEADER', 'MLEADER']):
            return False
        return True

    def inspection_match_name(values: list[Any], context_values: list[Any] | None=None) -> tuple[str, str] | None:
        match = match_inspection_object(values, context_values=context_values or [])
        if match and (not match.get('needs_llm')):
            return (str(match.get('canonical') or match.get('matched_alias') or '消防巡检对象'), str(match.get('reason') or 'inspection_library'))
        pattern = match_inspection_keyword_pattern(values, context_values=context_values or [])
        if pattern:
            return (str(pattern.get('canonical') or pattern.get('matched_alias') or '消防巡检对象'), str(pattern.get('reason') or 'inspection_keyword_pattern'))
        return None

    def is_structural_obstacle_candidate(*values: Any) -> bool:
        combined = ' '.join((str(value or '') for value in values if str(value or '').strip()))
        if not combined:
            return False
        if text_has_any(combined, STRICT_NON_STRUCTURAL_OBSTACLE_TERMS):
            return False
        has_structure = text_has_any(combined, STRUCTURAL_OBSTACLE_TERMS)
        if not has_structure:
            return False
        if text_has_any(combined, NON_STRUCTURAL_OBSTACLE_TERMS):
            strong_wall_layer = text_has_any(combined, ['WALL', 'A-WALL', '墙体', '剪力墙', '结构墙'])
            if not strong_wall_layer:
                return False
        return True

    def canonical_name_from_keywords(value: Any, role: str) -> str:
        text = str(value or '')
        if role == 'inspection_object':
            inspection_match = inspection_match_name([text])
            if inspection_match:
                return inspection_match[0]
        checks: dict[str, list[tuple[list[str], str]]] = {'inspection_object': [(SAFETY_EXIT_TERMS, '安全出口'), (['消火栓'], '消火栓/消防设备'), (['消防泵', '水泵', 'PUMP'], '水泵相关对象'), (['消防控制', '消控'], '消防控制室/消控对象'), (['消防电源'], '消防电源监控'), (['消防器材'], '消防器材'), (['报警主机', 'FAS'], '报警主机/FAS'), (['IBP'], 'IBP盘'), (['智能照明'], '智能照明主机'), (['智能疏散'], '智能疏散主机'), (['电气火灾'], '电气火灾监控'), (['应急箱'], '应急箱'), (['巡检柜'], '巡检柜'), (['强弱电'], '强弱电间'), (['强电'], '强电井/强电对象'), (['弱电'], '弱电井/弱电对象'), (['配电'], '配电相关对象'), (['通信设备', '通信'], '通信设备室/通信对象'), (['信号设备', '信号', '通号'], '信号设备室/信号对象'), (['环控'], '环控设备/环控室'), (['排烟机房'], '排烟机房'), (['防烟机房'], '防烟机房'), (['风机房'], '风机房'), (['设备房', '设备室', '设备间'], '设备用房'), (['消防'], '消防设施/设备对象')], 'passable_opening_object': [(['楼梯', 'STAIR', '扶梯'], '楼梯/扶梯'), (['门洞', '门口', 'DOOR', 'WALL_OPEN'], '门洞/门对象'), (['EVAC_PATH'], '疏散路径'), (['走道', '通道', '连通道'], '通道/走道'), (['出入口', '出口', '入口'], '出入口'), (['坡道'], '坡道'), (['前室'], '前室'), (['电梯厅'], '电梯厅')], 'obstacle_object': [(['COLUMN', '柱'], '柱'), (['WALL', '墙'], '墙体')]}
        for terms, name in checks.get(role, []):
            if text_has_any(text, terms):
                return name
        return ''

    def meaningful_name(value: Any) -> str:
        text = str(value or '').strip()
        if not text or text in {'0', '[]'}:
            return ''
        if '$' in text or '底图' in text:
            return ''
        if re.fullmatch('A\\$C[0-9A-F]+|\\*U\\d+|\\*X\\d+|\\$[^\\\\s]+|[A-Za-z0-9_-]{1,4}', text, flags=re.I):
            return ''
        if re.fullmatch('[A-Za-z0-9_$*_. -]+', text):
            upper = text.upper()
            if not any((hint in upper for hint in ASCII_NAME_HINTS)):
                return ''
        return text

    def looks_like_non_object_text(value: Any) -> bool:
        text = str(value or '').strip()
        if not text:
            return False
        if is_non_object_explanatory_text(text):
            return True
        if text_has_any(text, NON_OBJECT_TEXT_TERMS):
            return True
        return len(text) >= 18 and bool(re.search('\\d|[，,。.；;:：、]', text))



    def classified_text_decision_map(classified: dict[str, Any]) -> dict[str, dict[str, Any]]:
        decisions: dict[str, dict[str, Any]] = {}
        raw_decisions = classified.get('inspection_decisions', [])
        if isinstance(raw_decisions, list):
            for item in raw_decisions:
                if not isinstance(item, dict):
                    continue
                term = str(item.get('term', '') or '').strip()
                class_name = str(item.get('class_name', '') or '').strip()
                role = str(item.get('role', '') or '')
                if term and role == 'inspection_object':
                    canonical_match = inspection_match_name([class_name]) if class_name else None
                    if canonical_match:
                        item['class_name'] = canonical_match[0]
                    elif not class_name:
                        item['class_name'] = term
                        item['need_human_review'] = True
                    decisions[term] = item
        terms = classified.get('inspection_object', [])
        if isinstance(terms, list):
            for term_value in terms:
                term = str(term_value).strip()
                if not term or term in decisions:
                    continue
                match = inspection_match_name([term])
                if match:
                    decisions[term] = {'term': term, 'role': 'inspection_object', 'class_name': match[0], 'confidence': 0.9, 'need_human_review': False, 'reason': 'legacy_inspection_object_bucket'}
        return decisions

    def decision_for_value(value: Any, decision_by_term: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        clean = str(value or '').strip()
        if not clean:
            return None
        decision = decision_by_term.get(clean)
        if not decision:
            return None
        class_name = str(decision.get('class_name', '') or '').strip()
        if not class_name:
            return None
        return decision

    def classify_catalog_semantics(row: dict[str, str], decision_by_term: dict[str, dict[str, Any]]) -> tuple[str, str, str]:
        norm_text = str(row.get('norm_text_sample', '') or '').strip()
        raw_text = parse_sample(row.get('raw_text_sample', ''))
        layer = str(row.get('layer', '') or '')
        block = str(row.get('parent_block_name', '') or '')
        entity_type = str(row.get('entity_type', '') or '')
        geometry = str(row.get('geometry_kind', '') or '')
        combined = ' '.join([norm_text, raw_text, layer, block, entity_type, geometry])
        is_text_entity = is_text_like_entity(entity_type, geometry)
        is_obstacle_geometry = is_obstacle_geometry_entity(entity_type, geometry)
        if is_text_entity and norm_text and looks_like_non_object_text(norm_text):
            return ('not_route_related', '普通说明/注释文本', 'non_object_note_text')
        library_name = inspection_match_name([norm_text, raw_text, block, layer], [entity_type, geometry, row.get('source_counter', ''), row.get('block_path_sample', '')])
        if library_name:
            name, reason = library_name
            return ('inspection_object', name, reason)
        for source_value, source_reason in [(norm_text, 'llm_text_second_stage'), (raw_text, 'llm_raw_text_second_stage'), (block, 'llm_block_second_stage'), (layer, 'llm_layer_second_stage')]:
            decision = decision_for_value(source_value, decision_by_term)
            if decision:
                confidence = as_float(decision.get('confidence')) or 0.0
                review_mark = 'review' if decision.get('need_human_review') else 'auto'
                return ('inspection_object', str(decision.get('class_name')), f'{source_reason}:{review_mark}:{confidence:.2f}')
        if is_text_entity:
            return ('not_route_related', norm_text or raw_text or '非巡检文字标注', 'non_inspection_text_context_only')
        if text_has_any(combined, PASSABLE_FORCE_TERMS):
            name = canonical_name_from_keywords(combined, 'passable_opening_object') or meaningful_name(norm_text) or meaningful_name(block) or meaningful_name(layer) or '通行空间对象'
            return ('passable_opening_object', name, 'catalog_passable_keyword')
        if is_obstacle_geometry and is_structural_obstacle_candidate(combined):
            name = canonical_name_from_keywords(combined, 'obstacle_object') or meaningful_name(norm_text) or meaningful_name(block) or meaningful_name(layer) or '墙柱结构障碍'
            return ('obstacle_object', name, 'catalog_structural_obstacle_keyword')
        if is_text_entity:
            return ('not_route_related', '其他文字对象', 'unclassified_text')
        if entity_type == 'INSERT' or geometry == 'block_insert':
            name = meaningful_name(block) or meaningful_name(layer) or '其他块对象'
            return ('passable_opening_object', name, 'unclassified_block_default_passable')
        name = meaningful_name(norm_text) or meaningful_name(block) or meaningful_name(layer) or f"其他{entity_type or geometry or 'CAD'}对象"
        return ('passable_opening_object', name, 'unclassified_non_text_default_passable')

    def parse_json_counter(value: Any) -> dict[str, int]:
        raw = str(value or '').strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, int] = {}
        for key, val in data.items():
            out[str(key)] = as_int(val)
        return out

    def inspection_instance_count(row: dict[str, str], raw_count: int, semantic_name: str='') -> int:
        """Count inspection object instances, not every geometry primitive.

        Text labels and INSERT containers represent object instances. Virtual
        geometry inside blocks, hatches, and outline polylines are supporting
        geometry and should not inflate inspection-object counts.
        """
        entity_type = str(row.get('entity_type', '') or '').upper()
        geometry_kind = str(row.get('geometry_kind', '') or '').lower()
        source_counter = parse_json_counter(row.get('source_counter', ''))
        if geometry_kind in {'classified_text_term', 'classified_inspection_term'}:
            return raw_count
        if entity_type in {'TEXT', 'MTEXT', 'ATTRIB'} or geometry_kind == 'text':
            return raw_count
        if str(semantic_name or '').strip() == '安全出口':
            return 0
        if entity_type == 'INSERT' or geometry_kind == 'block_insert':
            if not source_counter or source_counter.get('insert_container', 0) > 0:
                return raw_count
        return 0









































    JOB_ID_PATTERN = re.compile('^[A-Za-z0-9_-]+$')




    return dict(locals())

_s04_review_server = _register_embedded_module(
    'web.server',
    _build_s04_review_server(),
    aliases=(),
)

# -----------------------------------------------------------------------------
# Migrated implementation: scripts/mark_inspection_objects_dxf.py
# -----------------------------------------------------------------------------
def _build_s04_marker():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'scripts/mark_inspection_objects_dxf.py'
    )
    __name__ = 'scripts.mark_inspection_objects_dxf'
    __package__ = 'scripts'
    import argparse
    import csv
    import json
    import math
    import re
    import sys
    from collections import Counter
    from datetime import datetime
    from pathlib import Path
    from typing import Any
    import ezdxf
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    import web.server as review_server
    JOBS_ROOT = PROJECT_ROOT / 'web' / 'runtime' / 'jobs'
    BOX_LAYER = 'CHECK_INSPECTION_OBJECT_BOX'
    TEXT_LAYER = 'CHECK_INSPECTION_OBJECT_TEXT'
    POINT_LAYER = 'CHECK_INSPECTION_OBJECT_POINT'
    EXCLUSION_LAYER_TERMS = ['不出图范围', '非巡检范围', '不巡检范围', '不标注范围']
    EXCLUSION_NOTE_TERMS = ['阴影部分详见单体图纸']

    def safe_float(value: Any) -> float | None:
        try:
            number = float(value)
        except Exception:
            return None
        return number if math.isfinite(number) else None


    def safe_layer_suffix(text: Any) -> str:
        clean = str(text or '').strip()
        clean = re.sub('[<>/\\\\":;?*|=,`\\[\\]]+', '_', clean)
        return clean[:80] or 'UNKNOWN'





    def bbox_from_inventory(row: dict[str, str]) -> tuple[float, float, float, float] | None:
        minx = safe_float(row.get('bbox_minx'))
        miny = safe_float(row.get('bbox_miny'))
        maxx = safe_float(row.get('bbox_maxx'))
        maxy = safe_float(row.get('bbox_maxy'))
        x = safe_float(row.get('x'))
        y = safe_float(row.get('y'))
        if None in (minx, miny, maxx, maxy) or maxx <= minx or maxy <= miny:
            if x is None or y is None:
                return None
            minx, miny, maxx, maxy = (x - 250.0, y - 250.0, x + 250.0, y + 250.0)
        width = maxx - minx
        height = maxy - miny
        pad = max(180.0, min(800.0, max(width, height) * 0.18))
        if width < 600.0:
            extra = (600.0 - width) / 2.0
            minx -= extra
            maxx += extra
        if height < 360.0:
            extra = (360.0 - height) / 2.0
            miny -= extra
            maxy += extra
        return (minx - pad, miny - pad, maxx + pad, maxy + pad)


    def bbox_area(bbox: tuple[float, float, float, float]) -> float:
        minx, miny, maxx, maxy = bbox
        return max(0.0, maxx - minx) * max(0.0, maxy - miny)

    def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1 = max(ax1, bx1)
        iy1 = max(ay1, by1)
        ix2 = min(ax2, bx2)
        iy2 = min(ay2, by2)
        intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        union = bbox_area(a) + bbox_area(b) - intersection
        return intersection / union if union > 0 else 0.0

    def center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
        minx, miny, maxx, maxy = bbox
        return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)

    def center_distance(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
        ax, ay = center(a)
        bx, by = center(b)
        return math.hypot(ax - bx, ay - by)



    def annotation_priority(item: dict[str, Any]) -> tuple[int, float]:
        entity_type = str(item.get('entity_type', '')).upper()
        geometry_kind = str(item.get('geometry_kind', '')).lower()
        source = str(item.get('source', ''))
        score = 0
        if entity_type == 'INSERT' or geometry_kind == 'block_insert':
            score += 300
        elif entity_type in {'TEXT', 'MTEXT', 'ATTRIB'} or geometry_kind == 'text':
            score += 200
        if source == 'direct_entity':
            score += 30
        elif source == 'insert_container':
            score += 20
        elif source == 'virtual_entity_in_insert':
            score -= 10
        if item.get('reason', '').startswith('inspection_library'):
            score += 10
        return (score, -bbox_area(item['bbox']))

    def annotation_standard_name(item: dict[str, Any]) -> str:
        return str(item.get('standard_class_name') or item.get('class_name') or '巡检对象').strip()

    def is_duplicate_annotation(item: dict[str, Any], kept: dict[str, Any]) -> bool:
        if annotation_standard_name(item) != annotation_standard_name(kept):
            return False
        if bbox_iou(item['bbox'], kept['bbox']) >= 0.55:
            return True
        return center_distance(item['bbox'], kept['bbox']) <= 700.0

    def dedupe_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for item in sorted(annotations, key=annotation_priority, reverse=True):
            if any((is_duplicate_annotation(item, existing) for existing in kept)):
                continue
            kept.append(item)
        return sorted(kept, key=lambda item: (annotation_standard_name(item), center(item['bbox'])[0], center(item['bbox'])[1]))



    def add_layer(doc: Any, name: str, color: int) -> None:
        if name not in doc.layers:
            doc.layers.add(name=name, color=color)
            return
        try:
            doc.layers.get(name).dxf.color = color
        except Exception:
            pass

    def add_label(msp: Any, text: str, point: tuple[float, float], height: float, layer: str, color: int) -> None:
        entity = msp.add_text(text, dxfattribs={'layer': layer, 'color': color, 'height': height})
        try:
            entity.set_placement(point)
        except Exception:
            entity.dxf.insert = point



    def write_marked_dxf(input_dxf: Path, output_dxf: Path, annotations: list[dict[str, Any]]) -> None:
        doc = ezdxf.readfile(input_dxf)
        msp = doc.modelspace()
        add_layer(doc, BOX_LAYER, 1)
        add_layer(doc, TEXT_LAYER, 1)
        add_layer(doc, POINT_LAYER, 1)
        for class_name in sorted({annotation_standard_name(item) for item in annotations}):
            add_layer(doc, 'CHECK_INSP_' + safe_layer_suffix(class_name), 1)
        for index, item in enumerate(annotations, start=1):
            class_name = annotation_standard_name(item)
            minx, miny, maxx, maxy = item['bbox']
            cx, cy = center(item['bbox'])
            width = maxx - minx
            height = maxy - miny
            marker_radius = max(120.0, min(500.0, max(width, height) * 0.08))
            text_height = max(260.0, min(700.0, max(width, height) * 0.12))
            class_layer = 'CHECK_INSP_' + safe_layer_suffix(class_name)
            polyline = msp.add_lwpolyline([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)], close=True, dxfattribs={'layer': class_layer, 'color': 1})
            try:
                polyline.dxf.const_width = max(20.0, min(80.0, marker_radius * 0.12))
            except Exception:
                pass
            msp.add_circle((cx, cy), radius=marker_radius, dxfattribs={'layer': POINT_LAYER, 'color': 1})
            add_label(msp, f'{index:03d} {class_name}', (minx, maxy + text_height * 0.4), text_height, TEXT_LAYER, 1)
        output_dxf.parent.mkdir(parents=True, exist_ok=True)
        doc.saveas(output_dxf)

    def timestamped_output_path(output_dxf: Path) -> Path:
        stamp = datetime.now().strftime('%Y%m%d%H%M%S')
        return output_dxf.with_name(f'{output_dxf.stem}_{stamp}{output_dxf.suffix}')
    return dict(locals())

_s04_marker = _register_embedded_module(
    'scripts.mark_inspection_objects_dxf',
    _build_s04_marker(),
    aliases=('mark_inspection_objects_dxf',),
)

# -----------------------------------------------------------------------------
# Migrated implementation: fire_inspection_system/inspection_object_recognition.py
# -----------------------------------------------------------------------------
def _build_s04_api():
    __file__ = str(
        __import__("pathlib").Path(globals()["__file__"]).resolve().parents[2]
        / 'fire_inspection_system/inspection_object_recognition.py'
    )
    __name__ = 'fire_inspection_system.inspection_object_recognition'
    __package__ = 'fire_inspection_system'
    import csv
    import json
    import sys
    from collections import Counter
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
    SCRIPTS_DIR = PROJECT_ROOT / 'scripts'
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from agents.inspection_class_fuzzy_matcher import standard_class_names
    from build_region_inspection_inventory import normalize_value, run_pipeline
    from mark_inspection_objects_dxf import bbox_from_inventory, dedupe_annotations, timestamped_output_path, write_marked_dxf
    DEFAULT_LLM_SCRIPT = PROJECT_ROOT / 'scripts' / 'llm-deepseekv4.py'
    STANDARD_CLASS_NAMES = frozenset(standard_class_names())

    @dataclass(frozen=True)
    class InspectionRecognitionResult:
        result_json: Path
        regions_manifest: Path
        marked_dxf: Path | None
        marked_report_json: Path | None
        region_count: int
        inspection_type_count: int
        inspection_instance_count: int
        llm_candidate_count: int
        llm_model: str

    def read_json(path: Path) -> dict[str, Any]:
        with path.open('r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}

    def write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def candidate_term(row: dict[str, str]) -> tuple[str, str]:
        entity_type = str(row.get('entity_type', '') or '').upper()
        norm_text = str(row.get('norm_text', '') or '').strip()
        if norm_text and entity_type in {'TEXT', 'MTEXT', 'ATTRIB'}:
            return (norm_text, 'text')
        block = str(row.get('parent_block_name', '') or '').strip()
        if block and entity_type == 'INSERT':
            return (block, 'block')
        return ('', 'none')

    def decision_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
        return (normalize_value(str(item.get('term', '') or '')), str(item.get('layer', '') or ''), str(item.get('parent_block_name', '') or ''), str(item.get('entity_type', '') or ''), str(item.get('geometry_kind', '') or ''))

    def inventory_row_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
        term, _source_type = candidate_term(row)
        return (normalize_value(term), str(row.get('layer', '') or ''), str(row.get('parent_block_name', '') or ''), str(row.get('entity_type', '') or ''), str(row.get('geometry_kind', '') or ''))

    def collect_region_annotations(region_output_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Map region-level inspection decisions back to instance bboxes.

        The region pipeline groups candidates semantically. For DXF review we need
        physical positions, so this function joins each floor's decisions back to
        its `cad_semantic_inventory.csv` rows.
        """
        result_path = region_output_dir / 'region_inspection_results.json'
        result = read_json(result_path)
        annotations: list[dict[str, Any]] = []
        matched_by_sheet: Counter[str] = Counter()
        decision_count_by_sheet: dict[str, int] = {}
        nonstandard_decisions_by_sheet: Counter[str] = Counter()
        for floor in result.get('floors', []) or []:
            if not isinstance(floor, dict):
                continue
            sheet_id = str(floor.get('sheet_id', '') or '')
            floor_id = str(floor.get('floor_id', '') or '')
            floor_name = str(floor.get('floor_name', '') or floor_id)
            sheet_dir = region_output_dir / sheet_id
            payload_path = sheet_dir / 'inspection_objects.json'
            inventory_path = sheet_dir / 'cad_semantic_inventory.csv'
            if not payload_path.exists() or not inventory_path.exists():
                continue
            payload = read_json(payload_path)
            decision_map: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
            for decision in payload.get('decisions', []) or []:
                if not isinstance(decision, dict) or decision.get('role') != 'inspection_object':
                    continue
                proposed_standard_name = str(decision.get('standard_class_name') or decision.get('class_name') or '').strip()
                if proposed_standard_name not in STANDARD_CLASS_NAMES:
                    nonstandard_decisions_by_sheet[sheet_id] += 1
                key = decision_key(decision)
                if key[0]:
                    decision_map[key] = decision
            decision_count_by_sheet[sheet_id] = len(decision_map)
            with inventory_path.open('r', encoding='utf-8-sig', newline='') as handle:
                for row in csv.DictReader(handle):
                    decision = decision_map.get(inventory_row_key(row))
                    if not decision:
                        continue
                    bbox = bbox_from_inventory(row)
                    if not bbox:
                        continue
                    original_object_name = str(decision.get('original_object_name') or decision.get('display_class_name') or decision.get('term') or '巡检对象').strip()
                    proposed_standard_name = str(decision.get('standard_class_name') or decision.get('class_name') or '').strip()
                    annotations.append({'object_id': row.get('object_id', ''), 'handle': row.get('handle', ''), 'source': row.get('source', ''), 'class_name': proposed_standard_name, 'standard_class_name': proposed_standard_name, 'original_object_name': original_object_name, 'reason': str(decision.get('reason') or 'region_inspection_result'), 'confidence': float(decision.get('confidence') or 0.0), 'sheet_id': sheet_id, 'floor_id': floor_id, 'floor_name': floor_name, 'source_type': decision.get('source_type', ''), 'term': original_object_name, 'layer': row.get('layer', ''), 'entity_type': row.get('entity_type', ''), 'geometry_kind': row.get('geometry_kind', ''), 'parent_block_name': row.get('parent_block_name', ''), 'norm_text': row.get('norm_text', ''), 'raw_text': row.get('raw_text', ''), 'bbox': bbox})
                    matched_by_sheet[sheet_id] += 1
        return (annotations, {'result_json': str(result_path.resolve()), 'region_count': result.get('region_count', 0), 'llm_model': result.get('llm_model', ''), 'matched_by_sheet': dict(matched_by_sheet), 'decision_count_by_sheet': decision_count_by_sheet, 'nonstandard_decision_count': sum(nonstandard_decisions_by_sheet.values()), 'nonstandard_decisions_by_sheet': dict(nonstandard_decisions_by_sheet)})

    def write_inspection_review_dxf(input_dxf: Path | str, region_output_dir: Path | str, output_dxf: Path | str) -> tuple[Path, Path, dict[str, Any]]:
        input_path = Path(input_dxf).expanduser().resolve()
        region_dir = Path(region_output_dir).resolve()
        output_path = Path(output_dxf).resolve()
        annotations, summary = collect_region_annotations(region_dir)
        deduped = dedupe_annotations(annotations)
        if not deduped:
            raise RuntimeError('没有找到可标注的区域级巡检对象实例。')
        try:
            write_marked_dxf(input_path, output_path, deduped)
        except PermissionError:
            output_path = timestamped_output_path(output_path)
            write_marked_dxf(input_path, output_path, deduped)
        class_counts = Counter((str(item['class_name']) for item in deduped))
        floor_counts = Counter((str(item.get('floor_name') or item.get('floor_id') or '') for item in deduped))
        report = {'input_dxf': str(input_path), 'output_dxf': str(output_path), 'source': 'region_inspection_results + per-sheet inspection_objects.json', 'total_annotations_before_dedupe': len(annotations), 'total_annotations': len(deduped), 'deduped_annotations': len(annotations) - len(deduped), 'class_counts': dict(class_counts.most_common()), 'floor_counts': dict(floor_counts.most_common()), **summary, 'sample_annotations': deduped[:30]}
        report_path = output_path.with_name('region_inspection_marked_report.json')
        write_json(report_path, report)
        return (output_path, report_path, report)

    def recognize_inspection_objects(input_dxf: Path | str, inventory_dir: Path | str, sheets_json: Path | str, output_dir: Path | str, *, llm_script: Path | str=DEFAULT_LLM_SCRIPT, no_llm: bool=False, write_review_dxf: bool=True) -> InspectionRecognitionResult:
        input_path = Path(input_dxf).expanduser().resolve()
        out_dir = Path(output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        result = run_pipeline(Path(inventory_dir).resolve(), Path(sheets_json).resolve(), out_dir, Path(llm_script).resolve(), no_llm=no_llm)
        marked_dxf: Path | None = None
        marked_report_json: Path | None = None
        if write_review_dxf:
            review_dir = out_dir.parent / 'review'
            review_dir.mkdir(parents=True, exist_ok=True)
            marked_dxf, marked_report_json, _report = write_inspection_review_dxf(input_path, out_dir, review_dir / f'{input_path.stem}_inspection_marked.dxf')
        catalog_rows = result.get('catalog_rows', []) if isinstance(result.get('catalog_rows'), list) else []
        return InspectionRecognitionResult(result_json=Path(result['artifacts']['result_json']), regions_manifest=out_dir / 'regions_manifest.json', marked_dxf=marked_dxf, marked_report_json=marked_report_json, region_count=int(result.get('region_count') or 0), inspection_type_count=len(catalog_rows), inspection_instance_count=sum((int(row.get('count') or 0) for row in catalog_rows if isinstance(row, dict))), llm_candidate_count=int(result.get('llm_candidate_count') or 0), llm_model=str(result.get('llm_model') or ''))
    return dict(locals())

_s04_api = _register_embedded_module(
    'fire_inspection_system.inspection_object_recognition',
    _build_s04_api(),
    aliases=('inspection_object_recognition',),
)

# === CONSOLIDATED PUBLIC API ===
from pathlib import Path
from typing import Any


def run_stage(
    input_dxf: Path,
    inventory_dir: Path,
    sheets_json: Path,
    run_dir: Path,
    *,
    no_llm: bool,
    write_review_dxf: bool,
) -> Any:
    return _s04_api.recognize_inspection_objects(
        input_dxf,
        inventory_dir,
        sheets_json,
        run_dir / "inspection_objects",
        no_llm=no_llm,
        write_review_dxf=write_review_dxf,
    )


__all__ = ["run_stage"]
