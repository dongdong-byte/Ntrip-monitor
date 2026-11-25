"""검사 결과를 메일 1통으로 요약해 발송한다.

발송 방식: config.ini [smtp] 계정으로 네이버웍스 SMTP(587, STARTTLS)에 로그인해 보낸다.

메일 구성 원칙:
  - 제목과 본문 첫 줄에 한 줄 요약 (수신자가 열자마자 상황 파악)
  - 검사 불가·엑셀 저장 실패·보조 목록 사용은 맨 위 "확인 필요"에 모은다
  - 그 아래 캐스터별로 이상 마운트 상세
  - 이번 결과 저장에 성공한 엑셀만 첨부한다 (저장 실패 시 과거 파일을 이번 결과처럼 첨부하지 않음)
  - 한글명·소속기관 미등록은 메일에 경고하지 않는다

outcomes 항목은 liveness_main.CasterOutcome과 같은 속성을 가진다:
  name, results, check_time, mount_source, excel_path, error, excel_error
"""

import logging
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import List, Tuple

from config_loader import SmtpConfig
from liveness_checker import (
    STATUS_ALIVE,
    STATUS_ERROR,
    STATUS_NO_DATA,
    STATUS_NOT_FOUND,
)

logger = logging.getLogger("gnss_crawler")

PROBLEM_STATUSES = (STATUS_NO_DATA, STATUS_ERROR, STATUS_NOT_FOUND)
SUBJECT_PREFIX = "[NTRIP 생존현황]"
CACHE_SOURCE_LABEL = "보조 목록"
XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class LivenessMailer:
    """책임: 실행 결과 목록 → 요약 메일 1통 작성·발송."""

    def __init__(self, smtp: SmtpConfig, timeout: float = 30.0):
        self.smtp = smtp
        self.timeout = timeout

    # ─────────────────────────────────────────────
    # 발송
    # ─────────────────────────────────────────────
    def send_report(self, outcomes: list, run_time: str) -> bool:
        """검사 요약 메일을 보낸다. 반환: 발송 성공 여부."""
        subject, body = build_report(outcomes, run_time)
        attachments = [
            o.excel_path for o in outcomes
            if o.excel_path and not o.error and not o.excel_error
        ]
        return self._send(subject, body, attachments)

    def send_test(self, run_time: str) -> bool:
        """SMTP 설정 확인용 짧은 메일. 검사·엑셀 없이 발송만 확인한다."""
        subject = f"{SUBJECT_PREFIX} 메일 발송 테스트 {run_time}"
        body = (
            "NTRIP 생존 감시 프로그램의 메일 설정 확인용 테스트 메일입니다.\n"
            f"■ 발송 시각: {run_time}\n"
            f"■ SMTP: {self.smtp.host}:{self.smtp.port} (STARTTLS)\n"
            f"■ 발신: {self.smtp.user}\n"
            "\n※ 이 메일이 보이면 config.ini [smtp] 설정이 정상입니다."
        )
        return self._send(subject, body, [])

    def _send(self, subject: str, body: str, attachments: List[str]) -> bool:
        msg = EmailMessage()
        msg["From"] = self.smtp.user
        msg["To"] = ", ".join(self.smtp.mail_to)
        msg.set_content(body, charset="utf-8")

        failed_attachments = []
        attached_count = 0
        for path in attachments:
            if self._attach(msg, path):
                attached_count += 1
            else:
                failed_attachments.append(Path(path).name)

        if failed_attachments:
            subject = f"[첨부 실패] {subject}"
            notice = "■ 첨부 실패: " + ", ".join(failed_attachments)
            # multipart 전체를 재설정하면 성공한 첨부까지 사라진다.
            text_part = msg.get_body(preferencelist=("plain",))
            text_part.set_content(notice + "\n\n" + body, charset="utf-8")
        msg["Subject"] = subject

        try:
            # with 블록: STARTTLS나 로그인 중 실패해도 연결을 닫는다.
            with smtplib.SMTP(
                self.smtp.host, self.smtp.port, timeout=self.timeout
            ) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(self.smtp.user, self.smtp.password)
                refused = server.send_message(msg)
                if refused:
                    logger.error(
                        "일부 수신자에게 메일 발송 실패: %s (나머지 수신자는 SMTP 서버가 접수)",
                        ", ".join(refused),
                    )
                    return False
        except smtplib.SMTPAuthenticationError as exc:
            logger.error(
                f"메일 인증 실패: {exc} "
                "(config.ini [smtp] user / password 확인. "
                "네이버웍스 외부 앱 비밀번호를 써야 합니다)"
            )
            return False
        except (smtplib.SMTPException, OSError) as exc:
            logger.error(f"메일 발송 실패: {type(exc).__name__}: {exc}")
            return False

        logger.info(
            f"메일 발송 완료 → {', '.join(self.smtp.mail_to)} "
            f"(첨부 {attached_count}개)"
        )
        return not failed_attachments

    @staticmethod
    def _attach(msg: EmailMessage, path: str) -> bool:
        file_path = Path(path)
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            logger.warning(f"첨부 파일을 읽지 못해 생략: {file_path} ({exc})")
            return False

        if file_path.suffix.lower() == ".xlsx":
            maintype, subtype = XLSX_MIME
        else:
            guessed, _ = mimetypes.guess_type(file_path.name)
            maintype, subtype = (guessed or "application/octet-stream").split("/", 1)

        # add_attachment는 한글 파일명을 RFC 2231 방식으로 인코딩한다.
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=file_path.name
        )
        return True


# ─────────────────────────────────────────────
# 메일 내용 작성 (발송과 분리 → 단위 테스트 가능)
# ─────────────────────────────────────────────
def count_statuses(results: dict) -> dict:
    counts = {s: 0 for s in (STATUS_ALIVE, *PROBLEM_STATUSES)}
    for r in results.values():
        if r.status in counts:
            counts[r.status] += 1
    return counts


def collect_notices(outcomes: list) -> List[str]:
    """맨 위 '확인 필요'에 올릴 항목. 없으면 빈 목록."""
    notices = []
    for o in outcomes:
        if o.error:
            notices.append(f"[{o.name}] 검사 불가: {o.error}")
            continue
        if o.excel_error:
            notices.append(
                f"[{o.name}] 엑셀 저장 실패 → 이번 결과는 첨부하지 않음 ({o.excel_error})"
            )
        if o.mount_source == CACHE_SOURCE_LABEL:
            notices.append(
                f"[{o.name}] 보조 목록으로 검사함 (자동 대체 또는 --source file 지정)"
            )
    return notices


def build_report(outcomes: list, run_time: str) -> Tuple[str, str]:
    """(제목, 본문)을 만든다."""
    checked = [o for o in outcomes if o.results and not o.error]
    total = sum(len(o.results) for o in checked)
    counts = {s: 0 for s in (STATUS_ALIVE, *PROBLEM_STATUSES)}
    for o in checked:
        for status, n in count_statuses(o.results).items():
            counts[status] += n
    alive = counts[STATUS_ALIVE]
    problem_total = total - alive
    notices = collect_notices(outcomes)

    caster_label = f"캐스터 {len(outcomes)}개"
    if checked:
        one_liner = (
            f"{caster_label}, 마운트 {total}개 중 양호 {alive}개 / 이상 {problem_total}개 "
            f"(무데이터 {counts[STATUS_NO_DATA]}, "
            f"오류 {counts[STATUS_ERROR]}, "
            f"미등록 {counts[STATUS_NOT_FOUND]})"
        )
        subject = f"{SUBJECT_PREFIX} {run_time} — 양호 {alive}/{total}"
    else:
        one_liner = f"{caster_label} 모두 검사하지 못했습니다."
        subject = f"{SUBJECT_PREFIX} {run_time} — 검사 불가"

    if notices:
        subject = f"[확인 필요] {subject}"

    lines = [
        f"■ 요약: {one_liner}",
        f"■ 실행 시각: {run_time}",
        "",
    ]

    if notices:
        lines.append("■ 확인 필요")
        lines.extend(f"  - {n}" for n in notices)
        lines.append("")

    for o in outcomes:
        lines.extend(_caster_section(o))

    lines.append("※ 첨부: 이번 결과가 저장된 캐스터별 누적 엑셀")
    lines.append("※ 본 메일은 NTRIP 생존 검사 프로그램에서 자동 발송되었습니다.")
    return subject, "\n".join(lines)


def _caster_section(o) -> List[str]:
    if o.error or not o.results:
        return [f"=== [{o.name}] 검사 불가 ===", f"  {o.error or '판별 결과 없음'}", ""]

    counts = count_statuses(o.results)
    total = len(o.results)
    header = (
        f"=== [{o.name}] 양호 {counts[STATUS_ALIVE]}/{total} "
        f"(검사 {o.check_time}, 목록 출처: {o.mount_source or '-'}) ==="
    )
    lines = [header]

    any_problem = False
    for status in PROBLEM_STATUSES:
        bad = sorted(m for m, r in o.results.items() if r.status == status)
        if not bad:
            continue
        any_problem = True
        lines.append(f"▶ {status} ({len(bad)}개)")
        for mount in bad:
            detail = o.results[mount].detail
            lines.append(f"  - {mount}" + (f"  ({detail})" if detail else ""))

    if not any_problem:
        lines.append("  이상 마운트 없음. 전체 정상 수신 중입니다.")

    lines.append("")
    return lines
