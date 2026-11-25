"""하위 호환 shim.

기존 코드는 `from liveness_checker import NtripLivenessChecker`로 임포트한다.
실제 구현은 ntrip 패키지로 옮겼으며, 이 파일은 기존 임포트 경로를 유지한다.
liveness_main.py / liveness_mailer.py / liveness_excel_writer.py는 수정 불필요.
"""

from ntrip import (  # noqa: F401
    STATUS_ALIVE,
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_NO_DATA,
    MountResult,
    NtripLivenessChecker,
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
