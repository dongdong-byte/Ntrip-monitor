"""NTRIP 프로토콜 계층.

책임: 요청 바이트 생성과 응답 헤더 해석만 담당한다.
소켓을 만지지 않으므로 단위 테스트가 쉽다.
"""

import base64
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .models import NtripVersion

DEFAULT_USER_AGENT = "NTRIP LivenessChecker/2.0"

# gnssdata 배포 클라이언트(NtripClient_v2_1.exe)가 실제로 보내는 User-Agent.
# 바이너리 문자열: "User-Agent: NTRIP Client 1.0"
CLIENT_USER_AGENT = "NTRIP Client 1.0"


@dataclass(frozen=True)
class RequestProfile:
    """요청 변형의 실제 구성.

    변형이 늘어나면 PROFILES에 항목만 추가한다.
    """

    http_version: str          # "1.0" | "1.1"
    send_version_header: bool  # Ntrip-Version: Ntrip/2.0 송신 여부
    user_agent: str


PROFILES = {
    # 구형 관행. 응답은 보통 ICY 200 OK.
    NtripVersion.V1: RequestProfile("1.0", False, DEFAULT_USER_AGENT),
    # RTCM 10410.1 Ntrip 2.0 정식 요청.
    NtripVersion.V2: RequestProfile("1.1", True, DEFAULT_USER_AGENT),
    # 배포 클라이언트 재현: HTTP/1.1이지만 버전 헤더를 보내지 않는다.
    # 캐스터가 이를 1.0으로 취급해 ICY로 답할 수 있으므로 v2와 결과가 갈릴 수 있다.
    NtripVersion.CLIENT: RequestProfile("1.1", False, CLIENT_USER_AGENT),
}

# 상태 라인에서 3자리 응답 코드만 추출한다.
# "ICY 200 OK" / "HTTP/1.1 401 Unauthorized" / "SOURCETABLE 200 OK"
# HTTP/1.1의 "1.1"은 3자리 연속 숫자가 아니므로 걸리지 않는다.
_STATUS_CODE_RE = re.compile(r"\b(\d{3})\b")


def build_auth_header(username: str, password: str) -> str:
    """Basic 인증 헤더 한 줄을 만든다. 계정이 없으면 빈 문자열."""
    if not username:
        return ""
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Authorization: Basic {token}\r\n"


def build_request(
    path: str,
    host: str,
    port: int,
    version: NtripVersion,
    auth_header: str = "",
) -> bytes:
    """요청 변형에 맞는 GET 요청 바이트를 만든다."""
    profile = PROFILES[version]
    version_header = (
        "Ntrip-Version: Ntrip/2.0\r\n" if profile.send_version_header else ""
    )

    if not path.startswith("/"):
        path = "/" + path

    request = (
        f"GET {path} HTTP/{profile.http_version}\r\n"
        f"Host: {host}:{port}\r\n"
        f"{version_header}"
        f"User-Agent: {profile.user_agent}\r\n"
        f"Accept: */*\r\n"
        f"{auth_header}"
        f"Connection: close\r\n"
        f"\r\n"
    )
    return request.encode()


def parse_status_code(status_line: str) -> Optional[int]:
    """상태 라인 첫 줄에서 3자리 응답 코드를 추출한다.

    문자열 포함 검사("200" in line)는 상태 라인 밖의 숫자에
    영향을 받을 수 있으므로 정규식으로 코드만 뽑는다.
    """
    match = _STATUS_CODE_RE.search(status_line)
    return int(match.group(1)) if match else None


def parse_headers(head: bytes) -> Tuple[str, Dict[str, str]]:
    """헤더 블록을 (상태 라인, 헤더 dict)로 분해한다.

    헤더 이름은 소문자로 정규화한다.
    """
    text = head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    status_line = lines[0].strip() if lines else ""

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()

    return status_line, headers


def is_icy_response(status_line: str) -> bool:
    """NTRIP 1.0 스타일(ICY 200 OK) 응답인지."""
    return status_line.upper().startswith("ICY")


def is_chunked(headers: Dict[str, str]) -> bool:
    """Transfer-Encoding: chunked 여부."""
    return "chunked" in headers.get("transfer-encoding", "").lower()


def detected_version(status_line: str) -> NtripVersion:
    """응답 형태로 캐스터가 실제로 답한 버전을 판별한다.

    ICY로 답했다면 Ntrip-Version 헤더를 무시하고 1.0으로 응답한 것이다.
    """
    return NtripVersion.V1 if is_icy_response(status_line) else NtripVersion.V2
