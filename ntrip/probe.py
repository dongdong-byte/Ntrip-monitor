"""스트림 관측 계층.

책임: 관측 창(window) 동안 body가 실제로 얼마나 흘렀는지 측정한다.
"1바이트라도 오면 살아있다"가 아니라 "기준 바이트 이상"을 요구한다.

판정에 recv() 호출 횟수를 쓰지 않는 이유:
    TCP recv() 횟수는 커널 버퍼링·MTU·타이밍의 산물이지 데이터의
    속성이 아니다. 같은 200바이트가 환경에 따라 1회로도 2회로도
    수신된다. 이를 판정 조건에 넣으면 "4000B를 1회 수신 → 무데이터",
    "101B를 2회 수신 → 양호" 같은 모순이 발생한다.
    recv_count는 ProbeResult에 남겨 로그·진단에만 사용한다.
    수신 지속성은 recv 횟수가 아니라 RTCM 프레임 수나 도착 간격으로
    판정해야 하며, 이는 3차 작업의 FrameValidator에서 다룬다.

확장 지점:
    3차 작업에서 RTCM3 프레임(0xD3) 검출과 CRC24Q 검증을 붙일 자리는
    measure() 내부의 누적 지점이다. 여기에 FrameValidator를 주입하면
    상위 계층(policy, checker)을 건드리지 않고 확장할 수 있다.
"""

import time

from .connection import BodyReader
from .models import ProbeResult


class StreamProbe:
    """수신 지속성 판정 기준을 들고 body를 관측한다."""

    def __init__(
        self,
        window_seconds: float,
        min_bytes: int = 100,
    ):
        self.window_seconds = window_seconds
        self.min_bytes = min_bytes

    def measure(self, reader: BodyReader) -> ProbeResult:
        """관측 창 동안 payload를 집계한다.

        기준 바이트를 채우면 창이 끝나기 전에 즉시 종료한다.
        1Hz RTCM이면 보통 1초 안에 확정되므로 정상 마운트의
        검사 시간이 늘어나지 않는다.
        """
        started = time.monotonic()
        deadline = started + self.window_seconds

        total = 0
        count = 0

        while True:
            if self.satisfied(total):
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            data = reader.read(remaining)
            if data is None:  # timeout 또는 스트림 종료
                break
            if not data:  # 청크 경계 등으로 아직 payload 없음 → 계속
                continue

            total += len(data)
            count += 1

        return ProbeResult(
            bytes_received=total,
            recv_count=count,
            duration_seconds=time.monotonic() - started,
        )

    def satisfied(self, total_bytes: int) -> bool:
        """정상 수신으로 인정할 기준을 만족했는지.

        바이트 양만 본다. recv() 횟수는 판정에 쓰지 않는다.
        (모듈 docstring 참조)
        """
        return total_bytes >= self.min_bytes
