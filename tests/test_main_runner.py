"""캐스터 반복 실행과 장애 격리 테스트. 네트워크·실제 엑셀을 사용하지 않는다."""

import unittest
import tempfile
from pathlib import Path
from unittest import mock

import liveness_main as lm
from config_loader import CasterConfig
from liveness_checker import MountResult


def caster(name):
    return CasterConfig(name=name, host=f"{name}.example", port=2101, user="u", password="p")


def results(*statuses):
    return {
        f"M{i:03d}-RTCM32": MountResult(mountpoint=f"M{i:03d}-RTCM32", status=s)
        for i, s in enumerate(statuses)
    }


class ParseArgumentsTests(unittest.TestCase):
    def test_options_and_filters(self):
        o = lm.parse_arguments(
            ["--caster", "gnssdata,seoul", "--caster", "nmpnt", "--mail",
             "--source", "FILE", "--protocol", "all", "ANHN", "BOEN-RTCM32"]
        )
        self.assertEqual(o.casters, ["gnssdata", "seoul", "nmpnt"])
        self.assertTrue(o.send_mail)
        self.assertEqual(o.mount_source, "file")
        self.assertEqual(o.protocol_mode, "all")
        self.assertEqual(o.filters, ["ANHN", "BOEN-RTCM32"])

    def test_errors(self):
        for args in (["--caster"], ["--caster", ", ,"], ["--caster", "--mail"], ["--loop", "0"], ["--source", "x"], ["--unknown"]):
            with self.assertRaises(ValueError, msg=args):
                lm.parse_arguments(args)


class SelectCastersTests(unittest.TestCase):
    def test_all_when_not_specified(self):
        casters = [caster("gnssdata"), caster("seoul")]
        self.assertEqual(lm.select_casters(casters, []), casters)

    def test_select_case_insensitive_keeps_config_order(self):
        casters = [caster("gnssdata"), caster("seoul"), caster("nmpnt")]
        picked = lm.select_casters(casters, ["NMPNT", "gnssdata"])
        self.assertEqual([c.name for c in picked], ["gnssdata", "nmpnt"])

    def test_unknown_name_rejected(self):
        with self.assertRaises(ValueError):
            lm.select_casters([caster("gnssdata")], ["seoul"])


class RunAllIsolationTests(unittest.TestCase):
    def test_list_exception_does_not_stop_next_caster(self):
        checker = mock.Mock()
        with mock.patch.object(lm, "build_checker", side_effect=[RuntimeError("first failed"), checker]), mock.patch.object(
            lm, "resolve_mounts", return_value=(["ANHN-RTCM32"], lm.SOURCE_SOURCETABLE)
        ) as resolve, mock.patch.object(lm, "sync_station_metadata"):
            self.assertFalse(lm.list_mounts([caster("first"), caster("second")], lm.Options()))
        self.assertEqual(resolve.call_args.args[1], "second")

    def test_exception_in_one_caster_does_not_stop_others(self):
        good = lm.CasterOutcome(name="seoul", results=results("양호"), check_time="t",
                                mount_source=lm.SOURCE_SOURCETABLE, excel_path="x.xlsx")

        def fake_run(c, options):
            if c.name == "gnssdata":
                raise RuntimeError("boom")
            return good

        with mock.patch.object(lm, "run_caster", side_effect=fake_run):
            outcomes = lm.run_all([caster("gnssdata"), caster("seoul")], lm.Options())

        self.assertEqual([o.name for o in outcomes], ["gnssdata", "seoul"])
        self.assertIn("boom", outcomes[0].error)
        self.assertFalse(outcomes[0].ok)
        self.assertTrue(outcomes[1].ok)


class RunCasterTests(unittest.TestCase):
    def setUp(self):
        self.checker = mock.Mock()
        self.checker.check_all.return_value = results("양호", "무데이터")
        self.writer = mock.Mock(output_path="out/생존현황_gnssdata.xlsx")
        patches = [
            mock.patch.object(lm, "build_checker", return_value=self.checker),
            mock.patch.object(lm, "build_writer", return_value=self.writer),
            mock.patch.object(lm, "sync_station_metadata", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_with_mounts(self, mounts, source=lm.SOURCE_SOURCETABLE, options=None):
        with mock.patch.object(lm, "resolve_mounts", return_value=(mounts, source)):
            return lm.run_caster(caster("gnssdata"), options or lm.Options())

    def test_success(self):
        o = self.run_with_mounts(["M000-RTCM32", "M001-RTCM32"])
        self.assertTrue(o.ok)
        self.assertEqual(o.excel_path, "out/생존현황_gnssdata.xlsx")
        self.assertEqual(len(o.results), 2)

    def test_excel_failure_keeps_results(self):
        self.writer.write.side_effect = PermissionError("파일이 열려 있음")
        o = self.run_with_mounts(["M000-RTCM32", "M001-RTCM32"])
        self.assertFalse(o.ok)
        self.assertIsNone(o.excel_path)
        self.assertIn("PermissionError", o.excel_error)
        self.assertEqual(len(o.results), 2)
        self.assertTrue(o.check_time)

    def test_writer_creation_failure_keeps_results(self):
        with mock.patch.object(lm, "build_writer", side_effect=OSError("creation failed")):
            o = self.run_with_mounts(["M000-RTCM32", "M001-RTCM32"])
        self.assertEqual(len(o.results), 2)
        self.assertIn("creation failed", o.excel_error)
        self.assertIsNone(o.excel_path)
        self.assertFalse(o.ok)

    def test_no_mounts_is_error_without_check(self):
        o = self.run_with_mounts([], source="")
        self.assertIn("마운트 목록", o.error)
        self.checker.check_all.assert_not_called()
        self.writer.write.assert_not_called()

    def test_filter_without_match_is_error(self):
        o = self.run_with_mounts(["M000-RTCM32"], options=lm.Options(filters=["ZZZZ"]))
        self.assertIn("필터", o.error)
        self.checker.check_all.assert_not_called()

    def test_metadata_synced_with_full_list_before_filter(self):
        with mock.patch.object(lm, "sync_station_metadata", return_value={}) as sync:
            self.run_with_mounts(
                ["M000-RTCM32", "XXXX-RTCM32"], options=lm.Options(filters=["M000"])
            )
        self.assertEqual(sync.call_args.args[1], ["M000-RTCM32", "XXXX-RTCM32"])
        self.assertEqual(self.checker.check_all.call_args.args[0], ["M000-RTCM32"])

    def test_cache_source_recorded(self):
        o = self.run_with_mounts(["M000-RTCM32"], source=lm.SOURCE_CACHE)
        self.assertEqual(o.mount_source, lm.SOURCE_CACHE)


class ExcelPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_casters_use_separate_files_and_keep_history(self):
        from openpyxl import load_workbook

        with mock.patch.dict(lm.LIVENESS_CONFIG, {"OUTPUT_DIR": self.temp.name}):
            first = lm.build_writer("first", {})
            second = lm.build_writer("second", {})
            first.write(results("양호"), "2026-11-25 10:00")
            second.write(results("오류"), "2026-11-25 10:00")
            first.write(results("무데이터"), "2026-11-25 11:00")
        for writer, expected in [(first, ["무데이터", "양호"]), (second, ["오류"])]:
            wb = load_workbook(writer.output_path)
            try:
                ws = wb["RTCM32"]
                self.assertEqual([ws.cell(2, c).value for c in range(5, 5 + len(expected))], expected)
            finally:
                wb.close()

    def test_save_and_replace_failure_preserve_existing_bytes(self):
        from liveness_excel_writer import LivenessExcelWriter

        writer = LivenessExcelWriter(self.temp.name, "history.xlsx")
        writer.write(results("양호"), "2026-11-25 10:00")
        before = Path(writer.output_path).read_bytes()
        for target in ["openpyxl.workbook.workbook.Workbook.save", "liveness_excel_writer.os.replace"]:
            with self.subTest(target=target), mock.patch(target, side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    writer.write(results("오류"), "2026-11-25 11:00")
            self.assertEqual(Path(writer.output_path).read_bytes(), before)
            self.assertEqual(list(Path(self.temp.name).glob(".liveness-*.xlsx")), [])


class StatusMeaningTests(unittest.TestCase):
    def test_503_is_no_data_and_not_retried(self):
        from ntrip import policy
        status, detail, retryable = policy.decide_response(503, False)
        self.assertEqual(status, "무데이터")
        self.assertIn("503", detail)
        self.assertIn("기준국 데이터 없음", detail)
        self.assertFalse(retryable)
        self.assertNotIn(503, policy.RETRYABLE_HTTP_CODES)


if __name__ == "__main__":
    unittest.main()
