"""Verify persisted HVAC migration and render actual exported symbols.

Usage: python tools/verify_hvac_output.py --run <pipeline output directory>
All generated verification files are stored inside the selected run.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    project = Path(__file__).resolve().parents[1]
    if not run.is_relative_to(project):
        raise SystemExit("验证输出目录必须位于 duotuzhi 内")
    audit_rows = json.loads((run / "05_migration/migration_audit.json").read_text(encoding="utf-8"))
    summary = json.loads((run / "05_migration/stage_summary.json").read_text(encoding="utf-8"))
    doc = ezdxf.readfile(summary["output_dxf"])
    audit = doc.audit()
    rows = [r for r in audit_rows if r["discipline"] == "hvac"]
    migrated = [r for r in rows if r["migrated"]]
    result = {
        "dxf": summary["output_dxf"],
        "audit_errors": len(audit.errors), "audit_fixes": len(audit.fixes),
        "hvac_candidates": len(rows), "hvac_migrated": len(migrated),
        "hvac_rejected": len(rows) - len(migrated),
        "migrated_categories": dict(Counter(r["category"] for r in migrated)),
        "rejected_reasons": dict(Counter(r["reason"] for r in rows if not r["migrated"])),
        "entity_type_counts": dict(Counter(doc.entitydb[r["target_entity_handle"]].dxftype() for r in migrated)),
        "original_layer_mismatches": sum(doc.entitydb[r["target_entity_handle"]].dxf.layer != r["source_layer"] for r in migrated),
        "missing_name_labels": sum(not doc.entitydb.get(r["label_handle"]) for r in migrated),
        "local_fan_symbols": sum(r.get("excluded_remote_auxiliary_primitives", 0) > 0 for r in migrated),
    }
    fans = [r for r in migrated if "风机" in r["category"]]
    sizes = [bbox.extents([doc.entitydb[r["target_entity_handle"]]], fast=True).size for r in fans]
    result["largest_fan_symbol_dimension"] = max((max(v.x, v.y) for v in sizes), default=0)
    out = run / "10_hvac_verification"
    out.mkdir(parents=True, exist_ok=True)

    # Render actual saved CAD entities, including nested original geometry.
    from PIL import Image, ImageDraw, ImageFont
    from ezdxf.addons.drawing import RenderContext
    from ezdxf.disassemble import recursive_decompose
    from ezdxf.path import make_path
    picks = {}
    for row in migrated:
        picks.setdefault(row["category"], row)
    width, height = 500, 550
    image = Image.new("RGB", (width * len(picks), height), "#202830")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 18)
    context = RenderContext(doc)
    context.set_current_layout(doc.modelspace())
    render_errors = []
    for index, (category, row) in enumerate(picks.items()):
        entity = doc.entitydb[row["target_entity_handle"]]
        box = bbox.extents([entity], fast=True)
        span = max(box.size.x, box.size.y, 1.0)
        scale = 350 / span
        cx = (box.extmin.x + box.extmax.x) / 2
        cy = (box.extmin.y + box.extmax.y) / 2
        draw.multiline_text((index * width + 15, 18),
            f"{category}\n{row['detection_id']}\n原图层：{row['source_layer']}", font=font, fill="white", spacing=7)
        primitives = []
        for primitive in recursive_decompose([entity]):
            if primitive.dxftype() == "POLYLINE" and (primitive.is_poly_face_mesh or primitive.is_polygon_mesh):
                primitives.extend(primitive.virtual_entities())
            elif primitive.dxftype() != "POINT":
                primitives.append(primitive)
        for primitive in primitives:
            try:
                path = make_path(primitive)
                points = [(index * width + 250 + (v.x - cx) * scale,
                           335 - (v.y - cy) * scale)
                          for v in path.flattening(max(span * 0.0005, 1e-6))]
                if len(points) >= 2:
                    color = context.resolve_all(primitive).color[:7]
                    draw.line(points, fill=color, width=2)
            except (TypeError, ValueError, AttributeError) as exc:
                render_errors.append(f"{row['detection_id']}:{primitive.dxftype()}:{exc}")
    image.save(out / "迁移后暖通真实图元样例.png")
    result["sample_render_errors"] = render_errors
    (out / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
