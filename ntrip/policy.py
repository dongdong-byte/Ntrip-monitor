"""판정 규칙 계층.

책임: "무엇을 받았는가"를 "어떤 상태인가"로 바꾸는 규칙만 담는다.
소켓도 HTTP도 모른다. 규칙이 바뀌면 이 파일만 고친다.

주의:
    반환하는 status는 반드시 models의 4개 값 중 하나여야 한다.
    엑셀에 누적 기록되고 mailer가 문자열로 집계하기 때문이다.
    세부 원인은 detail 문자열로 전달한다.
"""

from typing import Optional, Tuple

from .models import (
    STATUS_ALIVE,
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_NO_DATA,
    ProbeResult,
)

# 일시적 장애일 수 있어 재시도 가치가 있는 HTTP 코드.
# 503은 빼 둔다. gnssdata는 기준국 데이터가 없을 때 본문 없는 503을 주며,
# 같은 마운트를 다시 붙여도 결과가 같기 때문이다.
RETRYABLE_HTTP_CODES = (500, 502, 504)

# HTTP 코드별 사람이 읽는 설명 (민원 대응 매뉴얼과 대응)
HTTP_CODE_MEANING = {
    401: "인증 실패 (계정/비밀번호)",
    404: "마운트포인트 없음",
    500: "캐스터 내부 오류",
    501: "요청 기능 미지원",
    503: "기준국 데이터 없음",
}


def describe_code(code: Optional[int]) -> str:
    """HTTP 코드를 'HTTP 401 (인증 실패...)' 형태로 설명한다."""
    if code is None:
        return "코드 불명"
    meaning = HTTP_CODE_MEANING.get(code)
    return f"HTTP {code}" + (f" ({meaning})" if meaning else "")


def decide_response(
    code: Optional[int],
    sourcetable_returned: bool,
) -> Optional[Tuple[str, str, bool]]:
    """상태 라인만으로 확정 가능한 판정.

    반환: (status, detail, retryable) 또는 None(=body 관측 필요)
    """
    # NTRIP 1.0은 없는 마운트에 404 대신 sourcetable을 돌려준다.
    if sourcetable_returned:
        return STATUS_NOT_FOUND, "Sourcetable 반환 (마운트 없음)", False

    if code == 404:
        return STATUS_NOT_FOUND, describe_code(code), False

    if code == 200:
        return None  # body를 봐야 안다

    # gnssdata는 목록에는 있으나 기준국 스트림이 없을 때 본문 없는 503을 준다.
    # GNSS-Surfer에서도 0바이트(too_few_Bytes)로 보인다. 재시도하지 않는다.
    if code == 503:
        return STATUS_NO_DATA, describe_code(code), False

    retryable = code in RETRYABLE_HTTP_CODES if code else False
    return STATUS_ERROR, describe_code(code), retryable


def decide_stream(probe: ProbeResult, satisfied: bool) -> Tuple[str, str]:
    """200 OK 이후 body 관측 결과로 판정한다.

    반환: (status, detail)
    """
    if satisfied:
        return (
            STATUS_ALIVE,
            f"{probe.bytes_received}B/{probe.recv_count}회 "
            f"({probe.duration_seconds:.1f}s)",
        )

    if probe.bytes_received > 0:
        return (
            STATUS_NO_DATA,
            f"데이터 불충분 {probe.bytes_received}B/{probe.recv_count}회 "
            f"({probe.duration_seconds:.1f}s)",
        )

    return STATUS_NO_DATA, f"데이터 0바이트 ({probe.duration_seconds:.1f}s)"


# 비교 모드에서 여러 변형 결과를 하나로 합칠 때의 우선순위.
# 앞에 있을수록 우선한다. 한 변형이라도 정상 수신되면 기준국 자체는
# 살아있다고 보고, 변형 간 차이는 detail과 로그로 남긴다.
COMBINE_PRIORITY = (STATUS_ALIVE, STATUS_NO_DATA, STATUS_ERROR, STATUS_NOT_FOUND)


def combine_versions(statuses) -> str:
    """비교 모드에서 여러 변형 결과를 엑셀용 단일 상태로 합친다."""
    found = set(statuses)
    for status in COMBINE_PRIORITY:
        if status in found:
            return status
    return STATUS_ERROR


def is_retryable_exception(exc: BaseException) -> bool:
    """네트워크 계열 일시 장애만 재시도 대상으로 본다.

    401/404/무데이터는 재시도해도 결과가 같으므로 제외한다.
    (508개 검사에서 무데이터를 재시도하면 실행 시간만 2배가 된다.)
    """
    import socket

    return isinstance(
        exc,
        (
            socket.timeout,
            ConnectionResetError,
            ConnectionRefusedError,
            ConnectionAbortedError,
        ),
    )
