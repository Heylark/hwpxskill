#!/usr/bin/env python3
"""Regression tests for text_extract.py --tables-grid."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import text_extract  # noqa: E402

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"


def _p(*texts: str) -> str:
    return "".join(f"<hp:p><hp:run><hp:t>{t}</hp:t></hp:run></hp:p>" for t in texts)


def _tc(col: int, row: int, body: str, *, col_span: int = 1, row_span: int = 1) -> str:
    return (
        f"<hp:tc><hp:subList>{body}</hp:subList>"
        f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
        f'<hp:cellSpan colSpan="{col_span}" rowSpan="{row_span}"/></hp:tc>'
    )


NESTED = (
    '<hp:p><hp:run><hp:t>바깥</hp:t></hp:run><hp:run>'
    '<hp:tbl rowCnt="1" colCnt="1"><hp:tr>'
    + _tc(0, 0, _p("안쪽"))
    + "</hp:tr></hp:tbl></hp:run></hp:p>"
)

# 3x3 표. A 가 두 행, B 가 두 열을 덮으므로 1행에는 hp:tc 가 둘뿐이다.
# 등장 순서로 읽으면 C·D 가 0·1열로 밀리고, 좌표로 읽으면 1·2열에 놓인다.
TABLE = (
    '<hp:p><hp:run><hp:tbl rowCnt="3" colCnt="3">'
    "<hp:tr>"
    + _tc(0, 0, _p("A"), row_span=2)
    + _tc(1, 0, _p("B"), col_span=2)
    + "</hp:tr><hp:tr>"
    + _tc(1, 1, _p("C"))
    + _tc(2, 1, '<hp:p><hp:run><hp:t>D1<hp:lineBreak/>D2</hp:t></hp:run></hp:p>')
    + "</hp:tr><hp:tr>"
    + _tc(0, 2, _p("E1", "", "E2"))
    + _tc(1, 2, NESTED)
    + _tc(2, 2, _p("G"))
    + "</hp:tr></hp:tbl></hp:run></hp:p>"
)


def section_xml(body: str, prolog: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        + prolog
        + f'<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">{_p("본문")}{body}</hs:sec>'
    ).encode("utf-8")


def write_hwpx(directory: Path, section: bytes) -> Path:
    path = directory / "sample.hwpx"
    with ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("Contents/section0.xml", section)
    return path


class TablesGridTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_cells_are_placed_by_cell_addr_not_appearance_order(self) -> None:
        tables, warnings = text_extract.extract_tables_grid(
            write_hwpx(self.dir, section_xml(TABLE))
        )
        self.assertEqual([], warnings)
        outer = tables[0]
        self.assertEqual((1, 3, 3), (outer["index"], outer["rowCnt"], outer["colCnt"]))
        self.assertEqual(
            [
                ["A", "B", ""],
                ["", "C", "D1 D2"],
                ["E1 / E2", "바깥 [표 2]", "G"],
            ],
            outer["grid"],
        )
        spans = {(c["row"], c["col"]): (c["rowSpan"], c["colSpan"]) for c in outer["cells"]}
        self.assertEqual((2, 1), spans[(0, 0)])
        self.assertEqual((1, 2), spans[(0, 1)])

    def test_nested_table_gets_own_number_and_parent(self) -> None:
        tables, _ = text_extract.extract_tables_grid(
            write_hwpx(self.dir, section_xml(TABLE))
        )
        self.assertEqual(2, len(tables))
        inner = tables[1]
        self.assertEqual((2, 1, [["안쪽"]]), (inner["index"], inner["parent"], inner["grid"]))
        self.assertIsNone(tables[0]["parent"])

    def test_tsv_has_header_and_marks_merged_start_cells(self) -> None:
        tables, _ = text_extract.extract_tables_grid(
            write_hwpx(self.dir, section_xml(TABLE))
        )
        lines = text_extract.format_tables_grid_tsv(tables).split("\n")
        self.assertEqual("# 표 1\trowCnt=3\tcolCnt=3\t구역=0", lines[0])
        self.assertEqual(
            ["[병합 2행×1열] A", "[병합 1행×2열] B", ""], lines[1].split("\t")
        )
        self.assertEqual(["", "C", "D1 D2"], lines[2].split("\t"))
        self.assertIn("# 표 2\trowCnt=1\tcolCnt=1\t구역=0\t바깥표=1", lines)

    def test_rejects_dtd_and_entity_declarations(self) -> None:
        bomb = (
            '<!DOCTYPE hs:sec [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        )
        path = write_hwpx(self.dir, section_xml(TABLE, prolog=bomb))
        with self.assertRaises(text_extract.UnsafeXmlError):
            text_extract.extract_tables_grid(path)

        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "text_extract.py"), str(path), "--tables-grid"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("DTD/엔티티", result.stderr)

    def test_rejects_utf16_encoded_doctype(self) -> None:
        xml = section_xml(TABLE, prolog="<!DOCTYPE hs:sec>").decode("utf-8")
        xml = xml.replace('encoding="UTF-8"', 'encoding="UTF-16"')
        path = write_hwpx(self.dir, xml.encode("utf-16"))
        # 파싱 전 바이트 검사가 잡아야 한다. 파싱 뒤 내부 DTD 확인은 그다음 겹이라
        # 메시지("DTD 선언")가 다르다.
        with self.assertRaisesRegex(text_extract.UnsafeXmlError, "DTD/엔티티"):
            text_extract.extract_tables_grid(path)

    def test_cli_json_and_option_conflict(self) -> None:
        path = write_hwpx(self.dir, section_xml(TABLE))
        script = str(SCRIPTS / "text_extract.py")
        ok = subprocess.run(
            [sys.executable, script, str(path), "--tables-grid", "--grid-format", "json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, ok.returncode, ok.stderr)
        self.assertIn('"rowSpan": 2', ok.stdout)

        clash = subprocess.run(
            [sys.executable, script, str(path), "--tables-grid", "--format", "markdown"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(2, clash.returncode)

    def test_plain_mode_is_unchanged_by_grid_option(self) -> None:
        path = write_hwpx(self.dir, section_xml(TABLE))
        self.assertEqual("본문", text_extract.extract_plain(path))


if __name__ == "__main__":
    unittest.main()
