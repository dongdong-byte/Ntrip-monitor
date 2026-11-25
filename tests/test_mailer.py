"""요약 메일 작성·발송 테스트. 실제 SMTP 서버에 접속하지 않는다."""

import smtplib
import ssl
import tempfile
import unittest
from email import message_from_bytes
from email.policy import default
from pathlib import Path
from unittest import mock

import liveness_main as lm
from config_loader import CasterConfig, SmtpConfig
from liveness_checker import MountResult
from liveness_mailer import LivenessMailer, build_report

SMTP = SmtpConfig(user="sender@example.com", password="pw", mail_to=["to@example.com"])


def res(**statuses):
    return {
        m: MountResult(mountpoint=m, status=s, detail=f"d-{m}")
        for m, s in statuses.items()
    }


def outcome(name="gnssdata", results=None, **kw):
    base = dict(name=name, results=results or {}, check_time="2026-11-25 17:03",
                mount_source="Sourcetable")
    base.update(kw)
    return lm.CasterOutcome(**base)


class BuildReportTests(unittest.TestCase):
    def test_normal_single_caster(self):
        o = outcome(results={
            "ANHN-RTCM32": MountResult("ANHN-RTCM32", "양호"),
            "GGEO-RTCM32": MountResult("GGEO-RTCM32", "무데이터", detail="HTTP 503 (기준국 데이터 없음)"),
            "MKPO-RTCM32": MountResult("MKPO-RTCM32", "미등록", detail="HTTP 404"),
        }, excel_path="x.xlsx")
        subject, body = build_report([o], "2026-11-25 17:00")
        self.assertEqual(subject, "[NTRIP 생존현황] 2026-11-25 17:00 — 양호 1/3")
        self.assertIn("마운트 3개 중 양호 1개 / 이상 2개 (무데이터 1, 오류 0, 미등록 1)", body)
        self.assertIn("=== [gnssdata] 양호 1/3", body)
        self.assertIn("  - GGEO-RTCM32  (HTTP 503 (기준국 데이터 없음))", body)
        self.assertNotIn("확인 필요", body)
        self.assertNotIn("ANHN-RTCM32", body)  # 양호 마운트는 나열하지 않음

    def test_all_alive(self):
        o = outcome(results={"ANHN-RTCM32": MountResult("ANHN-RTCM32", "양호")}, excel_path="x")
        _, body = build_report([o], "t")
        self.assertIn("이상 마운트 없음", body)

    def test_notices_put_on_top_and_subject_flagged(self):
        outcomes = [
            outcome("gnssdata", results={"A-RTCM32": MountResult("A-RTCM32", "양호")},
                    excel_error="PermissionError: 열려 있음", mount_source="보조 목록"),
            outcome("seoul", error="검사할 마운트 목록을 확보하지 못함"),
        ]
        subject, body = build_report(outcomes, "t")
        self.assertTrue(subject.startswith("[확인 필요] [NTRIP 생존현황]"))
        top = body.split("=== [")[0]
        self.assertIn("[gnssdata] 엑셀 저장 실패 → 이번 결과는 첨부하지 않음", top)
        self.assertIn("[gnssdata] 보조 목록으로 검사함", top)
        self.assertIn("[seoul] 검사 불가: 검사할 마운트 목록을 확보하지 못함", top)
        self.assertIn("=== [seoul] 검사 불가 ===", body)
        # 검사 결과는 엑셀 저장 실패와 무관하게 집계
        self.assertIn("캐스터 2개, 마운트 1개 중 양호 1개", body)

    def test_all_casters_failed(self):
        subject, body = build_report([outcome("gnssdata", error="예외")], "t")
        self.assertIn("검사 불가", subject)
        self.assertIn("모두 검사하지 못했습니다", body)


class SendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.xlsx = Path(self.temp.name) / "생존현황_gnssdata.xlsx"
        self.xlsx.write_bytes(b"PK fake xlsx")

    def send(self, outcomes, **smtp_side_effect):
        with mock.patch("liveness_mailer.smtplib.SMTP") as smtp_cls:
            server = smtp_cls.return_value.__enter__.return_value
            server.send_message.return_value = {}
            for name, effect in smtp_side_effect.items():
                getattr(server, name).side_effect = effect
            ok = LivenessMailer(SMTP).send_report(outcomes, "2026-11-25 17:00")
        return ok, smtp_cls, server

    def test_naverworks_starttls_login_and_attachment(self):
        outcomes = [
            outcome("gnssdata", res(**{"A-RTCM32": "양호"}), excel_path=str(self.xlsx)),
            outcome("seoul", res(**{"B-RTCM32": "양호"}), excel_error="x"),  # 첨부 제외
        ]
        ok, smtp_cls, server = self.send(outcomes)
        self.assertTrue(ok)
        smtp_cls.assert_called_once_with("smtp.worksmobile.com", 587, timeout=30.0)
        server.starttls.assert_called_once()
        context = server.starttls.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        server.login.assert_called_once_with("sender@example.com", "pw")

        msg = server.send_message.call_args.args[0]
        parsed = message_from_bytes(msg.as_bytes(), policy=default)
        self.assertEqual(parsed["To"], "to@example.com")
        names = [p.get_filename() for p in parsed.iter_attachments()]
        self.assertEqual(names, ["생존현황_gnssdata.xlsx"])  # 한글 파일명 보존
        self.assertIn("[확인 필요]", parsed["Subject"])

    def test_auth_failure_returns_false(self):
        ok, _, _ = self.send(
            [outcome(results=res(**{"A-RTCM32": "양호"}))],
            login=smtplib.SMTPAuthenticationError(535, b"auth failed"),
        )
        self.assertFalse(ok)

    def test_network_failure_returns_false(self):
        with mock.patch("liveness_mailer.smtplib.SMTP", side_effect=OSError("no route")):
            ok = LivenessMailer(SMTP).send_report([outcome(error="x")], "t")
        self.assertFalse(ok)

    def test_missing_attachment_sends_notice_and_returns_failure(self):
        ok, _, server = self.send(
            [outcome(results=res(**{"A-RTCM32": "양호"}), excel_path=str(self.xlsx) + ".gone")]
        )
        self.assertFalse(ok)
        msg = server.send_message.call_args.args[0]
        self.assertEqual(list(msg.iter_attachments()), [])
        self.assertIn("첨부 실패", msg["Subject"])
        self.assertIn(".gone", msg.get_body(preferencelist=("plain",)).get_content())

    def test_missing_attachment_keeps_other_attachment(self):
        ok, _, server = self.send([
            outcome("first", res(**{"A": "양호"}), excel_path=str(self.xlsx)),
            outcome("second", res(**{"B": "양호"}), excel_path=str(self.xlsx) + ".gone"),
        ])
        self.assertFalse(ok)
        msg = server.send_message.call_args.args[0]
        self.assertEqual(len(list(msg.iter_attachments())), 1)
        self.assertIn("첨부 실패", msg.get_body(preferencelist=("plain",)).get_content())

    def test_partial_recipient_refusal_returns_failure(self):
        ok, _, server = self.send(
            [outcome(error="검사 실패")],
            send_message=lambda msg: {"blocked@example.com": (550, b"denied")},
        )
        self.assertFalse(ok)
        server.send_message.assert_called_once()

    def test_save_failure_never_attaches_stale_path(self):
        ok, _, server = self.send([
            outcome(results=res(**{"A": "양호"}), excel_path=str(self.xlsx), excel_error="disk")
        ])
        self.assertTrue(ok)
        self.assertEqual(list(server.send_message.call_args.args[0].iter_attachments()), [])

    def test_certificate_failure_returns_false_before_login(self):
        ok, _, server = self.send(
            [outcome(error="x")], starttls=ssl.SSLCertVerificationError("invalid certificate")
        )
        self.assertFalse(ok)
        server.login.assert_not_called()
        server.send_message.assert_not_called()

    def test_send_test_mail(self):
        with mock.patch("liveness_mailer.smtplib.SMTP") as smtp_cls:
            server = smtp_cls.return_value.__enter__.return_value
            server.send_message.return_value = {}
            self.assertTrue(LivenessMailer(SMTP).send_test("t"))
        self.assertIn("테스트", server.send_message.call_args.args[0]["Subject"])


class MainExitCodeTests(unittest.TestCase):
    """cron이 실패를 알 수 있도록 종료 코드를 확인한다."""

    def run_main(self, argv, outcomes, mail_ok=True):
        config = mock.Mock(
            casters=[CasterConfig("gnssdata", "h.example", 2101, "u", "p")], smtp=SMTP
        )
        with mock.patch.object(lm, "setup_logging"), \
             mock.patch.object(lm, "load_config", return_value=config), \
             mock.patch.object(lm, "run_all", return_value=outcomes), \
             mock.patch.object(lm, "send_mail", return_value=mail_ok) as send, \
             mock.patch("sys.argv", ["liveness_main.py", *argv]):
            try:
                lm.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, send

    def good(self):
        return [outcome(results=res(**{"A-RTCM32": "양호"}), excel_path="x.xlsx")]

    def test_success_with_mail(self):
        code, send = self.run_main(["--mail"], self.good())
        self.assertEqual(code, 0)
        send.assert_called_once()

    def test_mail_failure_exits_1(self):
        code, _ = self.run_main(["--mail"], self.good(), mail_ok=False)
        self.assertEqual(code, 1)

    def test_caster_failure_still_mails_and_exits_1(self):
        code, send = self.run_main(["--mail"], [outcome(error="검사 불가")])
        self.assertEqual(code, 1)
        send.assert_called_once()

    def test_no_mail_flag_no_mail(self):
        code, send = self.run_main([], self.good())
        self.assertEqual(code, 0)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
