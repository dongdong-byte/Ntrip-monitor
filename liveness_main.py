"""CLI 진입점: NTRIP 마운트포인트 실시간 생존 판별 → 엑셀 누적

기본 동작:
  python liveness_main.py
    → mounts.txt에 등록된 전체 마운트를 1회 검사

선택 실행:
  python liveness_main.py ANHN BOEN
    → ANHN, BOEN 관측소의 마운트만 검사

  python liveness_main.py ANHN-RTCM32
    → 해당 마운트 하나만 검사

  python liveness_main.py --loop 3600
    → 1시간 간격으로 반복 검사

  python liveness_main.py --list-mounts
    → 마운트 목록만 확인 (검사·엑셀 저장·메일 없음)

프로토콜 요청 변형:
  --protocol v2      → NTRIP 2.0 정식 요청 (HTTP/1.1 + Ntrip-Version) — 권장
  --protocol v1      → NTRIP 1.0 관행 요청 (HTTP/1.0)
  --protocol client  → gnssdata 배포 클라이언트와 동일한 요청 (민원 재현)
                       HTTP/1.1이지만 Ntrip-Version 헤더를 보내지 않음
  --protocol auto    → 캐스터가 실제로 답하는 버전을 1회 탐지해 고정
  --protocol both    → v1 / v2를 각각 검사해 비교 (접속 2배, 진단용)
  --protocol all     → v1 / v2 / client 셋 다 검사해 비교 (접속 3배, 진단용)

마운트 목록 출처:
  --source auto    → Sourcetable 조회 성공 시 사용, 실패하면 보조 목록 (기본값)
                     완전 수신한 Sourcetable은 보조 목록(mounts/<캐스터명>.txt)에 저장
  --source caster  → Sourcetable만 사용하고 실패하면 종료
  --source file    → 보조 목록만 사용 (진단용)

계정:
  config.ini의 [caster:이름] 섹션에서 읽는다. (config.example.ini 참고)
  설정 점검: python config_loader.py
"""

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import metadata_store
import mount_cache
from config_loader import CasterConfig, ConfigError, load_config
from liveness_checker import NtripLivenessChecker
from liveness_excel_writer import LivenessExcelWriter
from liveness_mailer import LivenessMailer
from mount_cache import clean_mount_name


BASE_DIR = Path(__file__).resolve().parent

LIVENESS_CONFIG = {
    # 캐스터 주소와 계정은 config.ini에서 읽는다.

    "CONNECT_TIMEOUT": 10.0,
    "DATA_TIMEOUT": 10.0,

    # 운영 캐스터 부하를 고려해 처음에는 낮게 시작합니다.
    "MAX_WORKERS": 15,

    # 마운트 목록 출처: auto / caster / file
    # auto: Sourcetable 우선, 실패하면 보조 목록(mounts/<캐스터명>.txt)
    # (예전 주석의 "GET /에 404"는 2026-11-25 확인 결과 사실이 아님. docs/sourcetable.md)
    "MOUNT_SOURCE": "auto",

    # NTRIP 요청 변형 (v1 / v2 / client / auto / both / all)
    #   v2     : NTRIP 2.0 정식 요청 (캐스터 2.0 지원 확인됨 → 기본값)
    #   v1     : NTRIP 1.0 관행 요청 (HTTP/1.0)
    #   client : 배포 클라이언트와 동일한 요청 (HTTP/1.1, 버전 헤더 없음)
    #   auto   : 캐스터가 답하는 버전을 1회 탐지해 고정
    #   both   : v1 / v2 비교 (접속 2배)
    #   all    : v1 / v2 / client 비교 (접속 3배)
    "PROTOCOL_MODE": "v2",

    # 캐스터별 보조 마운트 목록 폴더: mounts/<캐스터명>.txt
    "MOUNTS_DIR": str(BASE_DIR / "mounts"),
    # 캐스터별 관측소 메타데이터 폴더: metadata/<캐스터명>.json
    "METADATA_DIR": str(BASE_DIR / "metadata"),
    "OUTPUT_DIR": str(BASE_DIR / "output"),
    "OUTPUT_FILE": "마운트포인트_생존현황.xlsx",

    # ── 메일 설정 (--mail 플래그가 있을 때만 발송) ──
    # 회사 메일 제공사에 맞게 수정하세요.
    #   네이버웍스: smtp.worksmobile.com / 587 / SSL False
    #   네이버:     smtp.naver.com      / 587 / SSL False (앱 비밀번호)
    #   구글:       smtp.gmail.com      / 587 / SSL False (앱 비밀번호)
    #   하이웍스:   smtp.hiworks.com    / 465 / SSL True
    "SMTP_HOST": "",
    "SMTP_PORT": 587,
    "SMTP_SSL": False,               # 465 포트면 True, 587이면 False(STARTTLS)
    "SMTP_USER": "",                 # 환경변수 SMTP_USER 권장
    "SMTP_PASS": "",                 # 환경변수 SMTP_PASS 권장
    "MAIL_FROM": "",                 # 보내는 주소 (보통 SMTP_USER와 동일)
    "MAIL_TO": [],                   # 수신자 목록 예: ["office@company.co.kr"]
    "MAIL_ATTACH_EXCEL": False,      # True면 누적 엑셀 파일 첨부
}

logger = logging.getLogger("gnss_crawler")


def setup_logging() -> None:
    """콘솔과 파일에 실행 로그를 기록합니다."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(BASE_DIR / "liveness.log", encoding="utf-8"),
        ],
    )


def build_checker(
    caster: CasterConfig, protocol_mode: Optional[str] = None
) -> NtripLivenessChecker:
    """config.ini의 캐스터 설정으로 NTRIP 검사기를 생성합니다."""
    return NtripLivenessChecker(
        host=caster.host,
        port=caster.port,
        username=caster.user,
        password=caster.password,
        connect_timeout=LIVENESS_CONFIG["CONNECT_TIMEOUT"],
        data_timeout=LIVENESS_CONFIG["DATA_TIMEOUT"],
        max_workers=LIVENESS_CONFIG["MAX_WORKERS"],
        protocol_mode=protocol_mode or LIVENESS_CONFIG["PROTOCOL_MODE"],
    )




def build_mailer() -> Optional[LivenessMailer]:
    """메일 설정이 완성돼 있을 때만 Mailer를 생성합니다."""
    smtp_host = LIVENESS_CONFIG["SMTP_HOST"].strip()
    smtp_user = os.environ.get("SMTP_USER", LIVENESS_CONFIG["SMTP_USER"]).strip()
    smtp_pass = os.environ.get("SMTP_PASS", LIVENESS_CONFIG["SMTP_PASS"])
    sender = LIVENESS_CONFIG["MAIL_FROM"].strip() or smtp_user
    recipients = LIVENESS_CONFIG["MAIL_TO"]

    missing = []
    if not smtp_host:
        missing.append("SMTP_HOST")
    if not smtp_user:
        missing.append("SMTP_USER")
    if not recipients:
        missing.append("MAIL_TO")

    if missing:
        logger.error(f"메일 설정 누락: {', '.join(missing)} → 발송 생략")
        return None

    return LivenessMailer(
        smtp_host=smtp_host,
        smtp_port=LIVENESS_CONFIG["SMTP_PORT"],
        username=smtp_user,
        password=smtp_pass,
        sender=sender,
        recipients=recipients,
        use_ssl=LIVENESS_CONFIG["SMTP_SSL"],
    )


def sync_station_metadata(caster_name: str, mounts: list) -> dict:
    """미등록 관측소를 빈 항목으로 추가한 뒤, writer용 메타데이터를 읽습니다.

    필터 적용 전의 전체 마운트 목록을 넘겨야 미등록 관측소를 빠짐없이 찾습니다.
    """
    path = metadata_store.metadata_path(LIVENESS_CONFIG["METADATA_DIR"], caster_name)
    metadata_store.add_missing(path, mounts)
    return metadata_store.load(path)


def resolve_mounts(
    checker: NtripLivenessChecker,
    caster_name: str,
    source: Optional[str] = None,
) -> list:
    """설정된 출처에 따라 검사할 마운트 목록을 결정합니다.

    auto:   Sourcetable을 먼저 조회하고 실패하면 보조 목록 사용 (기본값)
    caster: Sourcetable만 사용
    file:   보조 목록만 사용 (진단용)

    Sourcetable을 완전히 수신하면 보조 목록(mounts/<캐스터명>.txt)을 갱신합니다.
    """
    selected_source = (source or LIVENESS_CONFIG["MOUNT_SOURCE"]).strip().lower()

    if selected_source not in {"file", "auto", "caster"}:
        logger.error(
            f"지원하지 않는 마운트 출처입니다: {selected_source} "
            "(file, auto, caster 중 하나를 사용하세요.)"
        )
        return []

    cache_file = mount_cache.cache_path(LIVENESS_CONFIG["MOUNTS_DIR"], caster_name)

    if selected_source == "file":
        logger.info(f"[{caster_name}] 마운트 목록 출처: 보조 목록 {cache_file.name}")
        mounts = mount_cache.read_mounts(cache_file)

        if not mounts:
            logger.error(f"[{caster_name}] 보조 목록에서 검사할 마운트를 확보하지 못했습니다.")

        return mounts

    logger.info(f"[{caster_name}] 마운트 목록 출처: Sourcetable 조회")
    # fetch_mountpoints는 완전 수신이 확인된 목록만 돌려준다. (ntrip/sourcetable.py)
    caster_mounts = checker.fetch_mountpoints()

    if caster_mounts:
        # 케이스는 sourcetable 원본 그대로 유지 (공백/슬래시만 정리)
        mounts = sorted({
            clean_mount_name(mount)
            for mount in caster_mounts
            if clean_mount_name(mount)
        })
        logger.info(f"[{caster_name}] Sourcetable 마운트 {len(mounts)}개 사용")
        mount_cache.write_mounts(
            cache_file, mounts, source=f"{checker.host}:{checker.port}"
        )
        return mounts

    if selected_source == "caster":
        logger.error(
            f"[{caster_name}] Sourcetable 조회에 실패했고 "
            "--source caster가 지정되어 종료합니다."
        )
        return []

    logger.warning(
        f"[{caster_name}] Sourcetable 조회 실패 → 보조 목록 {cache_file.name}로 검사합니다."
    )
    file_mounts = mount_cache.read_mounts(cache_file)

    if not file_mounts:
        logger.error(
            f"[{caster_name}] Sourcetable과 보조 목록 모두에서 "
            "검사할 마운트를 확보하지 못했습니다."
        )

    return file_mounts


def filter_mounts(
    mounts: list,
    filters: Optional[list],
) -> list:
    """관측소 코드 또는 정확한 마운트명으로 검사 대상을 제한합니다.

    비교는 대소문자 무시(양쪽 다 upper), 결과 목록은 원본 케이스 유지.

    예:
      ANHN        → ANHN-RTCM23, ANHN-RTCM30, ANHN-RTCM32
      ANHN-RTCM32 → ANHN-RTCM32만 선택
    """
    if not filters:
        return mounts

    normalized_filters = [
        clean_mount_name(value).upper()
        for value in filters
        if clean_mount_name(value)
    ]

    selected = [
        mount
        for mount in mounts
        if any(
            mount.upper() == value or mount.upper().startswith(value + "-")
            for value in normalized_filters
        )
    ]

    logger.info(f"필터 적용: {normalized_filters} → 마운트 {len(selected)}개")

    if not selected:
        logger.error(
            f"입력한 조건과 일치하는 마운트가 없습니다: {normalized_filters}"
        )

    return selected


def run_once(
    checker: NtripLivenessChecker,
    writer: LivenessExcelWriter,
    caster_name: str,
    station_filter: Optional[list] = None,
    mount_source: Optional[str] = None,
    send_mail: bool = False,
) -> bool:
    """마운트 목록 결정 → 필터 → 생존 검사 → 엑셀 저장을 1회 수행합니다."""
    mounts = resolve_mounts(checker, caster_name, mount_source)

    if not mounts:
        return False

    writer.station_metadata = sync_station_metadata(caster_name, mounts)

    mounts = filter_mounts(mounts, station_filter)

    if not mounts:
        return False

    logger.info(f"마운트 생존 판별 시작: 총 {len(mounts)}개")
    results = checker.check_all(mounts)

    if not results:
        logger.error("판별 결과가 없습니다.")
        return False

    check_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    writer.write(results, check_time)

    if send_mail:
        mailer = build_mailer()
        if mailer:
            attach = (
                writer.output_path
                if LIVENESS_CONFIG["MAIL_ATTACH_EXCEL"]
                else None
            )
            mailer.send(results, check_time, attach_path=attach)

    return True


def parse_arguments(args: list) -> tuple:
    """--loop, --source, --protocol, --mail, --list-mounts 옵션과 필터 인수를 분리합니다."""
    loop_interval = None
    mount_source = None
    protocol_mode = None
    send_mail = False
    list_only = False
    filters = []

    index = 0

    while index < len(args):
        arg = args[index]

        if arg == "--mail":
            send_mail = True
            index += 1
            continue

        if arg == "--list-mounts":
            list_only = True
            index += 1
            continue

        if arg == "--loop":
            if index + 1 >= len(args):
                raise ValueError("--loop 뒤에 반복 간격(초)을 입력해야 합니다.")

            try:
                loop_interval = int(args[index + 1])
            except ValueError as exc:
                raise ValueError(
                    "--loop 반복 간격은 정수 초 단위여야 합니다."
                ) from exc

            if loop_interval <= 0:
                raise ValueError("--loop 반복 간격은 1초 이상이어야 합니다.")

            index += 2
            continue

        if arg == "--source":
            if index + 1 >= len(args):
                raise ValueError(
                    "--source 뒤에 file, auto, caster 중 하나를 입력해야 합니다."
                )

            mount_source = args[index + 1].lower()

            if mount_source not in {"file", "auto", "caster"}:
                raise ValueError(
                    "--source는 file, auto, caster 중 하나여야 합니다."
                )

            index += 2
            continue

        if arg == "--protocol":
            if index + 1 >= len(args):
                raise ValueError(
                    "--protocol 뒤에 v1, v2, client, auto, both, all 중 하나를 입력해야 합니다."
                )

            protocol_mode = args[index + 1].lower()

            if protocol_mode not in {"v1", "v2", "client", "auto", "both", "all"}:
                raise ValueError(
                    "--protocol은 v1, v2, client, auto, both, all 중 하나여야 합니다."
                )

            index += 2
            continue

        filters.append(arg)
        index += 1

    return loop_interval, mount_source, protocol_mode, send_mail, list_only, filters


def main() -> None:
    setup_logging()

    try:
        (
            loop_interval,
            mount_source,
            protocol_mode,
            send_mail,
            list_only,
            station_filter,
        ) = parse_arguments(sys.argv[1:])
    except ValueError as exc:
        logger.error(str(exc))
        raise SystemExit(1)

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error(str(exc))
        raise SystemExit(1)

    # 여러 캐스터를 차례로 검사하는 구조는 D단계에서 만든다.
    # 그 전까지는 config.ini의 첫 번째 캐스터만 검사한다.
    caster = config.casters[0]
    if len(config.casters) > 1:
        skipped = ", ".join(c.name for c in config.casters[1:])
        logger.warning(
            f"현재는 첫 번째 캐스터({caster.name})만 검사합니다. 건너뜀: {skipped}"
        )

    checker = build_checker(caster, protocol_mode)

    if list_only:
        # 마운트 목록만 확인한다. 검사·엑셀 저장·메일은 하지 않는다.
        mounts = resolve_mounts(checker, caster.name, mount_source)
        if mounts:
            sync_station_metadata(caster.name, mounts)
        mounts = filter_mounts(mounts, station_filter or None) if mounts else mounts
        logger.info(f"[{caster.name}] 마운트 목록 확인 완료: {len(mounts)}개")
        if not mounts:
            raise SystemExit(1)
        return

    # 관측소 메타데이터는 run_once에서 마운트 목록을 확정한 뒤 동기화해 넣는다.
    writer = LivenessExcelWriter(
        LIVENESS_CONFIG["OUTPUT_DIR"],
        LIVENESS_CONFIG["OUTPUT_FILE"],
    )

    if loop_interval is None:
        run_once(
            checker, writer, caster.name,
            station_filter or None, mount_source, send_mail,
        )
        return

    logger.info(f"반복 모드: {loop_interval}초 간격 (Ctrl+C로 종료)")

    try:
        while True:
            run_once(
                checker, writer, caster.name,
                station_filter or None, mount_source, send_mail,
            )
            logger.info(f"{loop_interval}초 대기...")
            time.sleep(loop_interval)

    except KeyboardInterrupt:
        logger.info("반복 모드 종료")


if __name__ == "__main__":
    main()
