"""조율 계층.

책임: 하위 계층(protocol / connection / probe / policy)을 엮어
마운트 1개를 판별하고, 버전 모드·재시도·병렬 실행을 관리한다.
판정 규칙 자체는 policy가, 수신 측정은 probe가 갖는다.
"""

import logging
import socket
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from . import policy, protocol
from .connection import BodyReader, HeaderIncomplete, open_connection, recv_header
from .models import (
    COMPARISON_VERSIONS,
    STATUS_ALIVE,
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_NO_DATA,
    MountResult,
    NtripVersion,
    ProtocolMode,
    VersionAttempt,
)
from .probe import StreamProbe
from .sourcetable import SourcetableFetcher

logger = logging.getLogger("gnss_crawler")


class NtripLivenessChecker:
    """NTRIP 캐스터 마운트포인트 생존 판별기.

    protocol_mode:
        v2     — NTRIP 2.0 정식 요청(HTTP/1.1 + Ntrip-Version)으로만 검사
        v1     — NTRIP 1.0 관행 요청(HTTP/1.0)으로만 검사
        client — gnssdata 배포 클라이언트와 동일한 요청으로만 검사 (민원 재현)
        auto   — 시작 시 캐스터가 실제로 답하는 버전을 1회 탐지해 고정
        both   — v1 / v2를 각각 검사해 비교 (접속 2배, 진단용)
        all    — v1 / v2 / client를 각각 검사해 비교 (접속 3배, 진단용)
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str = "",
        password: str = "",
        connect_timeout: float = 5.0,
        data_timeout: float = 5.0,
        max_workers: int = 30,
        protocol_mode: str = ProtocolMode.V2,
        min_data_bytes: int = 100,
        retry_count: int = 1,
        retry_delay: float = 1.0,
    ):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.data_timeout = data_timeout
        self.max_workers = max_workers
        self.protocol_mode = ProtocolMode(protocol_mode)
        self.retry_count = retry_count
        self.retry_delay = retry_delay

        self._auth_header = protocol.build_auth_header(username, password)
        self._probe = StreamProbe(
            window_seconds=data_timeout,
            min_bytes=min_data_bytes,
        )
        self._sourcetable = SourcetableFetcher(
            host, port, self._auth_header, connect_timeout
        )
        # auto 모드에서 탐지된 버전 (전체 실행 동안 고정)
        self._resolved_version: Optional[NtripVersion] = None

    # ─────────────────────────────────────────────
    # sourcetable (기존 API 유지)
    # ─────────────────────────────────────────────
    def fetch_mountpoints(self) -> List[str]:
        return self._sourcetable.fetch()

    # ─────────────────────────────────────────────
    # 캐스터 버전 탐지
    # ─────────────────────────────────────────────
    def detect_caster_version(self, sample_mount: str) -> NtripVersion:
        """마운트 하나에 2.0 요청을 보내 캐스터가 답하는 형태로 버전을 정한다.

        ICY로 답하면 Ntrip-Version 헤더를 무시하고 1.0으로 응답한 것이고,
        501이면 2.0 요청 자체를 거부한 것이다.
        """
        attempt = self._attempt_once(sample_mount, NtripVersion.V2)

        if attempt.http_status == 501:
            logger.warning(
                f"캐스터 2.0 미지원(501) → 1.0으로 검사합니다. "
                f"(탐지 마운트: {sample_mount})"
            )
            return NtripVersion.V1

        if attempt.status_line and protocol.is_icy_response(attempt.status_line):
            logger.warning(
                f"캐스터가 ICY(1.0)로 응답 → 1.0으로 검사합니다. "
                f"(응답: {attempt.status_line})"
            )
            return NtripVersion.V1

        if attempt.status_line.upper().startswith("HTTP/"):
            logger.info(
                f"캐스터 2.0 응답 확인 → 2.0으로 검사합니다. "
                f"(응답: {attempt.status_line})"
            )
            return NtripVersion.V2

        logger.warning(
            f"버전 탐지 실패({attempt.detail}) → 2.0을 기본으로 사용합니다."
        )
        return NtripVersion.V2

    # ─────────────────────────────────────────────
    # 단일 마운트 판별
    # ─────────────────────────────────────────────
    def check_mount(self, mountpoint: str) -> MountResult:
        """버전 모드에 따라 검사하고 최종 결과를 확정한다."""
        versions = COMPARISON_VERSIONS.get(self.protocol_mode)
        if versions:
            return self._check_multi(mountpoint, versions)

        version = self._version_for_run()
        attempt, attempts_used = self._attempt_with_retry(mountpoint, version)
        return self._to_result(mountpoint, attempt, attempts_used, [attempt])

    def _version_for_run(self) -> NtripVersion:
        fixed = {
            ProtocolMode.V1: NtripVersion.V1,
            ProtocolMode.V2: NtripVersion.V2,
            ProtocolMode.CLIENT: NtripVersion.CLIENT,
        }.get(self.protocol_mode)
        if fixed:
            return fixed
        # auto: 탐지 결과 사용 (미탐지면 2.0)
        return self._resolved_version or NtripVersion.V2

    def _check_multi(self, mountpoint: str, versions) -> MountResult:
        """여러 요청 변형을 각각 검사해 비교 결과를 만든다."""
        attempts = []
        total_used = 0
        for version in versions:
            attempt, used = self._attempt_with_retry(mountpoint, version)
            attempts.append(attempt)
            total_used += used

        status = policy.combine_versions(a.status for a in attempts)
        primary = next((a for a in attempts if a.status == status), attempts[0])

        detail = " / ".join(
            f"{a.version.value}={a.status}({a.detail})" for a in attempts
        )

        return MountResult(
            mountpoint=mountpoint,
            status=status,
            bytes_received=max(a.bytes_received for a in attempts),
            detail=detail,
            http_status=primary.http_status,
            ntrip_version="+".join(a.version.value for a in attempts),
            recv_count=primary.recv_count,
            attempts=total_used,
            version_attempts=attempts,
        )

    def _attempt_with_retry(
        self, mountpoint: str, version: NtripVersion
    ) -> Tuple[VersionAttempt, int]:
        """네트워크 계열 실패만 재시도한다. 반환: (결과, 총 시도 횟수)"""
        attempt = self._attempt_once(mountpoint, version)
        used = 1

        while self._is_retryable(attempt) and used <= self.retry_count:
            time.sleep(self.retry_delay)
            attempt = self._attempt_once(mountpoint, version)
            used += 1
            attempt.detail = f"{attempt.detail} (재시도 {used - 1}회)"

        return attempt, used

    @staticmethod
    def _is_retryable(attempt: VersionAttempt) -> bool:
        if attempt.status != STATUS_ERROR:
            return False
        if attempt.http_status in policy.RETRYABLE_HTTP_CODES:
            return True
        return attempt.detail.startswith("네트워크:")

    def _attempt_once(
        self, mountpoint: str, version: NtripVersion
    ) -> VersionAttempt:
        """지정 버전으로 1회 접속해 판별한다."""
        sock = None
        try:
            sock = open_connection(self.host, self.port, self.connect_timeout)
            sock.sendall(
                protocol.build_request(
                    mountpoint, self.host, self.port, version, self._auth_header
                )
            )

            deadline = time.monotonic() + self.connect_timeout
            head, body_head = recv_header(sock, deadline)
            status_line, headers = protocol.parse_headers(head)
            code = protocol.parse_status_code(status_line)

            decided = policy.decide_response(
                code, sourcetable_returned=b"SOURCETABLE" in head
            )
            if decided is not None:
                status, detail, _ = decided
                return VersionAttempt(
                    version=version,
                    status=status,
                    http_status=code,
                    status_line=status_line,
                    detail=detail,
                )

            # 200 OK → body 관측
            reader = BodyReader(sock, body_head, protocol.is_chunked(headers))
            probe = self._probe.measure(reader)
            status, detail = policy.decide_stream(
                probe, self._probe.satisfied(probe.bytes_received)
            )

            return VersionAttempt(
                version=version,
                status=status,
                http_status=code,
                status_line=status_line,
                bytes_received=probe.bytes_received,
                recv_count=probe.recv_count,
                detail=detail,
            )

        except HeaderIncomplete as e:
            return VersionAttempt(
                version=version,
                status=STATUS_ERROR,
                detail=f"네트워크: 헤더 수신 중 연결 종료 ({e})",
            )
        except (socket.timeout, OSError) as e:
            label = (
                "네트워크: " if policy.is_retryable_exception(e) else "소켓 오류: "
            )
            return VersionAttempt(
                version=version, status=STATUS_ERROR, detail=f"{label}{e}"
            )
        finally:
            if sock:
                sock.close()

    @staticmethod
    def _to_result(
        mountpoint: str,
        attempt: VersionAttempt,
        attempts_used: int,
        history: List[VersionAttempt],
    ) -> MountResult:
        return MountResult(
            mountpoint=mountpoint,
            status=attempt.status,
            bytes_received=attempt.bytes_received,
            detail=attempt.detail,
            http_status=attempt.http_status,
            ntrip_version=attempt.version.value,
            recv_count=attempt.recv_count,
            attempts=attempts_used,
            version_attempts=history,
        )

    # ─────────────────────────────────────────────
    # 전체 마운트 병렬 판별
    # ─────────────────────────────────────────────
    def check_all(self, mountpoints: Optional[List[str]] = None) -> Dict[str, MountResult]:
        if mountpoints is None:
            mountpoints = self.fetch_mountpoints()
        if not mountpoints:
            return {}

        if self.protocol_mode is ProtocolMode.AUTO:
            self._resolved_version = self.detect_caster_version(mountpoints[0])

        logger.info(
            f"마운트 {len(mountpoints)}개 생존 판별 시작 "
            f"(모드 {self.protocol_mode.value}, 병렬 {self.max_workers}, "
            f"관측창 {self.data_timeout}s, "
            f"기준 {self._probe.min_bytes}B 이상, "
            f"재시도 {self.retry_count}회)"
        )

        results: Dict[str, MountResult] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.check_mount, m): m for m in mountpoints}
            done = 0
            for future in as_completed(futures):
                result = future.result()
                results[result.mountpoint] = result
                done += 1
                if done % 50 == 0:
                    logger.info(f"진행: {done}/{len(mountpoints)}")

        self._log_summary(results)
        self._log_failure_breakdown(results)
        if self.protocol_mode in COMPARISON_VERSIONS:
            self._log_version_comparison(results)
        return results

    # ─────────────────────────────────────────────
    # 로그
    # ─────────────────────────────────────────────
    @staticmethod
    def _log_summary(results: Dict[str, MountResult]):
        alive = sum(1 for r in results.values() if r.status == STATUS_ALIVE)
        no_data = sum(1 for r in results.values() if r.status == STATUS_NO_DATA)
        retried = sum(1 for r in results.values() if r.attempts > 1)
        logger.info(
            f"판별 완료: 양호 {alive} / 무데이터 {no_data} / "
            f"기타 {len(results) - alive - no_data} (재시도 발생 {retried}건)"
        )

    @staticmethod
    def _log_failure_breakdown(results: Dict[str, MountResult]):
        breakdown = Counter(
            (r.status, r.detail or "(detail 없음)")
            for r in results.values()
            if r.status in (STATUS_ERROR, STATUS_NOT_FOUND)
        )
        if breakdown:
            logger.warning("--- 오류/미등록 원인 분포 ---")
            for (status, detail), count in breakdown.most_common(10):
                logger.warning(f"  [{status}] {detail} × {count}개")

        codes = {
            r.http_status
            for r in results.values()
            if r.http_status and r.http_status not in (200, 404)
        }
        if 401 in codes:
            logger.error(
                "→ 401 감지: NTRIP_USER / NTRIP_PASS를 확인하세요. "
                "(set은 창을 닫으면 사라집니다. setx로 영구 등록 가능)"
            )
        if 503 in codes:
            logger.error(
                "→ 503 감지: 캐스터 과부하 또는 동시 접속 제한일 수 있습니다. "
                "MAX_WORKERS를 낮춰 재시도해 보세요."
            )
        if 501 in codes:
            logger.error(
                "→ 501 감지: 캐스터가 요청한 NTRIP 버전을 지원하지 않습니다. "
                "PROTOCOL_MODE를 v1로 바꿔 확인해 보세요."
            )

    @staticmethod
    def _log_version_comparison(results: Dict[str, MountResult]):
        """비교 모드: 변형별 판정이 갈린 마운트를 노출한다.

        이 목록이 곧 '클라이언트 요청 방식에 따라 민원이 갈리는 마운트'다.
        """
        mismatched = []
        combo_counter = Counter()

        for mount, result in results.items():
            attempts = result.version_attempts
            if len(attempts) < 2:
                continue
            combo = tuple(f"{a.version.value}={a.status}" for a in attempts)
            combo_counter[combo] += 1
            if len({a.status for a in attempts}) > 1:
                mismatched.append((mount, attempts))

        if not combo_counter:
            return

        logger.info("--- 요청 변형별 결과 분포 ---")
        for combo, count in combo_counter.most_common():
            mark = "" if len({c.split("=")[1] for c in combo}) == 1 else "  ← 불일치"
            logger.info(f"  {' / '.join(combo)} × {count}개{mark}")

        if not mismatched:
            logger.info("모든 요청 변형의 판정이 일치합니다.")
            return

        logger.warning(f"--- 변형별 판정 불일치 {len(mismatched)}개 ---")
        for mount, attempts in mismatched[:20]:
            detail = " / ".join(
                f"{a.version.value}={a.status}({a.detail})" for a in attempts
            )
            logger.warning(f"  {mount}: {detail}")
