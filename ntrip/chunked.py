"""HTTP chunked transfer encoding 디코더.

책임: 바이트를 먹여 넣으면(feed) 순수 payload만 돌려준다.
NTRIP 2.0 캐스터는 스트림을 chunked로 보낼 수 있는데,
청크 크기 문자열("1a2b\\r\\n")까지 GNSS 데이터로 세면
수신량 판정이 부풀려지므로 반드시 디코딩해야 한다.
"""

# 내부 상태
_STATE_SIZE = 0   # 청크 크기 줄을 읽는 중
_STATE_DATA = 1   # 청크 본문을 읽는 중
_STATE_CRLF = 2   # 청크 본문 뒤 CRLF를 건너뛰는 중
_STATE_DONE = 3   # 종료(0 크기 청크) 또는 파싱 불가

# 크기 줄이 이 길이를 넘으면 chunked가 아니라고 보고 중단
_MAX_SIZE_LINE = 64


class ChunkedDecoder:
    """스트리밍 chunked 디코더. feed()를 반복 호출한다."""

    def __init__(self):
        self._buf = bytearray()
        self._state = _STATE_SIZE
        self._remaining = 0
        self.finished = False
        self.malformed = False

    def feed(self, data: bytes) -> bytes:
        """수신 바이트를 넣고 디코딩된 payload를 반환한다.

        청크 경계가 걸치면 빈 bytes를 반환할 수 있으며,
        이는 오류가 아니라 "아직 완성된 payload가 없음"을 뜻한다.
        """
        self._buf += data
        out = bytearray()

        while True:
            if self._state == _STATE_SIZE:
                index = self._buf.find(b"\r\n")
                if index == -1:
                    if len(self._buf) > _MAX_SIZE_LINE:
                        self._fail()
                    break

                line = bytes(self._buf[:index]).split(b";", 1)[0].strip()
                del self._buf[: index + 2]

                try:
                    size = int(line, 16)
                except ValueError:
                    self._fail()
                    break

                if size == 0:
                    self._state = _STATE_DONE
                    self.finished = True
                    break

                self._remaining = size
                self._state = _STATE_DATA

            elif self._state == _STATE_DATA:
                if not self._buf:
                    break
                take = min(self._remaining, len(self._buf))
                out += self._buf[:take]
                del self._buf[:take]
                self._remaining -= take
                if self._remaining == 0:
                    self._state = _STATE_CRLF

            elif self._state == _STATE_CRLF:
                if len(self._buf) < 2:
                    break
                del self._buf[:2]
                self._state = _STATE_SIZE

            else:  # _STATE_DONE
                break

        return bytes(out)

    def _fail(self):
        self._state = _STATE_DONE
        self.finished = True
        self.malformed = True
