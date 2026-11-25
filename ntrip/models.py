"""판별 결과 모델과 상태 상수.

책임: 데이터 구조 정의만 담당한다. 로직을 두지 않는다.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

# ── 엑셀 writer / mailer와 공유하는 상태값 ──
# ※ 이 4개 문자열은 누적 엑셀에 그대로 기록되고, writer가 색상 매핑에,
#    mailer가 요약 집계에 문자열 비교로 사용한다.
#    값을 바꾸면 과거 이력과 리포트 집계가 조용히 깨지므로 변경 금지.
#    세부 원인은 MountResult.detail / http_status에 담는다.
STATUS_ALIVE = "양호"
STATUS_NO_DATA = "무데이터"
STATUS_NOT_FOUND = "미등록"
STATUS_ERROR = "오류"


class NtripVersion(str, Enum):
    """검사에 사용할 요청 변형.

    실제 요청 헤더 구성은 protocol.PROFILES가 갖는다.
    """

    V1 = "v1"          # GET /mount HTTP/1.0, 버전 헤더 없음
    V2 = "v2"          # GET /mount HTTP/1.1 + Ntrip-Version: Ntrip/2.0
    CLIENT = "client"  # GET /mount HTTP/1.1, 버전 헤더 없음
                       # = gnssdata 배포 NtripClient_v2_1.exe와 동일한 요청


class ProtocolMode(str, Enum):
    """마운트 검사 시 요청 변형 선택 전략."""

    V1 = "v1"          # 1.0으로만 검사
    V2 = "v2"          # 2.0으로만 검사
    CLIENT = "client"  # 배포 클라이언트와 동일한 요청으로만 검사 (민원 재현)
    AUTO = "auto"      # 시작 시 캐스터가 답하는 버전을 1회 탐지해 고정
    BOTH = "both"      # v1 / v2를 각각 검사해 비교 (접속 2배)
    ALL = "all"        # v1 / v2 / client 셋 다 검사해 비교 (접속 3배)


# 비교 모드별로 실제 시도할 변형 목록
COMPARISON_VERSIONS = {
    ProtocolMode.BOTH: (NtripVersion.V1, NtripVersion.V2),
    ProtocolMode.ALL: (NtripVersion.V1, NtripVersion.V2, NtripVersion.CLIENT),
}


@dataclass
class ProbeResult:
    """스트림 관측 결과."""

    bytes_received: int = 0
    recv_count: int = 0
    duration_seconds: float = 0.0


@dataclass
class VersionAttempt:
    """특정 버전으로 1회 검사한 결과 (both 모드 비교용)."""

    version: NtripVersion
    status: str
    http_status: Optional[int] = None
    status_line: str = ""
    bytes_received: int = 0
    recv_count: int = 0
    detail: str = ""


@dataclass
class MountResult:
    """마운트 1개의 최종 판별 결과.

    status는 엑셀/메일이 사용하는 4개 값만 가진다.
    나머지 필드는 진단·로그 전용이며 writer는 참조하지 않는다.
    """

    mountpoint: str
    status: str
    bytes_received: int = 0
    detail: str = ""
    # ── 진단용 ──
    http_status: Optional[int] = None
    ntrip_version: str = ""
    recv_count: int = 0
    duration_seconds: float = 0.0
    attempts: int = 1
    version_attempts: List[VersionAttempt] = field(default_factory=list)
