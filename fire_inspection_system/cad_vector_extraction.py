from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = PROJECT_ROOT / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

from cad_vector_inventory_agent import (  # type: ignore
    DEFAULT_MAX_INSERT_DEPTH,
    DEFAULT_TOP_N_LLM,
    file_sha256,
    run_inventory,
)

CAD_AGENT_SCRIPT = PROJECT_ROOT / "agents" / "cad_vector_inventory_agent.py"
DEFAULT_CACHE_ROOT = PROJECT_ROOT / ".cache" / "inventory"
REQUIRED_INVENTORY_FILES = (
    "inventory_manifest.json",
    "cad_object_inventory.csv",
    "cad_semantic_inventory.csv",
    "cad_geometry_inventory.csv",
    "cad_block_signatures.json",
    "cad_object_catalog.csv",
)


@dataclass(frozen=True)
class CadVectorExtractionResult:
    input_dxf: Path
    sha256: str
    inventory_dir: Path
    manifest_path: Path
    cache_hit: bool
    cache_version: str
    counts: dict[str, Any]


def stage_version(name: str, paths: list[Path], extras: list[str] | None = None) -> str:
    """Make a cache key from the code and settings that affect extraction."""
    digest = hashlib.sha256(name.encode("utf-8"))
    for path in paths:
        digest.update(str(path.relative_to(PROJECT_ROOT)).encode("utf-8", errors="replace"))
        digest.update(file_sha256(path).encode("ascii") if path.exists() else b"missing")
    for value in extras or []:
        digest.update(str(value).encode("utf-8", errors="replace"))
    return digest.hexdigest()[:20]


def inventory_cache_version(
    *,
    max_depth: int,
    top_n_llm: int,
    expand_all_inserts: bool,
    scan_modelspace_only: bool,
) -> str:
    mode = "full_insert_expand" if expand_all_inserts else "selective_semantic_insert_expand"
    return stage_version(
        "fire-inspection-vector-inventory-v1",
        [CAD_AGENT_SCRIPT],
        [str(max_depth), str(top_n_llm), mode, str(scan_modelspace_only)],
    )


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def inventory_is_complete(inventory_dir: Path) -> bool:
    return all((inventory_dir / name).exists() for name in REQUIRED_INVENTORY_FILES)


def extract_cad_vector_information(
    input_dxf: Path | str,
    *,
    cache_root: Path | str = DEFAULT_CACHE_ROOT,
    force: bool = False,
    scan_modelspace_only: bool = True,
    max_depth: int = DEFAULT_MAX_INSERT_DEPTH,
    top_n_llm: int = DEFAULT_TOP_N_LLM,
    expand_all_inserts: bool = False,
) -> CadVectorExtractionResult:
    """Extract reusable CAD vector inventory from a DXF file.

    The default mode records real INSERT instances and only selectively expands
    block internals that carry semantic information. This keeps the inventory
    useful for inspection-object recognition without inflating every block into
    thousands of inner line segments.
    """
    input_path = Path(input_dxf).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if input_path.suffix.lower() != ".dxf":
        raise ValueError(f"只支持 DXF 文件: {input_path}")

    sha256 = file_sha256(input_path)
    version = inventory_cache_version(
        max_depth=max_depth,
        top_n_llm=top_n_llm,
        expand_all_inserts=expand_all_inserts,
        scan_modelspace_only=scan_modelspace_only,
    )
    inventory_dir = Path(cache_root).resolve() / sha256 / version
    manifest_path = inventory_dir / "inventory_manifest.json"

    if not force and inventory_is_complete(inventory_dir):
        manifest = read_json(manifest_path)
        counts = manifest.get("counts", {}) if isinstance(manifest.get("counts"), dict) else {}
        return CadVectorExtractionResult(
            input_dxf=input_path,
            sha256=sha256,
            inventory_dir=inventory_dir,
            manifest_path=manifest_path,
            cache_hit=True,
            cache_version=version,
            counts=counts,
        )

    manifest = run_inventory(
        input_dxf=input_path,
        output_dir=inventory_dir,
        scan_modelspace_only=scan_modelspace_only,
        max_depth=max_depth,
        top_n_llm=top_n_llm,
        expand_all_inserts=expand_all_inserts,
    )
    manifest["cache_version"] = version
    manifest["source_sha256"] = sha256
    write_json(manifest_path, manifest)
    counts = manifest.get("counts", {}) if isinstance(manifest.get("counts"), dict) else {}

    return CadVectorExtractionResult(
        input_dxf=input_path,
        sha256=sha256,
        inventory_dir=inventory_dir,
        manifest_path=manifest_path,
        cache_hit=False,
        cache_version=version,
        counts=counts,
    )
