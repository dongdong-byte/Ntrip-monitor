"""config.ini 로더.

책임: ini 파일을 읽어 SMTP 설정과 캐스터 목록으로 변환하고 값을 검증한다.
ini에는 환경마다 달라지는 값(계정, 비밀번호, 수신자, 캐스터 주소)만 둔다.
나머지 동작 기본값은 코드에 고정한다. (docs/ini.md 참고)

ini 형식:
    [smtp]
    user = support@valuespace.info
    password = (네이버웍스 외부 앱 비밀번호)
    mail_to = support@valuespace.info

    [caster:gnssdata]
    host = www.gnssdata.or.kr
    port = 2101
    user = (NTRIP 계정)
    password = (NTRIP 비밀번호)

단독 실행으로 설정 파일을 점검할 수 있다. 비밀번호는 출력하지 않는다.
    python config_loader.py              → 같은 폴더의 config.ini 점검
    python config_loader.py 경로/파일.ini → 지정한 파일 점검
"""

import configparser
import ipaddress
import logging
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger("gnss_crawler")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.ini"
EXAMPLE_CONFIG_NAME = "config.example.ini"

# ── 코드에 고정된 SMTP 서버 (네이버웍스, 587 / STARTTLS) ──
SMTP_HOST = "smtp.worksmobile.com"
SMTP_PORT = 587

SMTP_SECTION = "smtp"
CASTER_PREFIX = "caster:"

SMTP_KEYS = ("user", "password", "mail_to")
CASTER_KEYS = ("host", "port", "user", "password")

# config.example.ini의 자리표시 값. 그대로 남아 있으면 미입력으로 본다.
PLACEHOLDER_RE = re.compile(r"^\*+$|^\(.*\)$")

# 캐스터 이름은 메일·로그·파일명(엑셀, 메타데이터, 마운트 캐시)에 쓰인다.
CASTER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """설정 파일을 사용할 수 없을 때. 메시지는 사용자에게 그대로 보여준다."""


@dataclass(frozen=True)
class SmtpConfig:
    user: str
    password: str = field(repr=False)
    mail_to: List[str] = field(default_factory=list)
    host: str = SMTP_HOST
    port: int = SMTP_PORT


@dataclass(frozen=True)
class CasterConfig:
    name: str
    host: str
    port: int
    user: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class AppConfig:
    path: Path
    smtp: SmtpConfig
    casters: List[CasterConfig]


def load_config(path=DEFAULT_CONFIG_PATH) -> AppConfig:
    """ini를 읽어 검증된 설정을 돌려준다. 문제가 있으면 ConfigError."""
    config_path = Path(path)

    if not config_path.exists():
        raise ConfigError(
            f"설정 파일이 없습니다: {config_path}\n"
            f"  {EXAMPLE_CONFIG_NAME}를 복사해 {config_path.name}로 저장한 뒤 "
            "계정과 비밀번호를 채우세요."
        )

    _warn_if_readable_by_others(config_path)

    # interpolation=None: 비밀번호의 '%'를 특수문자로 해석하지 않는다.
    # inline_comment_prefixes 미지정: 값 뒤의 ';'나 '#'도 값의 일부로 본다.
    # (비밀번호에 ';'가 들어갈 수 있으므로 주석은 반드시 별도 줄에 쓴다.)
    parser = configparser.ConfigParser(interpolation=None, strict=True)

    try:
        # utf-8-sig: 메모장이 붙이는 BOM이 있어도 읽힌다.
        with config_path.open("r", encoding="utf-8-sig") as file:
            parser.read_file(file)
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"설정 파일을 UTF-8로 읽을 수 없습니다: {config_path}\n"
            "  파일을 UTF-8로 저장하세요."
        ) from None
    except configparser.Error as exc:
        # 파서의 원래 오류에는 비밀번호가 포함된 원문 줄이 들어갈 수 있다.
        line = getattr(exc, "lineno", None)
        location = f" (줄 {line})" if line is not None else ""
        raise ConfigError(
            f"설정 파일 형식 오류: {config_path}{location}. "
            "섹션, 항목 중복 및 key = value 형식을 확인하세요."
        ) from None
    except OSError:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {config_path}") from None

    _check_sections(parser)

    smtp = _parse_smtp(parser)
    casters = _parse_casters(parser)

    return AppConfig(path=config_path, smtp=smtp, casters=casters)


# ─────────────────────────────────────────────
# 검증
# ─────────────────────────────────────────────
def _check_sections(parser: configparser.ConfigParser) -> None:
    """[smtp]와 [caster:이름] 외의 섹션은 오타로 보고 거부한다."""
    if parser.defaults():
        raise ConfigError("[DEFAULT] 공통값은 지원하지 않습니다. 각 섹션에 직접 입력하세요.")
    unknown = [
        name
        for name in parser.sections()
        if name != SMTP_SECTION and not name.startswith(CASTER_PREFIX)
    ]
    if unknown:
        raise ConfigError(
            f"알 수 없는 섹션: {', '.join(f'[{n}]' for n in unknown)}\n"
            f"  사용 가능한 섹션은 [{SMTP_SECTION}]와 [{CASTER_PREFIX}이름]입니다."
        )


def _read_section(
    parser: configparser.ConfigParser, section: str, keys: tuple
) -> dict:
    """필수 키가 모두 채워졌는지, 모르는 키가 없는지 확인한다."""
    values = dict(parser.items(section))

    unknown = sorted(set(values) - set(keys))
    if unknown:
        raise ConfigError(
            f"[{section}]에 알 수 없는 항목: {', '.join(unknown)}\n"
            f"  사용 가능한 항목: {', '.join(keys)}"
        )

    missing = []
    for key in keys:
        value = values.get(key, "").strip()
        if not value or PLACEHOLDER_RE.match(value):
            missing.append(key)
        values[key] = value

    if missing:
        raise ConfigError(
            f"[{section}]에 값이 비어 있거나 예시 값 그대로인 항목: "
            f"{', '.join(missing)}"
        )

    return values


def _parse_smtp(parser: configparser.ConfigParser) -> SmtpConfig:
    if not parser.has_section(SMTP_SECTION):
        raise ConfigError(f"[{SMTP_SECTION}] 섹션이 없습니다.")

    values = _read_section(parser, SMTP_SECTION, SMTP_KEYS)

    recipients = [addr.strip() for addr in values["mail_to"].split(",")]
    recipients = [addr for addr in recipients if addr]
    if not recipients:
        raise ConfigError(f"[{SMTP_SECTION}] mail_to에 수신자를 하나 이상 입력하세요.")
    invalid = [addr for addr in recipients if not _looks_like_email(addr)]
    if invalid:
        raise ConfigError(
            f"[{SMTP_SECTION}] mail_to에 메일 주소 형식이 아닌 값: "
            f"{', '.join(invalid)}"
        )

    if not _looks_like_email(values["user"]):
        raise ConfigError(
            f"[{SMTP_SECTION}] user는 보내는 메일 주소여야 합니다: {values['user']}"
        )

    return SmtpConfig(
        user=values["user"],
        password=values["password"],
        mail_to=recipients,
    )


def _parse_casters(parser: configparser.ConfigParser) -> List[CasterConfig]:
    casters = []

    for section in parser.sections():
        if not section.startswith(CASTER_PREFIX):
            continue

        name = section[len(CASTER_PREFIX):].strip()
        if not CASTER_NAME_RE.match(name):
            raise ConfigError(
                f"[{section}] 캐스터 이름은 영문, 숫자, '-', '_'만 쓸 수 있습니다. "
                "(파일명에 쓰이기 때문입니다)"
            )

        values = _read_section(parser, section, CASTER_KEYS)
        if not _looks_like_host(values["host"]):
            raise ConfigError(
                f"[{section}] host는 도메인 또는 IP 주소여야 합니다. "
                "URL, 경로, 역슬래시, 포트는 포함하지 마세요."
            )

        try:
            port = int(values["port"])
        except ValueError as exc:
            raise ConfigError(
                f"[{section}] port는 숫자여야 합니다: {values['port']}"
            ) from exc
        if not 1 <= port <= 65535:
            raise ConfigError(f"[{section}] port 범위 오류: {port}")

        casters.append(
            CasterConfig(
                name=name,
                host=values["host"],
                port=port,
                user=values["user"],
                password=values["password"],
            )
        )

    if not casters:
        raise ConfigError(
            f"감시할 캐스터가 없습니다. [{CASTER_PREFIX}이름] 섹션을 하나 이상 "
            "주석 해제하세요."
        )

    lowered = [c.name.lower() for c in casters]
    duplicated = sorted({n for n in lowered if lowered.count(n) > 1})
    if duplicated:
        # Windows 파일명은 대소문자를 구분하지 않아 엑셀/캐시 파일이 겹친다.
        raise ConfigError(
            f"대소문자만 다른 캐스터 이름이 있습니다: {', '.join(duplicated)}"
        )

    return casters


def _looks_like_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value))


def _looks_like_host(value: str) -> bool:
    """네트워크 접속 없이 주소 형식만 검사한다. IPv6는 괄호 없이 입력한다."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    try:
        hostname = value.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return len(hostname) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in hostname.split(".")
    )


def _warn_if_readable_by_others(path: Path) -> None:
    """리눅스에서 그룹/기타 사용자가 읽을 수 있으면 경고한다. (chmod 600 권장)"""
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            f"{path.name} 권한이 {oct(mode & 0o777)}입니다. "
            f"비밀번호 보호를 위해 'chmod 600 {path.name}'을 권장합니다."
        )


# ─────────────────────────────────────────────
# 단독 실행: 설정 점검
# ─────────────────────────────────────────────
def _describe(config: AppConfig) -> List[str]:
    smtp = config.smtp
    lines = [
        f"설정 파일: {config.path}",
        f"SMTP: {smtp.host}:{smtp.port} (STARTTLS), 발신 {smtp.user}, "
        f"비밀번호 설정됨",
        f"수신: {', '.join(smtp.mail_to)}",
        f"캐스터 {len(config.casters)}개:",
    ]
    for caster in config.casters:
        lines.append(
            f"  - {caster.name}: {caster.host}:{caster.port}, "
            f"계정 {caster.user}, 비밀번호 설정됨"
        )
    return lines


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH

    try:
        config = load_config(path)
    except ConfigError as exc:
        logger.error(str(exc))
        return 1

    for line in _describe(config):
        logger.info(line)
    logger.info("설정 점검 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
