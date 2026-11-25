"""마운트포인트 생존 판별 결과를 기관별·형식별 시트에 누적 저장합니다.

고정 열 순서:
  A: 소속기관
  B: 관측소명
  C: 관측소코드
  D: 마운트명
  E~: 검사시각별 상태

시트 구성:
  1. BINEX
  2. RTCM23
  3. RTCM30
  4. RTCM31
  5. RTCM32
  6. 소속기관별 시트

형식별 시트는 항상 워크북 맨 앞에 위 순서대로 배치됩니다.
그 밖의 형식은 기관별 시트에만 기록됩니다.

기존 파일 호환:
  - A:관측소코드, B:관측소명, C:소속기관, D:마운트명 형식을
    새 순서로 자동 변환합니다.
  - 과거의 A:관측소코드, B:마운트명 형식도 자동 변환합니다.
  - 기존 기관별 시트의 과거 검사 이력을 형식별 시트에도 자동 반영합니다.
"""

import logging
import os
import re
from collections import defaultdict
from typing import Optional

from metadata_store import station_code as identify_station_code

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

logger = logging.getLogger("gnss_crawler")

# ── 열 위치 ──
COL_AGENCY = 1         # A
COL_STATION_NAME = 2   # B
COL_STATION_CODE = 3   # C
COL_MOUNT = 4          # D
COL_TIME_START = 5     # E

# ── 시트 설정 ──
FORMAT_SHEET_ORDER = ["BINEX", "RTCM23", "RTCM30", "RTCM31", "RTCM32"]
LEGACY_SHEET_NAME = "마운트생존"
LEGACY_BACKUP_NAME = "마운트생존_통합백업"
UNKNOWN_AGENCY = "소속미확인"
INVALID_SHEET_CHARS = re.compile(r'[\\/?*\[\]:]')

# ── 스타일 ──
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11, name="맑은 고딕")
HEADER_FILL = PatternFill("solid", fgColor="3F4551")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center")
BASE_FONT = Font(name="맑은 고딕", size=10)
CENTER_ALIGN = Alignment(horizontal="center", vertical="center")

STATUS_FILLS = {
    "양호": PatternFill("solid", fgColor="C6EFCE"),
    "무데이터": PatternFill("solid", fgColor="FFC7CE"),
    "미등록": PatternFill("solid", fgColor="D9D9D9"),
    "오류": PatternFill("solid", fgColor="FFEB9C"),
}
STATUS_FONTS = {
    "양호": Font(color="006100", name="맑은 고딕", size=10),
    "무데이터": Font(color="9C0006", name="맑은 고딕", size=10),
    "미등록": Font(color="808080", name="맑은 고딕", size=10),
    "오류": Font(color="9C6500", name="맑은 고딕", size=10),
}


def sanitize_sheet_name(name: str) -> str:
    """Excel 시트명 금지 문자와 31자 제한을 처리합니다."""
    cleaned = INVALID_SHEET_CHARS.sub("_", (name or UNKNOWN_AGENCY).strip())
    return (cleaned or UNKNOWN_AGENCY)[:31]


class LivenessExcelWriter:
    """생존 판별 결과를 기관별·마운트 형식별 시트에 누적 저장합니다.

    station_metadata 형식:
    {
        "ANHN": {
            "station_name": "안흥 감시국",
            "agency": "국립해양측위정보원"
        }
    }
    """

    def __init__(
        self,
        output_dir: str,
        output_file: str,
        station_metadata: Optional[dict] = None,
    ):
        self.output_dir = output_dir
        self.output_file = output_file
        self.station_metadata = {
            str(code).strip().upper(): value
            for code, value in (station_metadata or {}).items()
        }

    @property
    def output_path(self) -> str:
        return os.path.join(self.output_dir, self.output_file)

    def write(self, results: dict, check_time: str):
        """현재 결과를 기관별·형식별 시트에 함께 누적합니다."""
        os.makedirs(self.output_dir, exist_ok=True)

        wb = self._load_or_create_workbook()

        # 과거 통합 시트가 남아 있다면 기관별 구조로 먼저 이관합니다.
        self._migrate_legacy_sheet(wb)

        # 요청한 네 개 형식별 시트는 데이터 유무와 관계없이 항상 만듭니다.
        self._ensure_format_sheets(wb)

        # 기존 기관별 시트의 과거 검사 이력을 형식별 시트에도 반영합니다.
        self._sync_format_sheets_from_agency_sheets(wb)

        agency_groups = self._group_results_by_agency(results)
        format_groups = self._group_results_by_format(results)

        # 기관별 시트 기록
        for agency, agency_results in sorted(agency_groups.items()):
            ws = self._get_or_create_sheet(wb, agency, kind="기관")
            self._ensure_schema(ws)
            self._backfill_metadata(ws)

            time_col = self._insert_time_col(ws, check_time)
            self._write_results(ws, agency_results, time_col)

        # 마운트 형식별 시트 기록
        for mount_type in FORMAT_SHEET_ORDER:
            ws = self._get_or_create_sheet(wb, mount_type, kind="형식")
            self._ensure_schema(ws)
            self._backfill_metadata(ws)

            type_results = format_groups.get(mount_type, {})
            if type_results:
                time_col = self._insert_time_col(ws, check_time)
                self._write_results(ws, type_results, time_col)

        self._remove_default_sheet(wb)
        self._move_format_sheets_to_front(wb)
        wb.save(self.output_path)

        alive = sum(1 for result in results.values() if result.status == "양호")
        logger.info(
            f"기관별·형식별 생존 판별 엑셀 저장 완료: {self.output_path} "
            f"(기관 {len(agency_groups)}개, 마운트 {len(results)}개, "
            f"양호 {alive}개, 시각 {check_time})"
        )

    # ─────────────────────────────────────────────
    # 결과 그룹핑
    # ─────────────────────────────────────────────
    def _group_results_by_agency(self, results: dict) -> dict:
        grouped = defaultdict(dict)
        missing_codes = set()

        for mount, result in results.items():
            station_code = self._station_code_from_mount(mount)
            metadata = self.station_metadata.get(station_code, {})
            agency = (metadata.get("agency") or UNKNOWN_AGENCY).strip()

            if not metadata:
                missing_codes.add(station_code)

            grouped[agency][mount] = result

        if missing_codes:
            preview = ", ".join(sorted(missing_codes)[:10])
            logger.debug(
                f"관측소 메타데이터 미확인 코드 {len(missing_codes)}개: {preview}"
            )

        return dict(grouped)

    @staticmethod
    def _group_results_by_format(results: dict) -> dict:
        grouped = defaultdict(dict)

        for mount, result in results.items():
            mount_type = LivenessExcelWriter._mount_type_from_mount(mount)
            if mount_type in FORMAT_SHEET_ORDER:
                grouped[mount_type][mount] = result

        return dict(grouped)

    # ─────────────────────────────────────────────
    # 워크북/시트
    # ─────────────────────────────────────────────
    def _load_or_create_workbook(self) -> Workbook:
        if os.path.exists(self.output_path):
            wb = load_workbook(self.output_path)
            logger.info(f"기존 파일 열기: {self.output_path}")
        else:
            wb = Workbook()
            logger.info(f"새 엑셀 파일 생성: {self.output_path}")
        return wb

    def _get_or_create_sheet(self, wb: Workbook, name: str, kind: str = ""):
        sheet_name = sanitize_sheet_name(name)

        if sheet_name in wb.sheetnames:
            return wb[sheet_name]

        ws = wb.create_sheet(sheet_name)
        self._create_header(ws)
        logger.info(f"{kind} 시트 생성: {sheet_name}")
        return ws

    def _ensure_format_sheets(self, wb: Workbook):
        for sheet_name in FORMAT_SHEET_ORDER:
            ws = self._get_or_create_sheet(wb, sheet_name, kind="형식")
            self._ensure_schema(ws)

    @staticmethod
    def _remove_default_sheet(wb: Workbook):
        if "Sheet" in wb.sheetnames and len(wb.sheetnames) > 1:
            default_ws = wb["Sheet"]
            if default_ws.max_row <= 1 and default_ws.max_column <= 1:
                wb.remove(default_ws)

    @staticmethod
    def _move_format_sheets_to_front(wb: Workbook):
        """BINEX, RTCM23, RTCM30, RTCM31, RTCM32를 정확한 순서로 맨 앞에 배치합니다."""
        front = [wb[name] for name in FORMAT_SHEET_ORDER if name in wb.sheetnames]
        remaining = [ws for ws in wb._sheets if ws.title not in FORMAT_SHEET_ORDER]
        wb._sheets = front + remaining

    # ─────────────────────────────────────────────
    # 기존 통합 시트 자동 이관
    # ─────────────────────────────────────────────
    def _migrate_legacy_sheet(self, wb: Workbook):
        """기존 마운트생존 통합 시트를 기관별 시트로 이관하고 숨김 백업합니다."""
        if LEGACY_SHEET_NAME not in wb.sheetnames:
            return

        legacy_ws = wb[LEGACY_SHEET_NAME]
        self._ensure_schema(legacy_ws)
        self._backfill_metadata(legacy_ws)

        time_headers = [
            (col, legacy_ws.cell(row=1, column=col).value)
            for col in range(COL_TIME_START, legacy_ws.max_column + 1)
            if legacy_ws.cell(row=1, column=col).value
        ]

        target_cache = {}
        time_col_cache = {}
        migrated_rows = 0

        for row in range(2, legacy_ws.max_row + 1):
            mount = legacy_ws.cell(row=row, column=COL_MOUNT).value
            if not mount:
                continue

            mount = str(mount)
            station_code = self._station_code_from_mount(mount)
            metadata = self.station_metadata.get(station_code, {})

            agency = (
                metadata.get("agency")
                or legacy_ws.cell(row=row, column=COL_AGENCY).value
                or UNKNOWN_AGENCY
            )
            agency = str(agency).strip() or UNKNOWN_AGENCY
            sheet_name = sanitize_sheet_name(agency)

            if sheet_name not in target_cache:
                target_ws = self._get_or_create_sheet(wb, agency, kind="기관")
                self._ensure_schema(target_ws)
                target_cache[sheet_name] = target_ws

                time_mapping = {}
                # 과거의 시간 순서를 유지하기 위해 오른쪽부터 삽입합니다.
                for _, header in reversed(time_headers):
                    target_col = self._insert_time_col(target_ws, str(header))
                    time_mapping[str(header)] = target_col
                time_col_cache[sheet_name] = time_mapping
            else:
                target_ws = target_cache[sheet_name]

            target_row = self._find_or_create_mount_row(target_ws, mount)
            station_name = (
                metadata.get("station_name")
                or legacy_ws.cell(row=row, column=COL_STATION_NAME).value
                or ""
            )

            self._write_cell(target_ws, target_row, COL_AGENCY, agency)
            self._write_cell(target_ws, target_row, COL_STATION_NAME, station_name)
            self._write_cell(target_ws, target_row, COL_STATION_CODE, station_code)
            self._write_cell(target_ws, target_row, COL_MOUNT, mount)

            for source_col, header in time_headers:
                status = legacy_ws.cell(row=row, column=source_col).value
                if status in (None, ""):
                    continue
                target_col = time_col_cache[sheet_name][str(header)]
                self._write_status_cell(target_ws, target_row, target_col, status)

            migrated_rows += 1

        backup_name = self._unique_sheet_name(wb, LEGACY_BACKUP_NAME)
        legacy_ws.title = backup_name
        legacy_ws.sheet_state = "hidden"

        logger.info(
            f"기존 통합 시트 자동 이관 완료: {migrated_rows}행 → "
            f"기관 {len(target_cache)}개 시트. "
            f"기존 시트는 '{backup_name}'으로 숨김 보관했습니다."
        )

    @staticmethod
    def _unique_sheet_name(wb: Workbook, base_name: str) -> str:
        candidate = sanitize_sheet_name(base_name)
        if candidate not in wb.sheetnames:
            return candidate

        number = 2
        while True:
            suffix = f"_{number}"
            candidate = sanitize_sheet_name(base_name[:31 - len(suffix)] + suffix)
            if candidate not in wb.sheetnames:
                return candidate
            number += 1

    # ─────────────────────────────────────────────
    # 기관 시트의 과거 이력 → 형식별 시트 동기화
    # ─────────────────────────────────────────────
    def _sync_format_sheets_from_agency_sheets(self, wb: Workbook):
        """기존 기관별 시트의 모든 과거 상태를 형식별 시트에 복사합니다.

        동일 마운트·동일 검사시각은 같은 셀에 덮어쓰므로 반복 실행해도 중복되지 않습니다.
        """
        excluded_names = set(FORMAT_SHEET_ORDER)
        excluded_names.add(LEGACY_SHEET_NAME)

        source_sheets = [
            ws for ws in wb.worksheets
            if ws.title not in excluded_names
            and not ws.title.startswith(LEGACY_BACKUP_NAME)
            and ws.sheet_state == "visible"
            and ws.title != "Sheet"
        ]

        copied_cells = 0

        for source_ws in source_sheets:
            self._ensure_schema(source_ws)
            self._backfill_metadata(source_ws)

            time_headers = [
                (col, source_ws.cell(row=1, column=col).value)
                for col in range(COL_TIME_START, source_ws.max_column + 1)
                if source_ws.cell(row=1, column=col).value
            ]

            for row in range(2, source_ws.max_row + 1):
                mount = source_ws.cell(row=row, column=COL_MOUNT).value
                if not mount:
                    continue

                mount = str(mount)
                mount_type = self._mount_type_from_mount(mount)
                if mount_type not in FORMAT_SHEET_ORDER:
                    continue

                target_ws = self._get_or_create_sheet(wb, mount_type, kind="형식")
                self._ensure_schema(target_ws)
                target_row = self._find_or_create_mount_row(target_ws, mount)

                agency = source_ws.cell(row=row, column=COL_AGENCY).value or ""
                station_name = source_ws.cell(row=row, column=COL_STATION_NAME).value or ""
                station_code = source_ws.cell(row=row, column=COL_STATION_CODE).value
                if not station_code:
                    station_code = self._station_code_from_mount(mount)

                self._write_cell(target_ws, target_row, COL_AGENCY, agency)
                self._write_cell(target_ws, target_row, COL_STATION_NAME, station_name)
                self._write_cell(target_ws, target_row, COL_STATION_CODE, station_code)
                self._write_cell(target_ws, target_row, COL_MOUNT, mount)

                # 오른쪽부터 삽입해야 원본의 최신→과거 열 순서를 유지합니다.
                for source_col, header in reversed(time_headers):
                    status = source_ws.cell(row=row, column=source_col).value
                    if status in (None, ""):
                        continue
                    target_col = self._insert_time_col(target_ws, str(header))
                    self._write_status_cell(target_ws, target_row, target_col, status)
                    copied_cells += 1

        if copied_cells:
            logger.info(f"기존 기관별 이력 → 형식별 시트 동기화: 상태 {copied_cells}셀")

    # ─────────────────────────────────────────────
    # 스키마/메타데이터
    # ─────────────────────────────────────────────
    def _ensure_schema(self, ws):
        """과거 열 순서를 새 열 순서로 자동 변환합니다.

        새 형식:
          A 소속기관 | B 관측소명 | C 관측소코드 | D 마운트명 | E~ 검사시각
        """
        headers = [ws.cell(row=1, column=col).value for col in range(1, 5)]

        # 아주 옛 형식: A 관측소코드 | B 마운트명 | C~ 검사시각
        if headers[0] == "관측소코드" and headers[1] == "마운트명":
            # 앞에 두 열을 추가하면 기존 A/B는 C/D로, 시간 열은 E 이후로 이동합니다.
            ws.insert_cols(1, amount=2)
            logger.info(
                f"[{ws.title}] 2열 형식 감지 → 소속기관/관측소명 열 추가"
            )

        else:
            headers = [ws.cell(row=1, column=col).value for col in range(1, 5)]

            # 이전 형식: A 관측소코드 | B 관측소명 | C 소속기관 | D 마운트명
            if headers == ["관측소코드", "관측소명", "소속기관", "마운트명"]:
                for row in range(2, ws.max_row + 1):
                    old_code = ws.cell(row=row, column=1).value
                    old_name = ws.cell(row=row, column=2).value
                    old_agency = ws.cell(row=row, column=3).value
                    old_mount = ws.cell(row=row, column=4).value

                    ws.cell(row=row, column=1, value=old_agency)
                    ws.cell(row=row, column=2, value=old_name)
                    ws.cell(row=row, column=3, value=old_code)
                    ws.cell(row=row, column=4, value=old_mount)

                logger.info(
                    f"[{ws.title}] 고정 열 순서 변경: "
                    "소속기관 → 관측소명 → 관측소코드 → 마운트명"
                )

        desired_headers = {
            COL_AGENCY: "소속기관",
            COL_STATION_NAME: "관측소명",
            COL_STATION_CODE: "관측소코드",
            COL_MOUNT: "마운트명",
        }
        for col, text in desired_headers.items():
            cell = ws.cell(row=1, column=col, value=text)
            self._style_header_cell(cell)

        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 22
        ws.column_dimensions["C"].width = 14
        ws.column_dimensions["D"].width = 20
        ws.freeze_panes = "E2"

    def _backfill_metadata(self, ws):
        """기존 행의 기관명·관측소명·코드를 최신 JSON 값으로 채웁니다."""
        missing_codes = set()

        for row in range(2, ws.max_row + 1):
            mount = ws.cell(row=row, column=COL_MOUNT).value
            if not mount:
                continue

            mount = str(mount)
            station_code = self._station_code_from_mount(mount)
            metadata = self.station_metadata.get(station_code, {})

            agency = metadata.get(
                "agency",
                ws.cell(row=row, column=COL_AGENCY).value or "",
            )
            station_name = metadata.get(
                "station_name",
                ws.cell(row=row, column=COL_STATION_NAME).value or "",
            )

            self._write_cell(ws, row, COL_AGENCY, agency)
            self._write_cell(ws, row, COL_STATION_NAME, station_name)
            self._write_cell(ws, row, COL_STATION_CODE, station_code)
            self._write_cell(ws, row, COL_MOUNT, mount)

            if not metadata:
                missing_codes.add(station_code)

        if missing_codes:
            preview = ", ".join(sorted(missing_codes)[:10])
            logger.debug(
                f"[{ws.title}] 관측소 메타데이터 미확인 코드 "
                f"{len(missing_codes)}개: {preview}"
            )

    @staticmethod
    def _station_code_from_mount(mount: str) -> str:
        return identify_station_code(mount) or ""

    @staticmethod
    def _mount_type_from_mount(mount: str) -> str:
        if "-" not in mount:
            return ""
        return mount.rsplit("-", 1)[1].strip().upper()

    # ─────────────────────────────────────────────
    # 시각 열 / 결과 기록
    # ─────────────────────────────────────────────
    def _insert_time_col(self, ws, check_time: str) -> int:
        for col in range(COL_TIME_START, ws.max_column + 1):
            if str(ws.cell(row=1, column=col).value) == str(check_time):
                return col

        if ws.max_column >= COL_TIME_START:
            ws.insert_cols(COL_TIME_START)

        cell = ws.cell(row=1, column=COL_TIME_START, value=check_time)
        self._style_header_cell(cell)
        ws.column_dimensions["E"].width = 18
        return COL_TIME_START

    def _write_results(self, ws, results: dict, time_col: int):
        existing = {
            str(ws.cell(row=row, column=COL_MOUNT).value): row
            for row in range(2, ws.max_row + 1)
            if ws.cell(row=row, column=COL_MOUNT).value
        }

        next_row = ws.max_row + 1 if ws.max_row >= 2 else 2

        for mount in sorted(results):
            result = results[mount]
            station_code = self._station_code_from_mount(mount)
            metadata = self.station_metadata.get(station_code, {})

            if mount in existing:
                row = existing[mount]
            else:
                row = next_row
                existing[mount] = row
                next_row += 1

            # JSON에 값이 없으면 기존 엑셀 칸을 유지한다.
            # 빈 항목은 load()에서 빠지므로, 없는 키를 빈칸으로 덮어쓰지 않는다.
            agency = metadata.get(
                "agency",
                ws.cell(row=row, column=COL_AGENCY).value or UNKNOWN_AGENCY,
            )
            station_name = metadata.get(
                "station_name", ws.cell(row=row, column=COL_STATION_NAME).value or ""
            )

            self._write_cell(ws, row, COL_AGENCY, agency)
            self._write_cell(ws, row, COL_STATION_NAME, station_name)
            self._write_cell(ws, row, COL_STATION_CODE, station_code)
            self._write_cell(ws, row, COL_MOUNT, mount)
            self._write_status_cell(ws, row, time_col, result.status)

    def _find_or_create_mount_row(self, ws, mount: str) -> int:
        for row in range(2, ws.max_row + 1):
            if str(ws.cell(row=row, column=COL_MOUNT).value) == mount:
                return row
        return ws.max_row + 1 if ws.max_row >= 2 else 2

    # ─────────────────────────────────────────────
    # 헤더/셀 스타일
    # ─────────────────────────────────────────────
    def _create_header(self, ws):
        headers = {
            COL_AGENCY: "소속기관",
            COL_STATION_NAME: "관측소명",
            COL_STATION_CODE: "관측소코드",
            COL_MOUNT: "마운트명",
        }
        for col, text in headers.items():
            cell = ws.cell(row=1, column=col, value=text)
            self._style_header_cell(cell)

        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 22
        ws.column_dimensions["C"].width = 14
        ws.column_dimensions["D"].width = 20
        ws.freeze_panes = "E2"

    @staticmethod
    def _style_header_cell(cell):
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER

    @staticmethod
    def _write_cell(ws, row: int, col: int, value):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font = BASE_FONT
        cell.alignment = CENTER_ALIGN
        cell.border = THIN_BORDER

    @staticmethod
    def _write_status_cell(ws, row: int, col: int, status: str):
        cell = ws.cell(row=row, column=col, value=status)
        cell.alignment = CENTER_ALIGN
        cell.border = THIN_BORDER
        cell.fill = STATUS_FILLS.get(status, PatternFill())
        cell.font = STATUS_FONTS.get(status, BASE_FONT)
