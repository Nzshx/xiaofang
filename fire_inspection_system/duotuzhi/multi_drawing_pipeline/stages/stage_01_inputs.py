from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from ..common import write_csv, write_json


ODA_EXE = Path(r"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe")
CAD_SUFFIXES = {".dwg", ".dxf"}
SUPPORTED_DISCIPLINES = {"building", "water", "electrical", "hvac"}

DISCIPLINE_NAMES = {
    "building": "建筑",
    "water": "水",
    "electrical": "电",
    "hvac": "暖通",
    "unknown": "未分类",
}

NON_SPATIAL_RE = re.compile(
    r"说明|图例|目录|系统图|原理图|计算书|大样|详图|剖面|立面|防雷|接地|设计说明|通用图",
    re.I,
)
PLAN_RE = re.compile(r"平面|布置|PM|喷淋|消火栓|动力|照明|火灾.*报警|自动报警|通风|防排烟", re.I)


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def convert_dwg_isolated(source: Path, cache_root: Path) -> Path:
    """Convert one DWG without writing beside the source drawing."""
    if not ODA_EXE.is_file():
        raise FileNotFoundError(f"未找到 ODA File Converter: {ODA_EXE}")
    fingerprint = _fingerprint(source)
    cache_dir = cache_root / fingerprint
    final_dxf = cache_dir / f"{source.stem}.dxf"
    metadata = cache_dir / "conversion.json"
    if final_dxf.is_file() and final_dxf.stat().st_size > 0 and metadata.is_file():
        return final_dxf

    input_dir = cache_dir / "oda_input"
    output_dir = cache_dir / "oda_output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    isolated_dwg = input_dir / "input.dwg"
    shutil.copy2(source, isolated_dwg)
    command = [
        str(ODA_EXE), str(input_dir), str(output_dir), "ACAD2013", "DXF", "0", "1", "*.dwg",
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=900,
        check=False,
    )
    candidates = list(output_dir.rglob("*.dxf"))
    if result.returncode != 0 or not candidates:
        raise RuntimeError(
            "ODA 转换失败\n"
            f"source={source}\nreturncode={result.returncode}\n"
            f"stdout_tail={result.stdout[-3000:]}\nstderr_tail={result.stderr[-3000:]}"
        )
    shutil.copy2(candidates[0], final_dxf)
    write_json(
        metadata,
        {
            "source": str(source.resolve()),
            "source_size": source.stat().st_size,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "output": str(final_dxf.resolve()),
            "oda": str(ODA_EXE),
            "stdout_tail": result.stdout[-3000:],
            "stderr_tail": result.stderr[-3000:],
        },
    )
    return final_dxf


def infer_discipline(path: Path, project_root: Path) -> tuple[str, float, str]:
    relative = str(path.resolve().relative_to(project_root.resolve()))
    parts = [part.lower() for part in Path(relative).parts[:-1]]
    joined = "|".join(parts)
    name = path.stem.lower()

    if any(part in {"水", "给排水", "水专业", "喷淋", "消火栓"} for part in parts) or any(
        token in joined for token in ("给排水", "水专业", "喷淋", "消火栓")
    ):
        return "water", 0.99, "目录语义=给排水/喷淋/消火栓"
    if any(part in {"电", "电气", "电专业", "强电", "弱电"} for part in parts) or any(
        token in joined for token in ("电气", "电专业", "强电", "弱电")
    ):
        return "electrical", 0.99, "目录语义=电气/强电/弱电"
    if any(part in {"暖", "暖通", "暖专业", "通风", "空调"} for part in parts) or any(
        token in joined for token in ("暖通", "暖专业", "通风", "空调")
    ):
        return "hvac", 0.99, "目录语义=暖通/通风/空调"
    if (any(part in {"建筑", "建施"} for part in parts) or any(token in joined for token in ("建筑", "建施"))) and "建筑底图" not in joined:
        return "building", 0.99, "目录语义=建筑/建施"

    if re.search(r"给排水|喷淋|消火栓|消防给水", name):
        return "water", 0.85, "文件名水专业语义"
    if re.search(r"电气|动力|照明|火灾.*报警|弱电|配电", name):
        return "electrical", 0.85, "文件名电专业语义"
    if re.search(r"暖通|通风|空调|防排烟", name):
        return "hvac", 0.85, "文件名暖通语义"
    if re.search(r"建筑|平面.*剖面|平面图", name):
        return "building", 0.70, "文件名建筑语义"
    return "unknown", 0.0, "目录和文件名均无可靠专业语义"


def infer_role(path: Path, discipline: str) -> tuple[str, bool, float, str]:
    name = path.stem
    if discipline == "unknown":
        return "unclassified", False, 0.0, "无法确定专业"
    if "建筑底图" in str(path.parent):
        return "duplicate_reference_base", False, 0.95, "专业目录内建筑底图副本"
    if discipline == "building":
        # Architectural deliverables commonly combine plans and sections in
        # one DWG. The presence of "剖面" must not exclude the whole package
        # when it also contains spatial plans.
        confidence = 0.95 if PLAN_RE.search(name) else 0.70
        return "target_building", True, confidence, "建筑目标底图候选"
    if NON_SPATIAL_RE.search(name):
        match = NON_SPATIAL_RE.search(name)
        # A filename is only a routing hint.  A package named "系统图" or
        # "防雷" may still contain one or more floor plans and valid inspection
        # blocks, so keep it for DXF content inspection.  Stage 02 decides
        # whether it actually contains floor sheets; stages 03/04 only migrate
        # objects that belong to a located and registered floor sheet.
        return (
            "professional_content_review", True, 0.60,
            f"文件名疑似非空间内容:{match.group(0) if match else ''};仅作提示，仍解析图内标题和图元",
        )
    if PLAN_RE.search(name):
        return "professional_plan", True, 0.95, "文件名包含空间平面语义"
    # Some projects place all floor plans in one discipline-wide package and
    # omit "平面图" from the filename. Stage 02 validates its internal titles.
    return "professional_package", True, 0.65, "专业整包图，需由图内标题二次筛选"


def discover_project(project_root: Path) -> list[dict[str, object]]:
    project_root = project_root.resolve()
    rows: list[dict[str, object]] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CAD_SUFFIXES:
            continue
        discipline, discipline_confidence, discipline_evidence = infer_discipline(path, project_root)
        role, selected, role_confidence, role_evidence = infer_role(path, discipline)
        rows.append(
            {
                "source": str(path.resolve()),
                "relative_path": str(path.resolve().relative_to(project_root)),
                "discipline": discipline,
                "discipline_name": DISCIPLINE_NAMES[discipline],
                "role": role,
                "selected": selected,
                "confidence": round(min(discipline_confidence, role_confidence), 3),
                "evidence": f"{discipline_evidence};{role_evidence}",
            }
        )

    building_candidates = [row for row in rows if row["discipline"] == "building" and row["selected"]]
    if building_candidates:
        def building_score(row: dict[str, object]) -> tuple[float, int, int]:
            source = Path(str(row["source"]))
            exact_dir = int(source.parent.name in {"建筑", "建施"})
            not_copy = int("建筑底图" not in str(source.parent))
            return float(row["confidence"]), exact_dir + not_copy, source.stat().st_size

        chosen = max(building_candidates, key=building_score)
        for row in building_candidates:
            if row is chosen:
                row["role"] = "target_building"
                row["evidence"] = f"{row['evidence']};自动选择为唯一建筑目标"
            else:
                row["selected"] = False
                row["role"] = "alternate_building"
                row["evidence"] = f"{row['evidence']};存在评分更高的建筑目标"
    return rows


def _manual_candidates(
    building: Iterable[Path],
    water: Iterable[Path],
    electrical: Iterable[Path],
    hvac: Iterable[Path],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for discipline, paths in (
        ("building", building), ("water", water), ("electrical", electrical), ("hvac", hvac),
    ):
        for path in paths:
            resolved = path.expanduser().resolve()
            rows.append(
                {
                    "source": str(resolved),
                    "relative_path": resolved.name,
                    "discipline": discipline,
                    "discipline_name": DISCIPLINE_NAMES[discipline],
                    "role": "target_building" if discipline == "building" else "professional_plan",
                    "selected": True,
                    "confidence": 1.0,
                    "evidence": "用户显式选择",
                }
            )
    return rows


def _prepare_candidate(row: dict[str, object], cache_root: Path) -> dict[str, object]:
    source = Path(str(row["source"])).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() == ".dwg":
        dxf = convert_dwg_isolated(source, cache_root / str(row["discipline"]))
        converted = True
    elif source.suffix.lower() == ".dxf":
        dxf = source
        converted = False
    else:
        raise ValueError(f"只支持 DWG/DXF: {source}")
    prepared = dict(row)
    prepared.update(
        {
            "drawing_id": hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12],
            "source": str(source),
            "dxf": str(dxf.resolve()),
            "converted": converted,
        }
    )
    return prepared


def run_stage(
    building: list[Path] | None,
    water: list[Path] | None,
    electrical: list[Path] | None,
    stage_dir: Path,
    cache_root: Path,
    *,
    hvac: list[Path] | None = None,
    project_root: Path | None = None,
) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    if project_root:
        candidates = discover_project(project_root)
        mode = "project_discovery"
    else:
        candidates = _manual_candidates(building or [], water or [], electrical or [], hvac or [])
        mode = "manual_files"

    selected = [row for row in candidates if bool(row["selected"]) and row["discipline"] in SUPPORTED_DISCIPLINES]
    buildings = [row for row in selected if row["discipline"] == "building"]
    if len(buildings) != 1:
        raise RuntimeError(f"必须且只能确定1张建筑目标图，当前={len(buildings)}")
    # A building-only project is a valid environment: it produces the SBM,
    # architectural Object Set and architectural inspection route. Professional
    # plans are optional additions to that same coordinate space.

    prepared = [_prepare_candidate(row, cache_root) for row in selected]
    write_json(stage_dir / "input_candidates.json", candidates)
    write_csv(stage_dir / "input_candidates.csv", candidates)
    payload = {
        "stage": "01_inputs",
        "mode": mode,
        "project_root": str(project_root.resolve()) if project_root else "",
        "counts": {
            "candidate_total": len(candidates),
            "selected_total": len(prepared),
            "building": sum(item["discipline"] == "building" for item in prepared),
            "water": sum(item["discipline"] == "water" for item in prepared),
            "electrical": sum(item["discipline"] == "electrical" for item in prepared),
            "hvac": sum(item["discipline"] == "hvac" for item in prepared),
            "converted": sum(bool(item["converted"]) for item in prepared),
            "excluded": len(candidates) - len(selected),
        },
        "drawings": prepared,
        "candidate_manifest": str((stage_dir / "input_candidates.csv").resolve()),
    }
    write_json(stage_dir / "prepared_drawings.json", payload)
    return payload
