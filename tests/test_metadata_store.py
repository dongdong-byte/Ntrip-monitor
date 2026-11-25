"""관측소 메타데이터 반자동 동기화 테스트. 네트워크를 사용하지 않는다."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import metadata_store


class StationCodeTests(unittest.TestCase):
    def test_standard_names(self):
        self.assertEqual(metadata_store.station_code("ANHN-RTCM32"), "ANHN")
        self.assertEqual(metadata_store.station_code("ggeo-BINEX"), "GGEO")

    def test_unclear_names_not_guessed(self):
        for name in ("YNJU_RTCM30", "NOHYPHEN", "A-B-C", "-RTCM32", "ANHN-", ""):
            self.assertIsNone(metadata_store.station_code(name), name)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "metadata" / "gnssdata.json"

    def write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_adds_only_missing_identifiable_codes(self):
        self.write({"ANHN": {"station_name": "안흥 감시국", "agency": "국립해양측위정보원"}})
        added = metadata_store.add_missing(
            self.path,
            ["ANHN-RTCM32", "GGEO-BINEX", "GGEO-RTCM32", "YNJU_RTCM30"],
        )
        self.assertEqual(added, ["GGEO"])
        data = self.read()
        self.assertEqual(list(data), ["ANHN", "GGEO"])
        self.assertEqual(data["ANHN"]["station_name"], "안흥 감시국")
        self.assertEqual(data["GGEO"], {"station_name": "", "agency": ""})
        self.assertNotIn("YNJU_RTCM30", data)
        self.assertNotIn("YNJU", data)

    def test_lowercase_existing_key_not_duplicated(self):
        self.write({"anhn": {"station_name": "안흥", "agency": "x"}})
        self.assertEqual(metadata_store.add_missing(self.path, ["ANHN-RTCM32"]), [])

    def test_no_change_means_no_write(self):
        self.write({"ANHN": {"station_name": "안흥", "agency": "x"}})
        before = self.path.read_bytes()
        metadata_store.add_missing(self.path, ["ANHN-RTCM32"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_file_is_created(self):
        added = metadata_store.add_missing(self.path, ["SEOU-RTCM32"])
        self.assertEqual(added, ["SEOU"])
        self.assertEqual(self.read(), {"SEOU": {"station_name": "", "agency": ""}})

    def test_broken_json_is_not_overwritten(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ broken", encoding="utf-8")
        self.assertEqual(metadata_store.add_missing(self.path, ["GGEO-BINEX"]), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ broken")

    def test_failed_replace_keeps_file(self):
        self.write({"ANHN": {"station_name": "안흥", "agency": "x"}})
        before = self.path.read_bytes()
        with mock.patch("metadata_store.os.replace", side_effect=OSError("disk")):
            self.assertEqual(metadata_store.add_missing(self.path, ["GGEO-BINEX"]), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "gnssdata.json"

    def test_empty_fields_dropped_for_writer(self):
        self.path.write_text(json.dumps({
            "anhn": {"station_name": "안흥", "agency": "국립해양측위정보원"},
            "GGEO": {"station_name": "", "agency": ""},
            "HSDO": {"station_name": "홍성", "agency": ""},
        }, ensure_ascii=False), encoding="utf-8")
        data = metadata_store.load(self.path)
        self.assertEqual(data["ANHN"]["agency"], "국립해양측위정보원")
        # 빈 항목은 writer에 넘기지 않는다 → 엑셀의 기존 값을 빈칸으로 덮지 않음
        self.assertNotIn("GGEO", data)
        self.assertEqual(data["HSDO"], {"station_name": "홍성"})

    def test_missing_or_broken_file_returns_empty(self):
        self.assertEqual(metadata_store.load(self.path), {})
        self.path.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(metadata_store.load(self.path), {})

    def test_null_and_non_text_fields_do_not_become_labels(self):
        self.path.write_text(json.dumps({
            "NULL": {"station_name": None, "agency": None},
            "BAD": {"station_name": [], "agency": 123},
            "NAME": {"station_name": "이름만 등록", "agency": None},
        }), encoding="utf-8")
        self.assertEqual(metadata_store.load(self.path), {
            "NAME": {"station_name": "이름만 등록"}
        })


class WriterMetadataTests(unittest.TestCase):
    def test_unclear_mount_is_not_matched_to_guessed_code(self):
        from liveness_excel_writer import LivenessExcelWriter
        writer = LivenessExcelWriter("unused", "unused.xlsx", {
            "AB": {"station_name": "잘못 연결되면 안 됨", "agency": "기관"}
        })
        self.assertEqual(writer._station_code_from_mount("AB-CD-EF"), "")
        self.assertEqual(writer._station_code_from_mount("ANHN-RTCM32"), "ANHN")
        groups = writer._group_results_by_agency({"AB-CD-EF": mock.Mock(status="양호")})
        self.assertIn("AB-CD-EF", groups["소속미확인"])

    def test_empty_metadata_preserves_existing_name_and_records_unknown(self):
        from openpyxl import Workbook
        from liveness_excel_writer import LivenessExcelWriter
        writer = LivenessExcelWriter("unused", "unused.xlsx")
        wb = Workbook()
        self.addCleanup(wb.close)
        ws = wb.active
        writer._create_header(ws)
        ws.cell(2, 1, "기존 기관")
        ws.cell(2, 2, "기존 한글명")
        ws.cell(2, 4, "ANHN-RTCM32")
        results = {m: mock.Mock(status="양호") for m in ("ANHN-RTCM32", "YNJU_RTCM30")}
        with mock.patch("liveness_excel_writer.logger.warning") as warning:
            writer._backfill_metadata(ws)
            writer._write_results(ws, results, 5)
            writer._group_results_by_agency(results)
        warning.assert_not_called()
        self.assertEqual(ws.cell(2, 1).value, "기존 기관")
        self.assertEqual(ws.cell(2, 2).value, "기존 한글명")
        self.assertEqual(ws.cell(3, 1).value, "소속미확인")
        self.assertEqual(ws.cell(3, 4).value, "YNJU_RTCM30")
        self.assertEqual(ws.cell(3, 2).value, "")
        self.assertEqual(ws.cell(3, 3).value, "")
        self.assertEqual(ws.cell(3, 5).value, "양호")


if __name__ == "__main__":
    unittest.main()
