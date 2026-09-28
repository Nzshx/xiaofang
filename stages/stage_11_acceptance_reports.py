"""阶段 11：按楼层生成巡检对象验收报告。"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AcceptanceReportResult:
    output_dir: Path
    index_markdown: Path
    summary_json: Path
    floor_reports: dict[str, Path]
    floor_summaries: dict[str, dict[str, Any]]

    def to_summary(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "output_dir": str(self.output_dir),
            "index_markdown": str(self.index_markdown),
            "summary_json": str(self.summary_json),
            "floor_count": len(self.floor_reports),
            "floor_reports": {
                floor_id: str(path)
                for floor_id, path in self.floor_reports.items()
            },
            "floors": self.floor_summaries,
        }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return path


def _floor_sort_key(floor_id: str) -> tuple[int, int, str]:
    value = floor_id.upper()
    match = re.fullmatch(r"([BF])(\d+)", value)
    if match and match.group(1) == "B":
        return (0, -int(match.group(2)), value)
    if match and match.group(1) == "F":
        return (1, int(match.group(2)), value)
    if value in {"RF", "ROOF"}:
        return (2, 0, value)
    return (3, 0, value)


def _floor_mapping(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, dict):
        return {
            str(key): row
            for key, row in value.items()
            if isinstance(row, dict)
        }
    if isinstance(value, list):
        result: dict[str, dict[str, Any]] = {}
        for row in value:
            if not isinstance(row, dict):
                continue
            floor_id = str(row.get("floor_id") or "").strip()
            if floor_id:
                result[floor_id] = row
        return result
    return {}


def _text(value: Any, default: str = "") -> str:
    result = str(value or "").strip()
    return result or default


def _raw_cad_name(row: dict[str, Any]) -> str:
    annotation = row.get("annotation") or {}
    return (
        _text(annotation.get("original_object_name"))
        or _text(row.get("original_object_name"))
        or _text(row.get("raw_name"))
        or _text(annotation.get("raw_name"))
        or _text(annotation.get("source_class_name"))
        or _text(row.get("object_id"))
        or _text(row.get("target_id"), "未命名 CAD 对象")
    )


def _class_name(row: dict[str, Any]) -> str:
    annotation = row.get("annotation") or {}
    return (
        _text(row.get("class_name"))
        or _text(row.get("standard_class_name"))
        or _text(annotation.get("standard_class_name"))
        or _text(annotation.get("target_class"))
        or "未分类"
    )


def _target_id(row: dict[str, Any], fallback: str) -> str:
    return (
        _text(row.get("target_id"))
        or _text(row.get("object_id"))
        or _text(row.get("source_object_id"))
        or fallback
    )


def _normalize_target(row: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    return {
        "target_id": _target_id(row, fallback_id),
        "object_id": (
            _text(row.get("object_id"))
            or _text(row.get("source_object_id"))
            or _text((row.get("annotation") or {}).get("source_object_id"))
        ),
        "class_name": _class_name(row),
        "raw_name": _raw_cad_name(row),
        "mandatory": bool(row.get("mandatory")),
    }


def _recognized_target_pool(
    recognition: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    pools: dict[str, list[dict[str, Any]]] = {}
    floor_names: dict[str, str] = {}
    for floor in recognition.get("floors") or []:
        if not isinstance(floor, dict):
            continue
        floor_id = _text(floor.get("floor_id"))
        if not floor_id:
            continue
        floor_names[floor_id] = (
            _text(floor.get("display_name"))
            or _text(floor.get("floor_name"))
            or floor_id
        )
        targets: list[dict[str, Any]] = []
        for row_index, row in enumerate(floor.get("catalog_rows") or [], start=1):
            if not isinstance(row, dict):
                continue
            count = max(0, int(row.get("count") or 0))
            for instance_index in range(1, count + 1):
                targets.append(
                    _normalize_target(
                        row,
                        (
                            f"{floor_id}_RECOGNIZED_"
                            f"{row_index:04d}_{instance_index:05d}"
                        ),
                    )
                )
        pools[floor_id] = targets
    return pools, floor_names


def _candidate_target_pool(
    candidates: dict[str, Any],
    fallback_pools: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    floors = _floor_mapping(candidates.get("floors"))
    if not floors:
        return fallback_pools
    result: dict[str, list[dict[str, Any]]] = {}
    for floor_id, floor in floors.items():
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for index, row in enumerate(floor.get("candidates") or [], start=1):
            if not isinstance(row, dict):
                continue
            normalized = _normalize_target(
                row,
                f"{floor_id}_CANDIDATE_{index:06d}",
            )
            if normalized["target_id"] in seen:
                continue
            seen.add(normalized["target_id"])
            rows.append(normalized)
        result[floor_id] = rows
    return result


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator * 100.0 / denominator, 2)


def _md(value: Any) -> str:
    return _text(value, "-").replace("|", r"\|").replace("\r", " ").replace(
        "\n", " "
    )


def _safe_filename(floor_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", floor_id).strip("_") or "floor"


def _build_floor_report(
    floor_id: str,
    floor_name: str,
    target_pool: list[dict[str, Any]],
    plan: dict[str, Any],
    forwarding: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    target_by_id = {row["target_id"]: row for row in target_pool}
    visit_sequence: list[dict[str, Any]] = []
    selected_by_id: dict[str, dict[str, Any]] = {}
    occurrence: Counter[str] = Counter()

    plan_targets = [
        row
        for row in plan.get("targets") or []
        if isinstance(row, dict)
    ]
    plan_targets.sort(key=lambda row: int(row.get("visit_order") or 0))
    for index, row in enumerate(plan_targets, start=1):
        normalized = _normalize_target(row, f"{floor_id}_PLAN_{index:06d}")
        target_id = normalized["target_id"]
        if target_id in target_by_id:
            normalized = {**normalized, **target_by_id[target_id]}
        selected_by_id.setdefault(target_id, normalized)
        occurrence[target_id] += 1
        visit_sequence.append(
            {
                **normalized,
                "visit_order": int(row.get("visit_order") or index),
                "route_segment_id": _text(row.get("route_segment_id"), "-"),
                "visit_ordinal": occurrence[target_id],
            }
        )

    visit_events = [
        row
        for row in forwarding.get("target_visit_events") or []
        if isinstance(row, dict)
    ]
    completed_visit_keys = {
        (
            _text(row.get("target_id")),
            int(row.get("visit_order") or 0),
            _text(row.get("route_segment_id")),
        )
        for row in visit_events
    }
    completed_ids = {
        _text(row.get("target_id"))
        for row in visit_events
        if _text(row.get("target_id"))
    }
    selected_ids = set(selected_by_id)
    completed_selected_ids = selected_ids & completed_ids

    target_categories = {
        row["class_name"] for row in target_pool if row["class_name"]
    }
    selected_categories = {
        row["class_name"] for row in selected_by_id.values() if row["class_name"]
    }
    completed_categories = {
        selected_by_id[target_id]["class_name"]
        for target_id in completed_selected_ids
    }

    all_category_counts = Counter(row["class_name"] for row in target_pool)
    selected_category_counts = Counter(
        row["class_name"] for row in selected_by_id.values()
    )
    completed_category_counts = Counter(
        selected_by_id[target_id]["class_name"]
        for target_id in completed_selected_ids
    )
    category_names = sorted(
        set(all_category_counts) | set(selected_category_counts)
    )

    metrics = {
        "floor_id": floor_id,
        "floor_name": floor_name,
        "route_status": _text(forwarding.get("status"), "未生成最终路线"),
        "route_feasible": bool(forwarding.get("feasible")),
        "target_category_count": len(target_categories),
        "target_object_count": len(target_pool),
        "sampled_category_count": len(selected_categories),
        "sampled_object_count": len(selected_ids),
        "completed_category_count": len(completed_categories),
        "completed_object_count": len(completed_selected_ids),
        "route_visit_count": len(visit_sequence),
        "repeated_visit_count": max(0, len(visit_sequence) - len(selected_ids)),
        "category_sample_coverage_percent": _ratio(
            len(selected_categories), len(target_categories)
        ),
        "object_sample_coverage_percent": _ratio(
            len(selected_ids), len(target_pool)
        ),
        "execution_completion_percent": _ratio(
            len(completed_selected_ids), len(selected_ids)
        ),
    }

    lines = [
        f"# {floor_name}巡检验收报告",
        "",
        f"- 楼层编号：`{_md(floor_id)}`",
        f"- 最终路线状态：{_md(metrics['route_status'])}",
        f"- 路线是否可行：{'是' if metrics['route_feasible'] else '否'}",
        "",
        "## 验收摘要",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 巡检目标类别 | {metrics['target_category_count']} 种 |",
        f"| 巡检目标对象 | {metrics['target_object_count']} 个 |",
        f"| 已抽检类别 | {metrics['sampled_category_count']} 种 |",
        f"| 已抽检对象（唯一对象） | {metrics['sampled_object_count']} 个 |",
        f"| 实际完成类别 | {metrics['completed_category_count']} 种 |",
        f"| 实际完成对象（唯一对象） | {metrics['completed_object_count']} 个 |",
        f"| 路线访问次数 | {metrics['route_visit_count']} 次 |",
        f"| 重复访问次数 | {metrics['repeated_visit_count']} 次 |",
        "",
        "## 巡检对象类别与数量",
        "",
        "| 类别 | 楼层目标数 | 抽检数 | 实际完成数 | 对象抽检覆盖率 |",
        "|---|---:|---:|---:|---:|",
    ]
    if category_names:
        for category in category_names:
            available = all_category_counts[category]
            sampled = selected_category_counts[category]
            completed = completed_category_counts[category]
            lines.append(
                f"| {_md(category)} | {available} | {sampled} | {completed} | "
                f"{_ratio(sampled, available):.2f}% |"
            )
    else:
        lines.append("| 未识别到巡检目标 | 0 | 0 | 0 | 0.00% |")

    lines.extend(
        [
            "",
            "## 巡检对象访问顺序",
            "",
            "对象名称优先使用识别结果中的原始 CAD 对象名；同一对象因路线回访而重复出现时会明确标记。",
            "",
        ]
    )
    if visit_sequence:
        lines.extend(
            [
                "| 顺序 | 路线段 | 原始 CAD 对象名 | 类别 | 对象 ID | "
                "访问次数 | 实际完成 |",
                "|---:|---|---|---|---|---:|---|",
            ]
        )
        for row in visit_sequence:
            key = (
                row["target_id"],
                row["visit_order"],
                row["route_segment_id"],
            )
            completed = key in completed_visit_keys
            lines.append(
                f"| {row['visit_order']} | {_md(row['route_segment_id'])} | "
                f"{_md(row['raw_name'])} | {_md(row['class_name'])} | "
                f"`{_md(row['target_id'])}` | {row['visit_ordinal']} | "
                f"{'是' if completed else '否'} |"
            )
    else:
        lines.append("当前楼层尚未形成抽检访问计划，因此没有可列出的访问顺序。")

    lines.extend(
        [
            "",
            "## 完成率结论",
            "",
            (
                f"- 类别抽检覆盖率：{metrics['sampled_category_count']}/"
                f"{metrics['target_category_count']} = "
                f"**{metrics['category_sample_coverage_percent']:.2f}%**。"
            ),
            (
                f"- 对象抽检覆盖率：{metrics['sampled_object_count']}/"
                f"{metrics['target_object_count']} = "
                f"**{metrics['object_sample_coverage_percent']:.2f}%**。"
            ),
            (
                f"- 抽检计划执行完成率：{metrics['completed_object_count']}/"
                f"{metrics['sampled_object_count']} = "
                f"**{metrics['execution_completion_percent']:.2f}%**。"
            ),
            "",
        ]
    )
    return lines, metrics


def run_stage(
    run_dir: Path | str,
    *,
    pipeline_summary: dict[str, Any] | None = None,
    path_result: dict[str, Any] | None = None,
) -> tuple[AcceptanceReportResult, Path]:
    """Generate one Markdown acceptance report per floor and update summaries."""
    run = Path(run_dir).resolve()
    output = run / "acceptance_reports"
    output.mkdir(parents=True, exist_ok=True)

    recognition = _read_json(
        run / "inspection_objects" / "region_inspection_results.json"
    )
    candidates = _read_json(
        run / "path_planning" / "dual_graph" / "physical_access_candidates.json"
    )
    visit_plan = _read_json(
        run
        / "path_planning"
        / "dual_graph"
        / "physical_walk"
        / "control_visit_plan.json"
    )
    forwarding = _read_json(
        run
        / "path_planning"
        / "dual_graph"
        / "physical_walk"
        / "forwarding_route.json"
    )

    recognized_pools, floor_names = _recognized_target_pool(recognition)
    target_pools = _candidate_target_pool(candidates, recognized_pools)
    plan_floors = _floor_mapping(visit_plan.get("floors"))
    forwarding_floors = _floor_mapping(forwarding.get("floors"))
    floor_ids = sorted(
        set(target_pools) | set(plan_floors) | set(forwarding_floors),
        key=_floor_sort_key,
    )

    floor_reports: dict[str, Path] = {}
    floor_summaries: dict[str, dict[str, Any]] = {}
    for floor_id in floor_ids:
        floor_name = floor_names.get(floor_id, floor_id)
        lines, metrics = _build_floor_report(
            floor_id,
            floor_name,
            target_pools.get(floor_id, []),
            plan_floors.get(floor_id, {}),
            forwarding_floors.get(floor_id, {}),
        )
        report_path = output / (
            f"{_safe_filename(floor_id)}_acceptance_report.md"
        )
        report_path.write_text("\n".join(lines), encoding="utf-8")
        floor_reports[floor_id] = report_path
        floor_summaries[floor_id] = {**metrics, "report": str(report_path)}

    index_lines = [
        "# 分楼层巡检验收报告",
        "",
        "下表汇总各楼层的目标对象、抽检覆盖情况与实际执行完成率。",
        "",
        "| 楼层 | 目标类别 | 目标对象 | 抽检类别 | 抽检对象 | "
        "类别覆盖率 | 对象覆盖率 | 执行完成率 | 报告 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for floor_id in floor_ids:
        row = floor_summaries[floor_id]
        report_name = floor_reports[floor_id].name
        index_lines.append(
            f"| {_md(row['floor_name'])} (`{_md(floor_id)}`) | "
            f"{row['target_category_count']} | {row['target_object_count']} | "
            f"{row['sampled_category_count']} | {row['sampled_object_count']} | "
            f"{row['category_sample_coverage_percent']:.2f}% | "
            f"{row['object_sample_coverage_percent']:.2f}% | "
            f"{row['execution_completion_percent']:.2f}% | "
            f"[查看报告]({report_name}) |"
        )
    if not floor_ids:
        index_lines.append("| 未识别到楼层 | 0 | 0 | 0 | 0 | 0.00% | 0.00% | 0.00% | - |")
    index_lines.append("")

    index_path = output / "acceptance_report_index.md"
    index_path.write_text("\n".join(index_lines), encoding="utf-8")
    summary_path = _write_json(
        output / "acceptance_report_summary.json",
        {
            "schema_version": 1,
            "artifact_type": "per_floor_inspection_acceptance_reports",
            "metric_definitions": {
                "category_sample_coverage_percent": (
                    "抽检类别数 / 楼层巡检目标类别数"
                ),
                "object_sample_coverage_percent": (
                    "抽检唯一对象数 / 楼层巡检目标唯一对象数"
                ),
                "execution_completion_percent": (
                    "实际完成的抽检唯一对象数 / 抽检唯一对象数"
                ),
            },
            "index_markdown": str(index_path),
            "floor_reports": {
                floor_id: str(path)
                for floor_id, path in floor_reports.items()
            },
            "floors": floor_summaries,
        },
    )
    result = AcceptanceReportResult(
        output_dir=output,
        index_markdown=index_path,
        summary_json=summary_path,
        floor_reports=floor_reports,
        floor_summaries=floor_summaries,
    )
    result_summary = result.to_summary()

    if path_result is not None:
        path_result["acceptance_reporting"] = result_summary
        outputs = path_result.setdefault("outputs", {})
        outputs["acceptance_report_index"] = str(index_path)
        outputs["acceptance_report_summary"] = str(summary_path)
        outputs["floor_acceptance_reports"] = {
            floor_id: str(path)
            for floor_id, path in floor_reports.items()
        }
        path_summary_path = (
            run / "path_planning" / "path_planning_summary.json"
        )
        path_result["summary_path"] = str(path_summary_path)
        _write_json(path_summary_path, path_result)

    current_pipeline_summary = pipeline_summary
    if current_pipeline_summary is None:
        current_pipeline_summary = _read_json(run / "pipeline_summary.json")
    current_pipeline_summary["acceptance_reporting"] = result_summary
    if path_result is not None:
        current_pipeline_summary["path_planning"] = path_result
    pipeline_summary_path = _write_json(
        run / "pipeline_summary.json",
        current_pipeline_summary,
    )
    return result, pipeline_summary_path


__all__ = ["AcceptanceReportResult", "run_stage"]
