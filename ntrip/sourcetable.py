"""Sourcetable 조회 계층.

책임: 캐스터에서 마운트 목록을 얻는 것만 담당한다.
캐스터 구현마다 요청 방식이 달라 변형을 순서대로 시도한다.
"""

import logging
import socket
from typing import List, Optional

from . import protocol
from .models import NtripVersion

logger = logging.getLogger("gnss_crawler")


class SourcetableFetcher:
    """sourcetable 요청 변형을 순회하며 STR 라인에서 마운트명을 추출한다."""

    # (label, path, version)
    VARIANTS = [
        ("v2: Ntrip-Version 헤더", "/", NtripVersion.V2),
        ("v1: GET / HTTP/1.0", "/", NtripVersion.V1),
        ("v1: 가짜 마운트 요청", "/PROBE_NO_MOUNT", NtripVersion.V1),
        ("v2: 가짜 마운트 요청", "/PROBE_NO_MOUNT", NtripVersion.V2),
    ]

    def __init__(self, host: str, port: int, auth_header: str, timeout: float):
        self.host = host
        self.port = port
        self.auth_header = auth_header
        self.timeout = timeout

    def fetch(self) -> List[str]:
        logger.info(f"sourcetable 조회: {self.host}:{self.port}")

        last_raw = None
        for label, path, version in self.VARIANTS:
            raw = self._raw_request(path, version)
            if raw is None:
                logger.warning(f"[{label}] 응답 없음")
                continue
            last_raw = raw

            mounts = self.extract_mounts(raw)
            if mounts:
                logger.info(f"[{label}] 성공 → 마운트 {len(mounts)}개 발견")
                return mounts

            status_line = raw.split("\r\n", 1)[0].split("\n", 1)[0]
            logger.warning(f"[{label}] STR 없음 (응답: {status_line})")

        logger.error("모든 sourcetable 요청 변형 실패.")
        self._log_diagnosis(last_raw)
        return []

    @staticmethod
    def extract_mounts(raw: str) -> List[str]:
        """STR;마운트명;... 라인에서 두 번째 필드를 모은다."""
        mounts = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("STR;"):
                fields = line.split(";")
                if len(fields) > 1 and fields[1]:
                    mounts.append(fields[1])
        return sorted(set(mounts))

    @staticmethod
    def _log_diagnosis(last_raw: Optional[str]):
        if not last_raw:
            return
        preview = last_raw[:500].replace("\r", "")
        logger.error(
            f"마지막 응답 원문(앞 500자):\n----------\n{preview}\n----------"
        )
        if "401" in last_raw[:200] or "Unauthorized" in last_raw[:500]:
            logger.error(
                "→ 401 인증 오류입니다. NTRIP_USER / NTRIP_PASS 환경변수 "
                "또는 LIVENESS_CONFIG의 USERNAME/PASSWORD를 설정하세요."
            )

    def _raw_request(self, path: str, version: NtripVersion) -> Optional[str]:
        """요청 후 응답 전체를 텍스트로 반환한다."""
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            ) as sock:
                sock.sendall(
                    protocol.build_request(
                        path, self.host, self.port, version, self.auth_header
                    )
                )
                sock.settimeout(self.timeout)

                chunks = []
                while True:
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks).decode(errors="replace")
        except OSError as e:
            logger.error(f"요청 실패 ({path}): {e}")
            return None
