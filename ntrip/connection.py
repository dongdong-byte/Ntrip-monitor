"""전송 계층.

책임: 소켓 연결, 헤더 종료까지의 수신, 헤더/body 분리, body 읽기.
판정 규칙이나 상태 문자열을 알지 못한다.
"""

import re
import socket
import time
from typing import Optional, Tuple

from .chunked import ChunkedDecoder

# 헤더 종료(\r\n\r\n)를 찾기 위해 읽어들일 최대 바이트.
# HTTP 응답에서 헤더가 비정상적으로 길거나, 헤더 종료 없이 바이너리가
# 흘러들 때의 최후 방어선이다.
# ICY 응답은 _split_icy()에서 첫 줄로 즉시 확정하므로 이 한도에
# 의존하지 않는다. (저속 스트림에서는 이 한도가 쌓이기 전에 헤더
#  타임아웃이 먼저 발생하므로 방어선으로 기능하지 못한다.)
MAX_HEADER_BYTES = 8192

# "Name: value" 형태의 HTTP 헤더 줄 시작 패턴.
# ICY 상태 줄 뒤에 헤더가 더 오는지, 스트림이 시작됐는지 구분한다.
_HEADER_LINE_RE = re.compile(rb"^[A-Za-z][A-Za-z0-9\-_]*:")


class HeaderIncomplete(Exception):
    """헤더 종료 전에 연결이 끊긴 경우."""


def open_connection(host: str, port: int, timeout: float) -> socket.socket:
    """TCP 연결을 연다. 실패 시 OSError 계열이 그대로 올라간다."""
    return socket.create_connection((host, port), timeout=timeout)


def _split_icy(buf: bytearray):
    """ICY 응답이면 (상태 줄, body)로 즉시 분리한다. 아직 판단 불가면 None.

    NTRIP 1.0 캐스터는 "ICY 200 OK\r\n" 다음에 빈 줄 없이 곧바로
    RTCM 바이너리를 보내는 경우가 많다. RTKLIB도 이 관행을 전제로
    "ICY 200 OK\r\n"까지만 잘라내고 나머지를 스트림으로 처리한다.

    이때 \r\n\r\n을 기다리면 저속 스트림에서는 MAX_HEADER_BYTES가
    쌓이기 전에 헤더 타임아웃이 먼저 발생해, 정상 마운트가 오류로
    판정된다(1Hz 243B/s면 8192B까지 34초 소요). 따라서 첫 줄이
    ICY고 그 뒤가 헤더가 아니면 거기까지만 헤더로 확정한다.

    None을 반환하는 세 경우는 recv_header가 더 읽어서 재판단한다:
      - 첫 줄 뒤가 아직 비어 있음 (스트림인지 헤더인지 알 수 없음)
      - 첫 줄 뒤가 CRLF로 시작 (빈 줄 = 헤더 종료 → 메인 검사가 처리)
      - 첫 줄 뒤가 "Name:" 형태 (ICY 뒤에 헤더를 더 보내는 캐스터)

    마지막 조건이 없으면 후속 헤더를 데이터로 세어 가짜 양호가 난다.
    RTCM2.3의 6-of-8 인코딩은 0x40~0x5F만 사용해 ':'(0x3A)가 나올 수
    없고, RTCM3(0xD3)·BINEX도 헤더 패턴과 겹치지 않는다.
    """
    line_end = buf.find(b"\r\n")
    if line_end == -1:
        return None

    head = bytes(buf[:line_end])
    if not head.upper().startswith(b"ICY"):
        return None

    body = bytes(buf[line_end + 2:])
    if not body:
        return None
    if body.startswith(b"\r\n"):
        return None
    if _HEADER_LINE_RE.match(body):
        return None

    return head, body


def recv_header(sock: socket.socket, deadline: float) -> Tuple[bytes, bytes]:
    """헤더 종료까지 수신해 (헤더 블록, 남은 body 조각)을 돌려준다.

    첫 recv 1회분만 보고 판단하면 헤더가 여러 패킷으로 쪼개졌을 때
    남은 헤더를 body로 오인한다. 여기서는 헤더 종료가 나올 때까지
    누적해서 읽되, ICY 응답은 첫 줄에서 즉시 확정한다.

    raises:
        socket.timeout: deadline 초과
        HeaderIncomplete: 헤더가 끝나기 전에 연결 종료
    """
    buf = bytearray()

    while True:
        index = buf.find(b"\r\n\r\n")
        if index != -1:
            return bytes(buf[:index]), bytes(buf[index + 4:])

        # NTRIP 1.0(ICY)은 빈 줄 없이 곧바로 스트림이 올 수 있다.
        icy = _split_icy(buf)
        if icy is not None:
            return icy

        if len(buf) >= MAX_HEADER_BYTES:
            head, _, body = bytes(buf).partition(b"\r\n")
            return head, body

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("header timeout")

        sock.settimeout(remaining)
        chunk = sock.recv(4096)
        if not chunk:
            raise HeaderIncomplete(bytes(buf).decode(errors="replace")[:80])
        buf += chunk


class BodyReader:
    """헤더 이후의 body를 읽는다. chunked면 디코딩해서 payload만 돌려준다."""

    def __init__(self, sock: socket.socket, initial: bytes, chunked: bool):
        self._sock = sock
        self._decoder = ChunkedDecoder() if chunked else None
        self._pending = self._decode(initial)
        self.closed = False

    def _decode(self, data: bytes) -> bytes:
        if not data:
            return b""
        return self._decoder.feed(data) if self._decoder else data

    def read(self, timeout: float) -> Optional[bytes]:
        """payload 조각을 읽는다.

        반환:
            bytes  — 디코딩된 payload (빈 bytes면 아직 완성된 payload 없음)
            None   — timeout 또는 스트림 종료
        """
        if self._pending:
            data = self._pending
            self._pending = b""
            return data

        if self.closed:
            return None

        if self._decoder is not None and self._decoder.finished:
            return None

        self._sock.settimeout(timeout)
        try:
            raw = self._sock.recv(4096)
        except socket.timeout:
            return None

        if not raw:
            self.closed = True
            return None

        return self._decode(raw)
