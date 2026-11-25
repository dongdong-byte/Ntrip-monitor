"""마운트포인트 생존 판별 결과를 이메일로 발송"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gnss_crawler")

# liveness_checker의 상태값과 동일해야 함
STATUS_ALIVE = "양호"
STATUS_NO_DATA = "무데이터"
STATUS_NOT_FOUND = "미등록"
STATUS_ERROR = "오류"

PROBLEM_STATUSES = (STATUS_NO_DATA, STATUS_ERROR, STATUS_NOT_FOUND)


class LivenessMailer:
    """
    책임: 판별 결과 dict → 요약 메일 작성/발송

    메일 구성 원칙:
      - 제목과 본문 첫 줄에 한 줄 요약 (수신자가 열자마자 상황 파악)
      - 그 아래 문제 마운트 상세 목록
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        sender: str,
        recipients: list,
        use_ssl: bool = False,
        timeout: float = 15.0,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.sender = sender
        self.recipients = recipients
        self.use_ssl = use_ssl
        self.timeout = timeout

    # ─────────────────────────────────────────────
    # 발송
    # ─────────────────────────────────────────────
    def send(
        self,
        results: dict,
        check_time: str,
        attach_path: Optional[str] = None,
    ) -> bool:
        if not self.recipients:
            logger.error("메일 수신자(MAIL_TO)가 비어 있습니다. 발송 생략.")
            return False

        subject, body = self._build_message(results, check_time)

        msg = MIMEMultipart()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if attach_path:
            self._attach_file(msg, attach_path)

        try:
            if self.use_ssl:
                server = smtplib.SMTP_SSL(
                    self.smtp_host, self.smtp_port, timeout=self.timeout
                )
            else:
                server = smtplib.SMTP(
                    self.smtp_host, self.smtp_port, timeout=self.timeout
                )
                server.starttls()

            with server:
                server.ehlo()
                if self.username and server.has_extn("auth"):
                    server.login(self.username, self.password)
                elif self.username:
                    logger.info(
                        "SMTP 서버가 AUTH를 지원하지 않아 로그인 없이 발송합니다."
                    )
                server.sendmail(self.sender, self.recipients, msg.as_string())

            logger.info(f"메일 발송 완료 → {', '.join(self.recipients)}")
            return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(
                f"메일 인증 실패: {e} "
                "(SMTP_USER/SMTP_PASS 확인. 네이버/구글은 앱 비밀번호 필요)"
            )
        except (smtplib.SMTPException, OSError) as e:
            logger.error(f"메일 발송 실패: {e}")
        return False

    # ─────────────────────────────────────────────
    # 메일 내용 작성
    # ─────────────────────────────────────────────
    def _build_message(self, results: dict, check_time: str) -> tuple:
        total = len(results)
        counts = {s: 0 for s in (STATUS_ALIVE, *PROBLEM_STATUSES)}
        for r in results.values():
            if r.status in counts:
                counts[r.status] += 1

        alive = counts[STATUS_ALIVE]
        problem_total = total - alive

        # 한 줄 요약 (제목/본문 첫 줄 공용)
        one_liner = (
            f"마운트 {total}개 중 양호 {alive}개 / 이상 {problem_total}개 "
            f"(무데이터 {counts[STATUS_NO_DATA]}, "
            f"오류 {counts[STATUS_ERROR]}, "
            f"미등록 {counts[STATUS_NOT_FOUND]})"
        )

        subject = f"[NTRIP 생존현황] {check_time} — 양호 {alive}/{total}"

        lines = [
            f"■ 요약: {one_liner}",
            f"■ 검사시각: {check_time}",
            "",
        ]

        # 문제 마운트 상세 (상태별 → 관측소코드순)
        for status in PROBLEM_STATUSES:
            bad = sorted(
                m for m, r in results.items() if r.status == status
            )
            if not bad:
                continue
            lines.append(f"▶ {status} ({len(bad)}개)")
            for mount in bad:
                detail = results[mount].detail
                lines.append(f"  - {mount}" + (f"  ({detail})" if detail else ""))
            lines.append("")

        if problem_total == 0:
            lines.append("이상 마운트 없음. 전체 정상 수신 중입니다.")
            lines.append("")

        lines.append("※ 본 메일은 NTRIP 생존 검사 프로그램에서 자동 발송되었습니다.")
        return subject, "\n".join(lines)

    @staticmethod
    def _attach_file(msg: MIMEMultipart, path: str):
        file_path = Path(path)
        if not file_path.exists():
            logger.warning(f"첨부 파일 없음, 첨부 생략: {file_path}")
            return
        with file_path.open("rb") as f:
            part = MIMEApplication(f.read(), Name=file_path.name)
        part["Content-Disposition"] = f'attachment; filename="{file_path.name}"'
        msg.attach(part)
