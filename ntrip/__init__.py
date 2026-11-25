"""NTRIP 마운트포인트 생존 판별 패키지.

계층 구조 (의존 방향: 위 → 아래):

    checker.py      조율: 버전 모드, 재시도, 병렬 실행
      ├─ policy.py      판정 규칙 (무엇을 어떤 상태로 볼 것인가)
      ├─ probe.py       스트림 지속성 관측  ← RTCM 프레임 검증 확장 지점
      ├─ sourcetable.py 마운트 목록 조회
      ├─ connection.py  소켓/헤더 수신/body 리더
      │    └─ chunked.py  chunked 디코더
      ├─ protocol.py    요청 생성 / 응답 헤더 파싱
      └─ models.py      데이터 구조 (로직 없음)

하위 모듈은 상위를 import하지 않는다.
"""

from .checker import NtripLivenessChecker
from .models import (
    STATUS_ALIVE,
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_NO_DATA,
    MountResult,
    NtripVersion,
    ProtocolMode,
)

__all__ = [
    "NtripLivenessChecker",
    "MountResult",
    "NtripVersion",
    "ProtocolMode",
    "STATUS_ALIVE",
    "STATUS_NO_DATA",
    "STATUS_NOT_FOUND",
    "STATUS_ERROR",
]
