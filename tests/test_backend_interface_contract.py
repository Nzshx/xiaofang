import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fire_inspection_system import backend_interface_3 as api


class BackendInterfaceContractTests(unittest.TestCase):
    def test_interface_three_payload_matches_excel_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "pipeline_summary.json").write_text("{}", encoding="utf-8")
            thumbnail = run_dir / "thumbnail.png"
            route = run_dir / "route.png"
            thumbnail.write_bytes(b"thumbnail")
            route.write_bytes(b"route")
            record = api.RenderRecord(
                floor_id="F1",
                image_path=thumbnail,
                image_width=100,
                image_height=100,
                cad_bbox=(0.0, 0.0, 100.0, 100.0),
                scale=1.0,
                offset_x=0.0,
                offset_y=0.0,
                route_image_path=route,
            )
            targets = [
                {
                    "target_id": "POINT-1",
                    "target_class": "安全出口",
                    "source_floor_id": "F1",
                    "floor_name": "一层",
                    "building_name": "1号楼",
                    "cad_x": 25.0,
                    "cad_y": 75.0,
                    "confidence": 0.9,
                    "spec": "A型",
                }
            ]
            with (
                patch.object(api, "load_render_records", return_value={"F1": record}),
                patch.object(api, "_load_targets", return_value=targets),
                patch.object(api, "_route_features", return_value=[]),
                patch.object(api, "_mandatory_by_target", return_value={"POINT-1": True}),
            ):
                payload = api.build_cad_result_payloads("CAD-1", run_dir)[0].payload

            self.assertEqual(
                set(payload),
                {
                    "cad_id",
                    "thumbnail_url",
                    "buliding_name",
                    "floor",
                    "id",
                    "cad_points",
                    "route_id",
                    "route_url",
                },
            )
            self.assertEqual(payload["buliding_name"], "1号楼")
            self.assertEqual(payload["floor"], "一层")
            self.assertNotIn("route", payload)
            self.assertEqual(payload["cad_points"][0]["mainId"], payload["id"])

    def test_interface_five_request_parser_matches_excel_shape(self) -> None:
        selections = api._parse_route_selection_requests(
            [
                {"point_id": ["P1", "P2"], "route_id": "R1"},
                {"point_id": ["P3", "P4"], "route_id": "R2"},
            ]
        )
        self.assertEqual(
            selections,
            [
                api.RouteSelectionRequest("R1", ("P1", "P2")),
                api.RouteSelectionRequest("R2", ("P3", "P4")),
            ],
        )

    def test_interface_six_callback_matches_excel_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            route_job_dir = root / "route-job"
            route_job_dir.mkdir()
            route_image = root / "generated-route.png"
            route_image.write_bytes(b"generated-route")
            config = api.BridgeConfig(
                job_root=root / "jobs",
                route_callback_url="https://backend.example/route/callback",
            )
            bridge = api.CadBackendBridge(config)
            sent = []
            try:
                with (
                    patch.object(
                        api,
                        "build_replanned_route_payload",
                        return_value=SimpleNamespace(floor_id="F1", route_path=route_image),
                    ),
                    patch.object(
                        api,
                        "post_json",
                        side_effect=lambda url, payload, **kwargs: sent.append(
                            (url, payload)
                        )
                        or {"msg": "接收成功", "result": True},
                    ),
                ):
                    bridge._run_route_generation(
                        "CAD-1",
                        root,
                        (api.RouteSelectionRequest("ROUTE-1", ("P1", "P2")),),
                        route_job_dir,
                    )
            finally:
                bridge._executor.shutdown(wait=True)

            payload = json.loads(
                (route_job_dir / "interface6_result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(set(payload), {"cad_id", "route"})
            self.assertEqual(payload["cad_id"], "CAD-1")
            self.assertEqual(set(payload["route"][0]), {"route_id", "route_url"})
            self.assertEqual(payload["route"][0]["route_id"], "ROUTE-1")
            self.assertEqual(sent[0][0], config.route_callback_url)
            self.assertEqual(sent[0][1], payload)


if __name__ == "__main__":
    unittest.main()
