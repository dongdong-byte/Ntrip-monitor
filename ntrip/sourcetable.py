"""Sourcetable 조회 계층.

책임: 캐스터에서 마운트 목록을 얻는 것만 담당한다.
캐스터 구현마다 요청 방식이 달라 변형을 순서대로 시도한다.

완전 수신 검증:
    응답을 일부만 받은 상태(수신 중 타임아웃, 연결 끊김)에서 STR 줄 몇 개를
    읽었다고 성공으로 처리하면, 그 불완전한 목록이 보조 파일(캐시)에 저장되어
    감시 대상이 조용히 줄어든다. 그래서 다음을 모두 만족할 때만 성공으로 본다.
      - 응답 코드 200
      - 본문에 ENDSOURCETABLE 줄이 있음
      - Content-Length 헤더가 있으면 받은 본문 크기가 그 이상
      - chunked 전송이면 종료 청크(0)까지 정상 디코딩
"""

import logging
import socket
import time
from dataclasses import dataclass
from typing import List, Optional

from . import protocol
from .chunked import ChunkedDecoder
from .models import NtripVersion

logger = logging.getLogger("gnss_crawler")

END_MARKER = "ENDSOURCETABLE"

# 비정상적으로 큰 응답을 무한정 받지 않기 위한 상한.
# gnssdata Sourcetable은 약 65KB다.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass
class SourcetableResponse:
    """Sourcetable 응답 1건의 해석 결과."""

    status_line: str
    complete: bool
    reason: str          # 성공이면 확인한 근거, 실패면 실패 이유
    body: str = ""
    mounts: Optional[List[str]] = None


def parse_response(raw: bytes) -> SourcetableResponse:
    """응답 원문을 헤더/본문으로 나누고 완전 수신 여부를 판정한다.

    소켓을 모르므로 단위 테스트가 쉽다.
    """
    head, sep, body_bytes = raw.partition(b"\r\n\r\n")
    status_line, headers = protocol.parse_headers(head)

    if not sep:
        return SourcetableResponse(status_line, False, "헤더 끝(빈 줄)을 받지 못함")

    code = protocol.parse_status_code(status_line)
    if code != 200:
        return SourcetableResponse(
            status_line, False, f"응답 코드 {code if code else '불명'}"
        )

    checks = []

    if protocol.is_chunked(headers):
        decoder = ChunkedDecoder()
        body_bytes = decoder.feed(body_bytes)
        if decoder.malformed:
            return SourcetableResponse(status_line, False, "chunked 디코딩 실패")
        if not decoder.finished:
            return SourcetableResponse(
                status_line, False, "chunked 종료 청크를 받지 못함 (수신 중단)"
            )
        checks.append("chunked 종료")
    else:
        length_text = headers.get("content-length")
        if length_text is not None:
            try:
                if not length_text.isascii() or not length_text.isdecimal():
                    raise ValueError
                expected = int(length_text)
            except ValueError:
                return SourcetableResponse(
                    status_line, False, f"Content-Length 값 오류: {length_text}"
                )
            if len(body_bytes) < expected:
                return SourcetableResponse(
                    status_line,
                    False,
                    f"본문 {len(body_bytes)}B < Content-Length {expected}B (수신 중단)",
                )
            body_bytes = body_bytes[:expected]
            checks.append(f"Content-Length {expected}B 일치")

    body = body_bytes.decode("utf-8", errors="replace")

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or lines[-1] != END_MARKER or lines.count(END_MARKER) != 1:
        return SourcetableResponse(
            status_line, False, f"마지막 {END_MARKER} 줄 없음 또는 위치 오류", body
        )
    checks.insert(0, END_MARKER)

    mounts = extract_mounts(body)
    if not mounts:
        return SourcetableResponse(status_line, False, "STR 줄 없음", body)

    return SourcetableResponse(
        status_line, True, ", ".join(checks), body, mounts
    )


def extract_mounts(text: str) -> List[str]:
    """STR;마운트명;... 라인에서 두 번째 필드를 모은다."""
    mounts = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("STR;"):
            fields = line.split(";")
            if len(fields) > 1 and fields[1]:
                mounts.append(fields[1])
    return sorted(set(mounts))


class SourcetableFetcher:
    """sourcetable 요청 변형을 순회하며 완전히 수신한 목록만 돌려준다.

    인증 헤더 없이 먼저 요청한다:
        Sourcetable은 공개 정보다. gnssdata 캐스터는 Authorization 헤더가
        붙으면 계정이 맞든 틀리든 GET /에 404를 돌려주고, 헤더가 없을 때만
        Sourcetable을 준다 (2026-11-25 확인). 예전 코드 주석의
        "GET /에 404"는 이 동작 때문이었다.
        인증이 있어야 목록을 주는 캐스터를 위해, 인증 없는 요청이 모두
        실패하면 인증을 붙여 다시 시도한다.
    """

    # (label, path, version, 인증 헤더 포함 여부)
    VARIANTS = [
        ("v2, 인증 없음", "/", NtripVersion.V2, False),
        ("v1, 인증 없음", "/", NtripVersion.V1, False),
        ("v2, 인증 포함", "/", NtripVersion.V2, True),
        ("v1, 인증 포함", "/", NtripVersion.V1, True),
        ("v1 가짜 마운트, 인증 포함", "/PROBE_NO_MOUNT", NtripVersion.V1, True),
        ("v2 가짜 마운트, 인증 포함", "/PROBE_NO_MOUNT", NtripVersion.V2, True),
    ]

    def __init__(self, host: str, port: int, auth_header: str, timeout: float):
        self.host = host
        self.port = port
        self.auth_header = auth_header
        self.timeout = timeout

    def fetch(self) -> List[str]:
        """완전 수신이 확인된 마운트 목록. 모든 변형이 실패하면 빈 목록."""
        logger.info(f"sourcetable 조회: {self.host}:{self.port}")

        last_raw = None
        for label, path, version, with_auth in self.VARIANTS:
            if with_auth and not self.auth_header:
                continue
            raw = self._raw_request(path, version, with_auth)
            if raw is None:
                logger.warning(f"[{label}] 응답 없음")
                continue
            last_raw = raw

            response = parse_response(raw)
            if response.complete:
                logger.info(
                    f"[{label}] 성공 → 마운트 {len(response.mounts)}개 "
                    f"(완전 수신 확인: {response.reason})"
                )
                return response.mounts

            logger.warning(
                f"[{label}] 실패: {response.reason} "
                f"(응답: {response.status_line or '없음'})"
            )

        logger.error("모든 sourcetable 요청 변형 실패.")
        self._log_diagnosis(last_raw)
        return []

    @staticmethod
    def _log_diagnosis(last_raw: Optional[bytes]):
        if not last_raw:
            return
        text = last_raw.decode("utf-8", errors="replace")
        preview = text[:500].replace("\r", "")
        logger.error(
            f"마지막 응답 원문(앞 500자):\n----------\n{preview}\n----------"
        )
        if "401" in text[:200] or "Unauthorized" in text[:500]:
            logger.error(
                "→ 401 인증 오류입니다. config.ini의 캐스터 user / password를 "
                "확인하세요."
            )

    def _raw_request(
        self, path: str, version: NtripVersion, with_auth: bool
    ) -> Optional[bytes]:
        """요청 후 연결이 닫히거나 타임아웃될 때까지 받은 원문을 반환한다.

        여기서는 완전성을 판단하지 않는다. (parse_response의 책임)
        """
        auth = self.auth_header if with_auth else ""
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as sock:
                sock.sendall(
                    protocol.build_request(
                        path, self.host, self.port, version, auth
                    )
                )
                sock.settimeout(self.timeout)

                chunks = []
                total = 0
                deadline = time.monotonic() + self.timeout
                while total < MAX_RESPONSE_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    sock.settimeout(remaining)
                    try:
                        chunk = sock.recv(min(8192, MAX_RESPONSE_BYTES - total))
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                if total >= MAX_RESPONSE_BYTES:
                    logger.error("Sourcetable 응답 크기 상한 초과")
                    return None
                return b"".join(chunks)
        except OSError as e:
            logger.error(f"요청 실패 ({path}): {e}")
            return None
