"""캐스터별 관측소 메타데이터(metadata/<캐스터명>.json) 읽기와 반자동 동기화.

식별자는 영문 관측소 코드다. 한글 관측소명과 소속기관은 사람이 나중에 채우는
부가 정보다. Sourcetable에는 한글 정보가 없으므로 완전 자동화는 하지 않는다.

반자동 동기화:
  - 검사할 마운트에서 관측소 코드를 식별할 수 있는데 JSON에 없으면
    빈 항목 {"station_name": "", "agency": ""}을 추가한다.
  - 사람이 채운 값은 절대 덮어쓰지 않는다.
  - 식별 규칙에 맞지 않는 이름(예: YNJU_RTCM30)은 임의로 잘라 추가하지 않는다.
  - 다른 캐스터의 파일은 건드리지 않는다.

파일 형식 (기존 station_metadata.json과 같음):
  {
    "ANHN": { "station_name": "안흥 감시국", "agency": "국립해양측위정보원" },
    "GGEO": { "station_name": "", "agency": "" }
  }
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("gnss_crawler")

FIELDS = ("station_name", "agency")

# 확인된 명명 규칙: <관측소코드>-<형식>  예) ANHN-RTCM32, GGEO-BINEX
# 하이픈이 정확히 하나이고 양쪽이 영문·숫자인 경우만 코드로 인정한다.
_MOUNT_RE = re.compile(r"^([A-Za-z0-9]{2,10})-([A-Za-z0-9.]+)$")


def metadata_path(metadata_dir, caster_name: str) -> Path:
    return Path(metadata_dir) / f"{caster_name}.json"


def station_code(mount: str) -> Optional[str]:
    """명명 규칙으로 식별 가능한 관측소 코드. 불명확하면 None."""
    match = _MOUNT_RE.match(mount.strip())
    return match.group(1).upper() if match else None


def _read_raw(path: Path) -> Optional[dict]:
    """JSON 원본을 순서 그대로 읽는다. 없으면 {}, 읽기·해석 실패면 None."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            raw = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.error(f"관측소 메타데이터 읽기 실패: {path} ({exc})")
        return None
    if not isinstance(raw, dict):
        logger.error(f"관측소 메타데이터 형식 오류(최상위가 객체가 아님): {path}")
        return None
    return raw


def load(path) -> Dict[str, dict]:
    """엑셀 writer용 메타데이터. {코드(대문자): {필드: 값}}

    빈 문자열 필드는 넣지 않는다. writer는 값이 없는 필드에 대해 엑셀에
    이미 적힌 값을 유지하므로, 자동 추가된 빈 항목이 기존 엑셀 값을
    빈칸으로 덮어쓰지 않는다.
    """
    file_path = Path(path)
    raw = _read_raw(file_path)
    if not raw:
        if raw == {} and not file_path.exists():
            logger.warning(f"관측소 메타데이터 파일이 없습니다: {file_path}")
        return {}

    metadata = {}
    for code, value in raw.items():
        normalized = str(code).strip().upper()
        if not normalized or not isinstance(value, dict):
            continue
        fields = {
            key: value[key].strip()
            for key in FIELDS
            if isinstance(value.get(key), str) and value[key].strip()
        }
        if fields:
            metadata[normalized] = fields

    logger.info(
        f"{file_path.name}에서 관측소 메타데이터 {len(metadata)}개 로드 "
        f"(전체 항목 {len(raw)}개)"
    )
    return metadata


def add_missing(path, mounts: List[str]) -> List[str]:
    """식별 가능한 미등록 코드를 빈 항목으로 추가한다. 반환: 추가한 코드 목록.

    파일을 읽지 못하면(깨진 JSON 등) 아무것도 쓰지 않는다. 사람이 고칠
    파일을 프로그램이 덮어써서 내용을 잃는 일을 막기 위해서다.
    """
    file_path = Path(path)
    raw = _read_raw(file_path)
    if raw is None:
        logger.error("관측소 메타데이터를 읽지 못해 빈 항목 추가를 건너뜁니다.")
        return []

    existing = {str(code).strip().upper() for code in raw}
    codes = {station_code(m) for m in mounts}
    added = sorted(c for c in codes if c and c not in existing)
    if not added:
        return []

    for code in added:
        raw[code] = {"station_name": "", "agency": ""}

    tmp_name = None
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=file_path.parent,
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(raw, tmp, ensure_ascii=False, indent=2)
            tmp.write("\n")
        os.replace(tmp_name, file_path)
    except OSError as exc:
        logger.error(f"관측소 메타데이터 저장 실패 (기존 파일 유지): {exc}")
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
        return []

    logger.info(
        f"{file_path.name}에 미등록 관측소 {len(added)}개를 빈 항목으로 추가: "
        f"{', '.join(added)}"
    )
    return added
