"""캐스터별 보조 마운트 목록(mounts/<캐스터명>.txt) 읽기·저장.

이 파일은 Sourcetable 조회에 실패했을 때만 쓰는 보조 목록이다.
Sourcetable을 완전히 수신할 때마다 자동으로 갱신되므로 사람이 관리하지 않는다.

파일 형식:
  - 한 줄에 마운트 하나
  - 빈 줄과 #으로 시작하는 줄은 무시
  - 마운트명 대소문자는 원본 그대로 (NTRIP 마운트명은 대소문자를 구분한다)
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import List

logger = logging.getLogger("gnss_crawler")


def clean_mount_name(value: str) -> str:
    """요청용 정리: 앞뒤 공백/선행 슬래시만 제거. 대소문자는 원본 유지."""
    return value.strip().lstrip("/")


def cache_path(mounts_dir, caster_name: str) -> Path:
    return Path(mounts_dir) / f"{caster_name}.txt"


def read_mounts(path) -> List[str]:
    """보조 목록을 읽는다. 파일이 없거나 읽기 실패면 빈 목록."""
    file_path = Path(path)

    if not file_path.exists():
        logger.warning(f"보조 마운트 목록이 없습니다: {file_path}")
        return []

    mounts = set()
    try:
        with file_path.open("r", encoding="utf-8-sig") as file:
            for line in file:
                value = line.strip()
                if not value or value.startswith("#"):
                    continue
                mount = clean_mount_name(value)
                if mount:
                    mounts.add(mount)
    except (OSError, UnicodeError) as exc:
        logger.error(f"보조 마운트 목록 읽기 실패: {exc}")
        return []

    result = sorted(mounts)
    logger.info(f"{file_path.name}에서 마운트 {len(result)}개 로드")
    return result


def write_mounts(path, mounts: List[str], source: str) -> bool:
    """완전 수신한 Sourcetable 목록을 보조 목록으로 저장한다.

    같은 폴더의 임시 파일에 다 쓴 뒤 교체한다. 저장 도중 실패해도
    기존 파일은 그대로 남는다. 반환: 저장 성공 여부.
    """
    file_path = Path(path)
    mounts = sorted({clean_mount_name(m) for m in mounts if clean_mount_name(m)})
    if not mounts:
        logger.error("저장할 마운트가 없어 보조 목록을 갱신하지 않습니다.")
        return False

    # 갱신 시각은 넣지 않는다. 목록이 같으면 파일 내용도 같아서,
    # 파일 변경(및 git 기록)이 곧 마운트 추가·삭제 이력이 된다.
    header = [
        "# 보조 마운트 목록 (자동 생성 — 직접 수정하지 마세요)",
        f"# 출처: {source} Sourcetable",
        f"# 마운트 수: {len(mounts)}",
        "# Sourcetable 조회에 실패했을 때만 이 목록으로 검사합니다.",
    ]

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
            tmp.write("\n".join(header + mounts) + "\n")
        os.replace(tmp_name, file_path)
    except OSError as exc:
        logger.error(f"보조 마운트 목록 저장 실패 (기존 파일 유지): {exc}")
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
        return False

    logger.info(f"보조 마운트 목록 갱신: {file_path} ({len(mounts)}개)")
    return True
