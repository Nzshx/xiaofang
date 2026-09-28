from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ezdxf

from multi_drawing_pipeline import cache_support
from multi_drawing_pipeline.common import Sheet
from multi_drawing_pipeline.stages.room_topology import build_room_topologies_cached


class CadParseCacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        cache_support.clear_document_cache()

    def test_unchanged_cad_is_parsed_only_once_per_process(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.dxf"
            ezdxf.new("R2018").saveas(source)
            loader = ezdxf.readfile
            with mock.patch.object(cache_support.ezdxf, "readfile", wraps=loader) as patched:
                first = cache_support.readfile_once(source)
                second = cache_support.readfile_once(source)
            self.assertIs(first, second)
            self.assertEqual(patched.call_count, 1)

    def test_content_cache_key_changes_when_rule_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.bin"
            rule = root / "rule.py"
            source.write_bytes(b"same drawing")
            rule.write_text("VERSION = 1\n", encoding="utf-8")
            first = cache_support.content_cache_key(
                "test", input_files=[source], rule_files=[rule], options={"mode": "all"},
            )
            rule.write_text("VERSION = 2\n", encoding="utf-8")
            second = cache_support.content_cache_key(
                "test", input_files=[source], rule_files=[rule], options={"mode": "all"},
            )
            self.assertNotEqual(first, second)


class RoomTopologyCacheTests(unittest.TestCase):
    def test_room_topology_is_reused_across_calls(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "building.dxf"
            document = ezdxf.new("R2018")
            document.modelspace().add_line((0, 0), (100, 0), dxfattribs={"layer": "WALL"})
            document.saveas(source)
            sheet = Sheet(
                drawing=str(source), discipline="building", kind="建筑平面", floor="F1",
                title="一层平面图", title_x=10, title_y=10, center_x=50, center_y=50,
                min_x=0, min_y=0, max_x=100, max_y=100, sheet_id="S1", floors=["F1"],
            )
            first, first_hit = build_room_topologies_cached(
                document, [sheet], source_path=source, cache_root=root / "cache",
            )
            second, second_hit = build_room_topologies_cached(
                ezdxf.new("R2018"), [sheet], source_path=source, cache_root=root / "cache",
            )
            self.assertFalse(first_hit)
            self.assertTrue(second_hit)
            self.assertEqual(first["S1"].walls.segments, second["S1"].walls.segments)


if __name__ == "__main__":
    unittest.main()
