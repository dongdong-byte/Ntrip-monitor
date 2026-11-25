"""CLI 진입점: NTRIP 마운트포인트 실시간 생존 판별 → 캐스터별 엑셀 누적

기본 동작:
  python liveness_main.py
    → config.ini의 모든 캐스터를 차례로 1회 검사
      캐스터마다 Sourcetable 마운트 전체를 검사하고 output/생존현황_<캐스터명>.xlsx에 누적

선택 실행:
  python liveness_main.py --caster gnssdata
    → 지정한 캐스터만 검사 (여러 번 쓰거나 쉼표로 구분 가능)

  python liveness_main.py ANHN BOEN
    → ANHN, BOEN 관측소의 마운트만 검사 (모든 캐스터에 적용)

  python liveness_main.py ANHN-RTCM32
    → 해당 마운트 하나만 검사

  python liveness_main.py --loop 3600
    → 1시간 간격으로 반복 검사

  python liveness_main.py --list-mounts
    → 마운트 목록만 확인 (검사·엑셀 저장·메일 없음)

메일:
  python liveness_main.py --mail
    → 검사 후 요약 메일 1통 발송 (cron 운영 형태)
      config.ini [smtp] 계정으로 네이버웍스(smtp.worksmobile.com:587, STARTTLS)에서 보냄
      이번 결과 저장에 성공한 캐스터별 엑셀을 첨부
      메일 발송에 실패하면 종료 코드 1

  python liveness_main.py --mail-test
    → 검사 없이 짧은 테스트 메일만 발송 (메일 설정 확인용)

프로토콜 요청 변형:
  --protocol auto    → 캐스터가 실제로 답하는 버전을 1회 탐지해 고정 (기본값)
  --protocol v2      → NTRIP 2.0 정식 요청 (HTTP/1.1 + Ntrip-Version)
  --protocol v1      → NTRIP 1.0 관행 요청 (HTTP/1.0)
  --protocol client  → gnssdata 배포 클라이언트와 동일한 요청 (민원 재현)
                       HTTP/1.1이지만 Ntrip-Version 헤더를 보내지 않음
  --protocol both    → v1 / v2를 각각 검사해 비교 (접속 2배, 진단용)
  --protocol all     → v1 / v2 / client 셋 다 검사해 비교 (접속 3배, 진단용)

마운트 목록 출처:
  --source auto    → Sourcetable 조회 성공 시 사용, 실패하면 보조 목록 (기본값)
                     완전 수신한 Sourcetable은 보조 목록(mounts/<캐스터명>.txt)에 저장
  --source caster  → Sourcetable만 사용하고 실패하면 그 캐스터는 검사 불가
  --source file    → 보조 목록만 사용 (진단용)

장애 격리:
  한 캐스터의 검사나 엑셀 저장이 실패해도 나머지 캐스터는 계속 검사한다.
  검사 결과는 엑셀 저장 성공 여부와 별도로 보존한다.
  검사 불가, 엑셀 저장 실패, 메일 발송 실패가 하나라도 있으면 종료 코드 1로 끝난다.
  실패한 캐스터가 있어도 메일은 보내며, 메일 맨 위 "확인 필요"에 표시한다.

계정:
  config.ini의 [caster:이름] 섹션에서 읽는다. (config.example.ini 참고)
  설정 점검: python config_loader.py
"""

import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import metadata_store
import mount_cache
from config_loader import CasterConfig, ConfigError, SmtpConfig, load_config
from liveness_checker import MountResult, NtripLivenessChecker
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
    # (예전 주석의 "GET /에 404"는 인증 헤더 때문이었음. docs/sourcetable.md)
    "MOUNT_SOURCE": "auto",

    # NTRIP 요청 변형 (v1 / v2 / client / auto / both / all)
    #   auto   : 캐스터별로 시작 시 v2 요청 1회로 버전을 탐지해 고정 (기본값)
    #            HTTP 응답이면 v2, ICY 또는 501이면 v1. 한계는 docs/ini.md 3절
    #   v2     : NTRIP 2.0 정식 요청
    #   v1     : NTRIP 1.0 관행 요청 (HTTP/1.0)
    #   client : 배포 클라이언트와 동일한 요청 (HTTP/1.1, 버전 헤더 없음)
    #   both   : v1 / v2 비교 (접속 2배)
    #   all    : v1 / v2 / client 비교 (접속 3배)
    "PROTOCOL_MODE": "auto",

    # 캐스터별 보조 마운트 목록 폴더: mounts/<캐스터명>.txt
    "MOUNTS_DIR": str(BASE_DIR / "mounts"),
    # 캐스터별 관측소 메타데이터 폴더: metadata/<캐스터명>.json
    "METADATA_DIR": str(BASE_DIR / "metadata"),
    "OUTPUT_DIR": str(BASE_DIR / "output"),
    # 캐스터별 누적 엑셀 파일명
    "OUTPUT_FILE_TEMPLATE": "생존현황_{caster}.xlsx",

    # 메일: 계정·수신자는 config.ini [smtp], 서버는 config_loader의 고정값
    # (smtp.worksmobile.com:587, STARTTLS). 엑셀은 항상 첨부한다.
}

SOURCE_SOURCETABLE = "Sourcetable"
SOURCE_CACHE = "보조 목록"

logger = logging.getLogger("gnss_crawler")


# ─────────────────────────────────────────────
# 실행 옵션 / 캐스터별 실행 결과
# ─────────────────────────────────────────────
@dataclass
class Options:
    loop_interval: Optional[int] = None
    mount_source: Optional[str] = None
    protocol_mode: Optional[str] = None
    send_mail: bool = False
    test_mail: bool = False
    list_only: bool = False
    casters: List[str] = field(default_factory=list)
    filters: List[str] = field(default_factory=list)


@dataclass
class CasterOutcome:
    """캐스터 1개의 1회 실행 결과.

    검사 결과(results)는 엑셀 저장 성공 여부와 별도로 보존한다.
    메일은 이 객체 목록을 받아 캐스터별 결과와 실패를 함께 보고한다.
    """

    name: str
    results: Dict[str, MountResult] = field(default_factory=dict)
    check_time: str = ""
    mount_source: str = ""             # Sourcetable / 보조 목록
    excel_path: Optional[str] = None   # 이번 결과 저장에 성공한 파일만
    error: str = ""                    # 검사 불가 사유
    excel_error: str = ""              # 엑셀 저장 실패 사유

    @property
    def ok(self) -> bool:
        return bool(self.results) and not self.error and not self.excel_error


# ─────────────────────────────────────────────
# 구성 요소 생성
# ─────────────────────────────────────────────
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


def build_writer(caster_name: str, station_metadata: dict) -> LivenessExcelWriter:
    return LivenessExcelWriter(
        LIVENESS_CONFIG["OUTPUT_DIR"],
        LIVENESS_CONFIG["OUTPUT_FILE_TEMPLATE"].format(caster=caster_name),
        station_metadata=station_metadata,
    )


def build_mailer(smtp: SmtpConfig) -> LivenessMailer:
    """config.ini [smtp] 설정으로 Mailer를 생성합니다. (설정 검증은 config_loader가 함)"""
    return LivenessMailer(smtp)


# ─────────────────────────────────────────────
# 마운트 목록 / 메타데이터
# ─────────────────────────────────────────────
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
) -> Tuple[list, str]:
    """설정된 출처에 따라 검사할 마운트 목록을 결정합니다.

    auto:   Sourcetable을 먼저 조회하고 실패하면 보조 목록 사용 (기본값)
    caster: Sourcetable만 사용
    file:   보조 목록만 사용 (진단용)

    Sourcetable을 완전히 수신하면 보조 목록(mounts/<캐스터명>.txt)을 갱신합니다.
    반환: (마운트 목록, 실제로 사용한 출처). 실패하면 ([], "").
    """
    selected_source = (source or LIVENESS_CONFIG["MOUNT_SOURCE"]).strip().lower()

    if selected_source not in {"file", "auto", "caster"}:
        logger.error(
            f"지원하지 않는 마운트 출처입니다: {selected_source} "
            "(file, auto, caster 중 하나를 사용하세요.)"
        )
        return [], ""

    cache_file = mount_cache.cache_path(LIVENESS_CONFIG["MOUNTS_DIR"], caster_name)

    if selected_source == "file":
        logger.info(f"[{caster_name}] 마운트 목록 출처: 보조 목록 {cache_file.name}")
        mounts = mount_cache.read_mounts(cache_file)

        if not mounts:
            logger.error(f"[{caster_name}] 보조 목록에서 검사할 마운트를 확보하지 못했습니다.")
            return [], ""

        return mounts, SOURCE_CACHE

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
        return mounts, SOURCE_SOURCETABLE

    if selected_source == "caster":
        logger.error(
            f"[{caster_name}] Sourcetable 조회에 실패했고 "
            "--source caster가 지정되어 이 캐스터는 검사하지 않습니다."
        )
        return [], ""

    logger.warning(
        f"[{caster_name}] Sourcetable 조회 실패 → 보조 목록 {cache_file.name}로 검사합니다."
    )
    file_mounts = mount_cache.read_mounts(cache_file)

    if not file_mounts:
        logger.error(
            f"[{caster_name}] Sourcetable과 보조 목록 모두에서 "
            "검사할 마운트를 확보하지 못했습니다."
        )
        return [], ""

    return file_mounts, SOURCE_CACHE


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


def select_casters(casters: List[CasterConfig], names: List[str]) -> List[CasterConfig]:
    """--caster로 지정한 캐스터만 고른다. 없는 이름이면 ValueError."""
    if not names:
        return list(casters)

    by_name = {c.name.lower(): c for c in casters}
    unknown = [n for n in names if n.lower() not in by_name]
    if unknown:
        raise ValueError(
            f"config.ini에 없는 캐스터: {', '.join(unknown)} "
            f"(사용 가능: {', '.join(c.name for c in casters)})"
        )

    wanted = {n.lower() for n in names}
    return [c for c in casters if c.name.lower() in wanted]


# ─────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────
def run_caster(caster: CasterConfig, options: Options) -> CasterOutcome:
    """캐스터 1개: 목록 결정 → 메타데이터 동기화 → 필터 → 검사 → 엑셀 저장."""
    outcome = CasterOutcome(name=caster.name)
    checker = build_checker(caster, options.protocol_mode)

    mounts, outcome.mount_source = resolve_mounts(
        checker, caster.name, options.mount_source
    )
    if not mounts:
        outcome.error = "검사할 마운트 목록을 확보하지 못함 (Sourcetable·보조 목록 모두 실패)"
        return outcome

    station_metadata = sync_station_metadata(caster.name, mounts)

    mounts = filter_mounts(mounts, options.filters or None)
    if not mounts:
        outcome.error = "필터와 일치하는 마운트 없음"
        return outcome

    logger.info(f"[{caster.name}] 마운트 생존 판별 시작: 총 {len(mounts)}개")
    results = checker.check_all(mounts)
    if not results:
        outcome.error = "판별 결과 없음"
        return outcome

    outcome.results = results
    outcome.check_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 엑셀 저장이 실패해도 검사 결과는 outcome에 남는다.
    try:
        writer = build_writer(caster.name, station_metadata)
        writer.write(results, outcome.check_time)
        outcome.excel_path = writer.output_path
    except Exception as exc:  # noqa: BLE001 — 어떤 저장 실패든 결과는 보존한다
        outcome.excel_error = f"{type(exc).__name__}: {exc}"
        logger.exception(f"[{caster.name}] 엑셀 생성·저장 실패")

    return outcome


def run_all(casters: List[CasterConfig], options: Options) -> List[CasterOutcome]:
    """모든 캐스터를 차례로 실행한다. 한 캐스터의 예외가 나머지를 막지 않는다."""
    outcomes = []
    for caster in casters:
        logger.info(f"===== [{caster.name}] {caster.host}:{caster.port} 검사 시작 =====")
        try:
            outcome = run_caster(caster, options)
        except Exception as exc:  # noqa: BLE001 — 캐스터 단위 장애 격리
            logger.exception(f"[{caster.name}] 검사 중 예외")
            outcome = CasterOutcome(
                name=caster.name, error=f"검사 중 예외: {type(exc).__name__}: {exc}"
            )
        outcomes.append(outcome)

    log_run_summary(outcomes)
    return outcomes


def log_run_summary(outcomes: List[CasterOutcome]) -> None:
    logger.info("===== 실행 요약 =====")
    for o in outcomes:
        if o.error:
            logger.error(f"[{o.name}] 검사 불가: {o.error}")
            continue
        alive = sum(1 for r in o.results.values() if r.status == "양호")
        excel = o.excel_path if o.excel_path else f"저장 실패 ({o.excel_error})"
        line = (
            f"[{o.name}] 양호 {alive}/{len(o.results)} "
            f"(목록 출처: {o.mount_source}, 엑셀: {excel})"
        )
        if o.excel_error or o.mount_source == SOURCE_CACHE:
            logger.warning(line)
        else:
            logger.info(line)


def send_mail(outcomes: List[CasterOutcome], smtp: SmtpConfig, run_time: str) -> bool:
    """모든 캐스터 결과를 메일 1통으로 보낸다. 반환: 발송 성공 여부.

    검사에 실패한 캐스터가 있어도 보낸다. 실패 내용은 메일의 "확인 필요"에 표시된다.
    """
    return build_mailer(smtp).send_report(outcomes, run_time)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def list_mounts(casters: List[CasterConfig], options: Options) -> bool:
    """마운트 목록만 확인한다. 검사·엑셀 저장·메일은 하지 않는다."""
    all_ok = True
    for caster in casters:
        try:
            checker = build_checker(caster, options.protocol_mode)
            mounts, source = resolve_mounts(checker, caster.name, options.mount_source)
            if mounts:
                sync_station_metadata(caster.name, mounts)
                mounts = filter_mounts(mounts, options.filters or None)
        except Exception:
            logger.exception(f"[{caster.name}] 마운트 목록 확인 실패")
            all_ok = False
            continue
        logger.info(
            f"[{caster.name}] 마운트 목록 확인 완료: {len(mounts)}개"
            + (f" (출처: {source})" if source else "")
        )
        all_ok = all_ok and bool(mounts)
    return all_ok


# ─────────────────────────────────────────────
# 인수
# ─────────────────────────────────────────────
def parse_arguments(args: list) -> Options:
    """옵션과 필터 인수를 분리합니다."""
    options = Options()
    index = 0

    def value_of(name: str, choices: str) -> str:
        if index + 1 >= len(args):
            raise ValueError(f"{name} 뒤에 {choices}을(를) 입력해야 합니다.")
        return args[index + 1]

    while index < len(args):
        arg = args[index]

        if arg == "--mail":
            options.send_mail = True
            index += 1
            continue

        if arg == "--mail-test":
            options.test_mail = True
            index += 1
            continue

        if arg == "--list-mounts":
            options.list_only = True
            index += 1
            continue

        if arg == "--caster":
            raw = value_of("--caster", "캐스터 이름")
            names = [n.strip() for n in raw.split(",") if n.strip()]
            if not names or any(n.startswith("--") for n in names):
                raise ValueError("--caster 뒤에 캐스터 이름을 하나 이상 입력하세요.")
            options.casters.extend(names)
            index += 2
            continue

        if arg == "--loop":
            raw = value_of("--loop", "반복 간격(초)")
            try:
                options.loop_interval = int(raw)
            except ValueError as exc:
                raise ValueError("--loop 반복 간격은 정수 초 단위여야 합니다.") from exc
            if options.loop_interval <= 0:
                raise ValueError("--loop 반복 간격은 1초 이상이어야 합니다.")
            index += 2
            continue

        if arg == "--source":
            options.mount_source = value_of("--source", "file, auto, caster 중 하나").lower()
            if options.mount_source not in {"file", "auto", "caster"}:
                raise ValueError("--source는 file, auto, caster 중 하나여야 합니다.")
            index += 2
            continue

        if arg == "--protocol":
            options.protocol_mode = value_of(
                "--protocol", "v1, v2, client, auto, both, all 중 하나"
            ).lower()
            if options.protocol_mode not in {"v1", "v2", "client", "auto", "both", "all"}:
                raise ValueError(
                    "--protocol은 v1, v2, client, auto, both, all 중 하나여야 합니다."
                )
            index += 2
            continue

        if arg.startswith("--"):
            raise ValueError(f"알 수 없는 옵션: {arg}")

        options.filters.append(arg)
        index += 1

    return options


def main() -> None:
    setup_logging()

    try:
        options = parse_arguments(sys.argv[1:])
        config = load_config()
        casters = select_casters(config.casters, options.casters)
    except (ValueError, ConfigError) as exc:
        logger.error(str(exc))
        raise SystemExit(1)

    if options.test_mail:
        # 검사 없이 메일 설정만 확인한다.
        if not build_mailer(config.smtp).send_test(now_text()):
            raise SystemExit(1)
        return

    logger.info(f"대상 캐스터 {len(casters)}개: {', '.join(c.name for c in casters)}")

    if options.list_only:
        if not list_mounts(casters, options):
            raise SystemExit(1)
        return

    def run_cycle() -> bool:
        run_time = now_text()
        outcomes = run_all(casters, options)
        mail_ok = True
        if options.send_mail:
            mail_ok = send_mail(outcomes, config.smtp, run_time)
        return all(o.ok for o in outcomes) and mail_ok

    if options.loop_interval is None:
        if not run_cycle():
            raise SystemExit(1)
        return

    logger.info(f"반복 모드: {options.loop_interval}초 간격 (Ctrl+C로 종료)")
    try:
        while True:
            run_cycle()
            logger.info(f"{options.loop_interval}초 대기...")
            time.sleep(options.loop_interval)
    except KeyboardInterrupt:
        logger.info("반복 모드 종료")


if __name__ == "__main__":
    main()
