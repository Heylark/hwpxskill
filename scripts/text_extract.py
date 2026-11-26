#!/usr/bin/env python3
"""Extract text from an HWPX document by reading OWPML XML directly.

Usage:
    python3 scripts/text_extract.py document.hwpx
    python3 scripts/text_extract.py document.hwpx --format markdown
    python3 scripts/text_extract.py document.hwpx --include-tables
    python3 scripts/text_extract.py document.hwpx --tables-grid
    python3 scripts/text_extract.py document.hwpx --tables-grid --grid-format json
"""

from __future__ import annotations

import argparse
import json
import sys
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from lxml import etree

NS = {
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
}


def _parse_xml(data: bytes) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False)
    return etree.parse(BytesIO(data), parser).getroot()


def _text_of(scope: etree._Element) -> str:
    return "".join(
        "".join(t.itertext())
        for t in scope.xpath(".//hp:t", namespaces=NS)
    )


def _direct_run_text_of(paragraph: etree._Element) -> str:
    return "".join(
        "".join(t.itertext())
        for t in paragraph.xpath("./hp:run/hp:t", namespaces=NS)
    )


def _section_names(zf: ZipFile) -> list[str]:
    return sorted(
        name
        for name in zf.namelist()
        if name.startswith("Contents/section") and name.endswith(".xml")
    )


def _top_level_paragraphs(section: etree._Element) -> list[etree._Element]:
    paragraphs = section.xpath("./hp:p", namespaces=NS)
    if paragraphs:
        return paragraphs
    return section.xpath(".//hs:sec/hp:p", namespaces=NS)


def _paragraph_is_inside_table(p: etree._Element) -> bool:
    parent = p.getparent()
    while parent is not None:
        if etree.QName(parent).localname == "tc":
            return True
        parent = parent.getparent()
    return False


def extract_plain(hwpx_path: Path, *, include_tables: bool = False) -> str:
    lines: list[str] = []
    try:
        zf = ZipFile(hwpx_path, "r")
    except BadZipFile:
        raise SystemExit(f"올바른 ZIP/HWPX 파일이 아닙니다: {hwpx_path}")

    with zf:
        for section_name in _section_names(zf):
            root = _parse_xml(zf.read(section_name))
            query = ".//hp:p" if include_tables else "./hp:p"
            paragraphs = root.xpath(query, namespaces=NS)
            for p in paragraphs:
                if not include_tables and _paragraph_is_inside_table(p):
                    continue
                text = (
                    _text_of(p) if include_tables else _direct_run_text_of(p)
                ).strip()
                if text:
                    lines.append(text)
    return "\n".join(lines)


def extract_markdown(hwpx_path: Path) -> str:
    lines: list[str] = []
    try:
        zf = ZipFile(hwpx_path, "r")
    except BadZipFile:
        raise SystemExit(f"올바른 ZIP/HWPX 파일이 아닙니다: {hwpx_path}")

    with zf:
        for section_idx, section_name in enumerate(_section_names(zf)):
            if section_idx:
                lines.extend(["", "---", ""])
            root = _parse_xml(zf.read(section_name))

            for p in _top_level_paragraphs(root):
                own_text = _direct_run_text_of(p).strip()
                if own_text:
                    lines.append(own_text)

                for cell in p.xpath(".//hp:tc", namespaces=NS):
                    cell_text = _text_of(cell).strip()
                    if cell_text:
                        lines.append(f"  {cell_text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 표 격자 추출 (--tables-grid)
#
# hp:tc 를 등장 순서대로 읽으면 위 행에서 rowSpan 으로 내려온 병합 셀 자리만큼
# 아래 행의 hp:tc 개수가 줄어들어, 그 행의 값이 왼쪽으로 밀린다. 그래서 각 셀을
# hp:cellAddr(colAddr·rowAddr) 좌표에 놓고 hp:cellSpan(colSpan·rowSpan)으로 덮는
# 범위를 기록한다. 셀 안 표(중첩 표)는 별도 번호를 받아 따로 출력하고, 바깥 셀에는
# "[표 N]" 자리 표시만 남긴다.
# ---------------------------------------------------------------------------

_FORBIDDEN_DECLS = (b"<!DOCTYPE", b"<!ENTITY")
# 섹션 XML 이 UTF-16 으로 저장돼 있어도 바이트 검사가 빠지지 않도록 두 인코딩을 함께 본다.
_FORBIDDEN_PATTERNS = tuple(
    decl.decode("ascii").encode(enc)
    for decl in _FORBIDDEN_DECLS
    for enc in ("utf-8", "utf-16-le", "utf-16-be")
)


class UnsafeXmlError(ValueError):
    """DTD 또는 엔티티 선언이 들어 있는 XML."""


def _parse_untrusted_xml(data: bytes, name: str) -> etree._Element:
    """외부에서 받은 문서의 XML 을 파싱한다.

    DTD·엔티티 선언이 있으면 파싱하기 전에 거부한다(XXE·엔티티 폭탄). 바이트 검사를
    우회한 경우에 대비해 파서도 DTD 를 읽지 않고 네트워크에 접근하지 않도록 두고,
    파싱 뒤 내부 DTD 가 남아 있는지 한 번 더 확인한다.
    """
    for pattern in _FORBIDDEN_PATTERNS:
        if pattern in data:
            raise UnsafeXmlError(
                f"{name}: DTD/엔티티 선언이 있어 읽지 않습니다 (XXE·엔티티 폭탄 방지)"
            )
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
    )
    tree = etree.parse(BytesIO(data), parser)
    if tree.docinfo.internalDTD is not None:
        raise UnsafeXmlError(
            f"{name}: DTD 선언이 있어 읽지 않습니다 (XXE·엔티티 폭탄 방지)"
        )
    return tree.getroot()


_HP = NS["hp"]
_TBL = f"{{{_HP}}}tbl"
_T = f"{{{_HP}}}t"
# hp:t 안에서 글자 사이를 띄우는 컨트롤. 그대로 두면 앞뒤 낱말이 붙는다.
_SPACING_CONTROLS = {"lineBreak", "tab", "fwSpace", "nbSpace"}


def _t_text(t: etree._Element) -> str:
    parts = [t.text or ""]
    for child in t:
        if etree.QName(child).localname in _SPACING_CONTROLS:
            parts.append(" ")
        parts.append(child.tail or "")
    return "".join(parts)


def _collect_text(el: etree._Element, buf: list[str], numbers: dict) -> None:
    if el.tag == _TBL:
        # 앞뒤 글자와 붙지 않게 공백으로 감싼다. 겹친 공백은 _cell_text 가 하나로 줄인다.
        buf.append(f" [표 {numbers[el]}] ")
        return
    if el.tag == _T:
        buf.append(_t_text(el))
        return
    for child in el:
        if isinstance(child.tag, str):
            _collect_text(child, buf, numbers)


def _cell_text(tc: etree._Element, numbers: dict) -> str:
    paragraphs: list[str] = []
    for p in tc.xpath("./hp:subList/hp:p", namespaces=NS):
        buf: list[str] = []
        _collect_text(p, buf, numbers)
        text = " ".join("".join(buf).split())
        if text:
            paragraphs.append(text)
    return " / ".join(paragraphs)


def _int_attr(el: etree._Element | None, name: str) -> int | None:
    if el is None:
        return None
    value = el.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _grid_of_table(
    tbl: etree._Element, numbers: dict, label: str
) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    cells: list[dict] = []
    for tc in tbl.xpath("./hp:tr/hp:tc", namespaces=NS):
        addr = tc.find(f"{{{_HP}}}cellAddr")
        span = tc.find(f"{{{_HP}}}cellSpan")
        row = _int_attr(addr, "rowAddr")
        col = _int_attr(addr, "colAddr")
        if row is None or col is None or row < 0 or col < 0:
            raise SystemExit(
                f"{label}: hp:cellAddr 가 없거나 잘못된 셀이 있어 격자를 만들 수 없습니다"
            )
        cells.append({
            "row": row,
            "col": col,
            "rowSpan": max(_int_attr(span, "rowSpan") or 1, 1),
            "colSpan": max(_int_attr(span, "colSpan") or 1, 1),
            "text": _cell_text(tc, numbers),
        })

    declared_rows = _int_attr(tbl, "rowCnt")
    declared_cols = _int_attr(tbl, "colCnt")
    used_rows = max((c["row"] + c["rowSpan"] for c in cells), default=0)
    used_cols = max((c["col"] + c["colSpan"] for c in cells), default=0)
    row_cnt = declared_rows if declared_rows is not None else used_rows
    col_cnt = declared_cols if declared_cols is not None else used_cols
    if used_rows > row_cnt or used_cols > col_cnt:
        warnings.append(
            f"{label}: 셀 좌표가 선언 크기 rowCnt={row_cnt} colCnt={col_cnt} 를 넘습니다"
            f" (실제 {used_rows}행 {used_cols}열) — 격자를 넓혀 출력합니다"
        )
        row_cnt, col_cnt = max(row_cnt, used_rows), max(col_cnt, used_cols)

    grid: list[list[str]] = [["" for _ in range(col_cnt)] for _ in range(row_cnt)]
    owner: dict[tuple[int, int], tuple[int, int]] = {}
    for c in sorted(cells, key=lambda c: (c["row"], c["col"])):
        start = (c["row"], c["col"])
        for r in range(c["row"], c["row"] + c["rowSpan"]):
            for k in range(c["col"], c["col"] + c["colSpan"]):
                if (r, k) in owner:
                    warnings.append(
                        f"{label}: ({r},{k}) 칸을 {owner[(r, k)]} 셀과 {start} 셀이 함께 덮습니다"
                    )
                else:
                    owner[(r, k)] = start
        grid[c["row"]][c["col"]] = c["text"]

    table = {
        "rowCnt": row_cnt,
        "colCnt": col_cnt,
        "cells": cells,
        "grid": grid,
    }
    return table, warnings


def extract_tables_grid(hwpx_path: Path) -> tuple[list[dict], list[str]]:
    """문서의 모든 표를 (행, 열) 격자로 돌려준다.

    표 번호는 구역 순서·문서 순서대로 1부터 매기며 셀 안 표도 번호를 받는다.
    반환값은 (표 목록, 경고 목록)이다.
    """
    try:
        zf = ZipFile(hwpx_path, "r")
    except BadZipFile:
        raise SystemExit(f"올바른 ZIP/HWPX 파일이 아닙니다: {hwpx_path}")

    tables: list[dict] = []
    warnings: list[str] = []
    with zf:
        roots = [
            (idx, _parse_untrusted_xml(zf.read(name), name))
            for idx, name in enumerate(_section_names(zf))
        ]
        numbers: dict = {}
        order: list[tuple[int, etree._Element]] = []
        for section_idx, root in roots:
            for tbl in root.iter(_TBL):
                numbers[tbl] = len(numbers) + 1
                order.append((section_idx, tbl))

        for section_idx, tbl in order:
            number = numbers[tbl]
            parent = next(
                (a for a in tbl.iterancestors(_TBL)),
                None,
            )
            table, table_warnings = _grid_of_table(tbl, numbers, f"표 {number}")
            tables.append({
                "index": number,
                "section": section_idx,
                "parent": numbers[parent] if parent is not None else None,
                **table,
            })
            warnings.extend(table_warnings)
    return tables, warnings


def _tsv_cell(text: str) -> str:
    return " ".join(text.replace("\t", " ").split())


def format_tables_grid_tsv(tables: list[dict]) -> str:
    """표마다 머리 줄(번호·rowCnt·colCnt) 뒤에 rowCnt 줄 × colCnt 칸 TSV 를 둔다.

    병합 셀은 시작 좌표 칸에만 "[병합 R행×C열] 값" 으로 쓰고 덮인 칸은 비운다.
    """
    blocks: list[str] = []
    for t in tables:
        head = f"# 표 {t['index']}\trowCnt={t['rowCnt']}\tcolCnt={t['colCnt']}\t구역={t['section']}"
        if t["parent"] is not None:
            head += f"\t바깥표={t['parent']}"
        grid = [list(row) for row in t["grid"]]
        for c in t["cells"]:
            if c["rowSpan"] > 1 or c["colSpan"] > 1:
                value = grid[c["row"]][c["col"]]
                marker = f"[병합 {c['rowSpan']}행×{c['colSpan']}열]"
                grid[c["row"]][c["col"]] = f"{marker} {value}".rstrip()
        lines = [head] + ["\t".join(_tsv_cell(v) for v in row) for row in grid]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_tables_grid_json(tables: list[dict]) -> str:
    return json.dumps(tables, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract text from an HWPX document"
    )
    parser.add_argument("input", type=Path, help="Path to .hwpx file")
    parser.add_argument(
        "--format", "-f",
        choices=["plain", "markdown"],
        default="plain",
        help="Output format (default: plain)",
    )
    parser.add_argument(
        "--include-tables",
        action="store_true",
        help="Include text from tables and nested objects in plain mode",
    )
    parser.add_argument(
        "--tables-grid",
        action="store_true",
        help=(
            "Output only tables as (row, col) grids placed by hp:cellAddr/"
            "hp:cellSpan, so merged cells do not shift columns"
        ),
    )
    parser.add_argument(
        "--grid-format",
        choices=["tsv", "json"],
        default="tsv",
        help="Grid output format with --tables-grid (default: tsv)",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        help="Output file path (default: stdout)",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"Error: File not found: {args.input}", file=sys.stderr)
        return 2

    if args.tables_grid:
        if args.format != "plain" or args.include_tables:
            parser.error("--tables-grid 는 --format/--include-tables 와 함께 쓸 수 없습니다")
        try:
            tables, warnings = extract_tables_grid(args.input)
        except (UnsafeXmlError, etree.XMLSyntaxError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        if args.grid_format == "json":
            result = format_tables_grid_json(tables)
        else:
            result = format_tables_grid_tsv(tables)
    elif args.format == "markdown":
        result = extract_markdown(args.input)
    else:
        result = extract_plain(args.input, include_tables=args.include_tables)

    if args.output:
        args.output.write_text(result, encoding="utf-8")
        print(f"Extracted to: {args.output}", file=sys.stderr)
    else:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
