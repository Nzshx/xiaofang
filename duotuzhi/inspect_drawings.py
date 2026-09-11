from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

import ezdxf


FLOOR_RE = re.compile(
    r"(?:地下\s*[一二两三四五六七八九十0-9]+\s*层|地上\s*[一二两三四五六七八九十0-9]+\s*层|"
    r"负?\s*[一二两三四五六七八九十0-9]+\s*层|屋面|屋顶|机房层|设备层|"
    r"[BbFf]?\s*-?\s*\d+\s*[Ff]|-\s*\d+\s*[Ff])"
)
KEYWORD_RE = re.compile(
    r"消火栓|消防栓|喷头|喷淋|水泵|水箱|水池|报警阀|灭火器|灭火装置|"
    r"发电机|配电房|变配电|应急照明|疏散|探测器|烟感|温感|消防电话|"
    r"消防通讯|广播|警报|区域显示|手动报警|手报|报警控制器|联动控制器|消防控制室"
)
RELEVANT_LAYER_RE = re.compile(
    r"EQUIP|消防|喷头|喷淋|给水|应急|广播|电话|通讯|通信|报警|探测|疏散|照明",
    re.IGNORECASE,
)


def entity_text(entity: object) -> str:
    kind = entity.dxftype()
    if kind == "TEXT":
        return str(entity.dxf.text or "")
    if kind == "MTEXT":
        try:
            return str(entity.plain_text())
        except Exception:
            return str(entity.text or "")
    return ""


def point_of(entity: object) -> list[float] | None:
    try:
        if entity.dxftype() == "TEXT":
            p = entity.dxf.insert
        else:
            p = entity.dxf.insert
        return [round(float(p.x), 3), round(float(p.y), 3)]
    except Exception:
        return None


def inspect(path: Path) -> dict[str, object]:
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    types: collections.Counter[str] = collections.Counter()
    layers: collections.Counter[str] = collections.Counter()
    blocks: collections.Counter[str] = collections.Counter()
    relevant_layer_blocks: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    floor_hits: list[dict[str, object]] = []
    keyword_hits: list[dict[str, object]] = []
    for entity in msp:
        kind = entity.dxftype()
        layer = str(entity.dxf.get("layer", "0"))
        types[kind] += 1
        layers[layer] += 1
        if kind == "INSERT":
            block_name = str(entity.dxf.name)
            blocks[block_name] += 1
            if RELEVANT_LAYER_RE.search(layer):
                relevant_layer_blocks[layer][block_name] += 1
            for attrib in entity.attribs:
                text = str(attrib.dxf.text or "").strip()
                if text and FLOOR_RE.search(text):
                    floor_hits.append({"text": text, "point": point_of(attrib), "layer": layer})
                if text and KEYWORD_RE.search(text):
                    keyword_hits.append({"text": text, "point": point_of(attrib), "layer": layer})
        elif kind in {"TEXT", "MTEXT"}:
            text = entity_text(entity).strip().replace("\n", " ")
            if text and FLOOR_RE.search(text):
                floor_hits.append({"text": text[:200], "point": point_of(entity), "layer": layer})
            if text and KEYWORD_RE.search(text):
                keyword_hits.append({"text": text[:200], "point": point_of(entity), "layer": layer})
    relevant_block_names = {
        name for counter in relevant_layer_blocks.values() for name in counter
    }
    relevant_block_definitions: dict[str, dict[str, object]] = {}
    for name in sorted(relevant_block_names):
        try:
            block = doc.blocks.get(name)
        except Exception:
            continue
        definition_types: collections.Counter[str] = collections.Counter()
        definition_layers: collections.Counter[str] = collections.Counter()
        definition_texts: list[str] = []
        for child in block:
            definition_types[child.dxftype()] += 1
            definition_layers[str(child.dxf.get("layer", "0"))] += 1
            if child.dxftype() in {"TEXT", "MTEXT"}:
                text = entity_text(child).strip().replace("\n", " ")
                if text:
                    definition_texts.append(text[:300])
            elif child.dxftype() == "ATTDEF":
                text = str(child.dxf.get("text", "")).strip()
                tag = str(child.dxf.get("tag", "")).strip()
                if text or tag:
                    definition_texts.append(f"{tag}:{text}"[:300])
        relevant_block_definitions[name] = {
            "types": definition_types.most_common(),
            "layers": definition_layers.most_common(),
            "texts": definition_texts[:100],
        }
    return {
        "path": str(path.resolve()),
        "size": path.stat().st_size,
        "entity_count": sum(types.values()),
        "types": types.most_common(),
        "top_layers": layers.most_common(120),
        "top_blocks": blocks.most_common(120),
        "relevant_layer_blocks": {
            layer: counter.most_common() for layer, counter in sorted(relevant_layer_blocks.items())
        },
        "relevant_block_definitions": relevant_block_definitions,
        "floor_hits": floor_hits[:1000],
        "keyword_hits": keyword_hits[:1000],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查 DXF 的图层、块、楼层文字和巡检关键词。")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = [inspect(path) for path in args.paths]
    text = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(args.output.resolve())
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
