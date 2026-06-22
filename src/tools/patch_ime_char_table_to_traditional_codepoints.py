#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Patch IMECharTable.asm candidate codepoints to Traditional-character codepoints.

Why:
- IMECharTable.asm does not look up src/charmap.txt by character.
- It stores raw DFS codepoint bytes generated for the original CHS IME.
- After full CJK / Big5 extension, src/charmap.txt may correctly separate:
    怀=<old CHS codepoint>
    懷=<new CHT/extension codepoint>
  but the IME still points at the old 怀 codepoint.
- This tool rewrites the IME candidate table so old simplified-code candidates point
  to the current Traditional codepoint when one exists.

This keeps glyph/codepoint consistency. It does NOT redraw the glyph of 怀 as 懷.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover
    OpenCC = None  # type: ignore

VERSION = "patch-ime-char-table-to-traditional-codepoints-2026-06-19-v1"

CHARMAP_TXT_RE = re.compile(r"^(.+?)=\$?([0-9A-Fa-f]{4})\s*(?:;.*)?$")
LABEL_RE = re.compile(r"^\s*(IME_[A-Za-z0-9_]+_Char):")
BYTE_RE = re.compile(r"\$([0-9A-Fa-f]{2})")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass(frozen=True)
class CharmapEntry:
    char: str
    code: int
    line_no: int

    @property
    def code_hex(self) -> str:
        return f"${self.code:04X}"


@dataclass
class ImeBlock:
    label: str
    original_lines: List[str]
    bytes_: List[int]


def is_cjk(ch: str) -> bool:
    return len(ch) == 1 and bool(CJK_RE.fullmatch(ch))


def load_charmap(path: Path) -> Tuple[Dict[str, CharmapEntry], Dict[int, CharmapEntry]]:
    by_char: Dict[str, CharmapEntry] = {}
    by_code: Dict[int, CharmapEntry] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        s = raw.strip()
        m = CHARMAP_TXT_RE.match(s)
        if not m:
            continue
        ch = m.group(1).strip()
        if len(ch) != 1:
            continue
        code = int(m.group(2), 16)
        entry = CharmapEntry(ch, code, line_no)
        by_char.setdefault(ch, entry)
        by_code.setdefault(code, entry)
    return by_char, by_code


def make_opencc(mode: str):
    if mode.lower() in {"none", "identity", "raw"}:
        class Identity:
            def convert(self, s: str) -> str:
                return s
        return Identity()
    if OpenCC is None:
        raise SystemExit(
            "Missing dependency: opencc-python-reimplemented. "
            "Install with: python3 -m pip install --user opencc-python-reimplemented"
        )
    try:
        return OpenCC(mode)
    except Exception:
        return OpenCC(mode + ".json")


def parse_ime_blocks(path: Path) -> Tuple[List[str], List[ImeBlock]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    preamble: List[str] = []
    blocks: List[ImeBlock] = []

    current_label: Optional[str] = None
    current_lines: List[str] = []

    def flush() -> None:
        nonlocal current_label, current_lines
        if current_label is None:
            return
        vals: List[int] = []
        for raw in current_lines:
            line = raw.split(";", 1)[0]
            if "db" not in line:
                continue
            for hx in BYTE_RE.findall(line):
                vals.append(int(hx, 16))
        blocks.append(ImeBlock(current_label, current_lines[:], vals))
        current_label = None
        current_lines = []

    for raw in lines:
        m = LABEL_RE.match(raw)
        if m:
            flush()
            current_label = m.group(1)
            current_lines = [raw]
        else:
            if current_label is None:
                preamble.append(raw)
            else:
                current_lines.append(raw)
    flush()
    return preamble, blocks


def bytes_to_codes(vals: List[int]) -> Tuple[List[int], bool]:
    out: List[int] = []
    saw_term = False
    usable: List[int] = []
    for b in vals:
        if b == 0x50:
            saw_term = True
            break
        usable.append(b)
    for i in range(0, len(usable) - 1, 2):
        out.append((usable[i] << 8) | usable[i + 1])
    return out, saw_term


def codes_to_db_lines(codes: List[int], per_line: int = 16) -> List[str]:
    lines: List[str] = []
    byte_tokens: List[str] = []
    for code in codes:
        byte_tokens.append(f"${(code >> 8) & 0xFF:02X}")
        byte_tokens.append(f"${code & 0xFF:02X}")

    # Split by code count, not raw byte count.
    for start in range(0, len(codes), per_line):
        chunk_codes = codes[start:start + per_line]
        tokens: List[str] = []
        for code in chunk_codes:
            tokens.append(f"${(code >> 8) & 0xFF:02X}")
            tokens.append(f"${code & 0xFF:02X}")
        if start + per_line >= len(codes):
            tokens.append("$50")
        lines.append("\t db " + ",".join(tokens))
    if not codes:
        lines.append("\t db $50")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Patch IMECharTable.asm candidate codepoints to current Traditional codepoints.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base-charmap", default="reports/charmap_chs_base.txt")
    ap.add_argument("--current-charmap", default="src/charmap.txt")
    ap.add_argument("--ime", default="src/dfs/IMECharTable.asm")
    ap.add_argument("--mode", default="s2twp")
    ap.add_argument("--out", default="reports/cht_ime_codepoint_patch")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--per-line", type=int, default=16)
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()

    base_charmap = Path(args.base_charmap)
    if not base_charmap.is_absolute():
        base_charmap = repo / base_charmap

    current_charmap = Path(args.current_charmap)
    if not current_charmap.is_absolute():
        current_charmap = repo / current_charmap

    ime_path = Path(args.ime)
    if not ime_path.is_absolute():
        ime_path = repo / ime_path

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = repo / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    _, base_by_code = load_charmap(base_charmap)
    current_by_char, _ = load_charmap(current_charmap)
    cc = make_opencc(args.mode)

    preamble, blocks = parse_ime_blocks(ime_path)

    changed_rows: List[Tuple[str, str, str, str, str]] = []
    skipped_rows: List[Tuple[str, str, str, str, str]] = []
    rewritten_lines: List[str] = list(preamble)

    total_candidates = 0
    total_changed = 0

    for block in blocks:
        old_codes, saw_term = bytes_to_codes(block.bytes_)
        new_codes: List[int] = []
        display_chars: List[str] = []

        for code in old_codes:
            total_candidates += 1
            base_entry = base_by_code.get(code)
            if base_entry is None:
                new_codes.append(code)
                display_chars.append("?")
                skipped_rows.append((block.label, f"${code:04X}", "", "", "no_base_charmap_entry"))
                continue

            base_ch = base_entry.char
            trad = cc.convert(base_ch)

            if len(trad) != 1 or not is_cjk(trad):
                new_codes.append(code)
                display_chars.append(base_ch)
                skipped_rows.append((block.label, f"${code:04X}", base_ch, trad, "conversion_not_single_cjk"))
                continue

            target = current_by_char.get(trad)
            if target is None:
                new_codes.append(code)
                display_chars.append(base_ch)
                skipped_rows.append((block.label, f"${code:04X}", base_ch, trad, "traditional_char_not_in_current_charmap"))
                continue

            new_codes.append(target.code)
            display_chars.append(trad)

            if target.code != code:
                total_changed += 1
                changed_rows.append((block.label, f"${code:04X}", base_ch, trad, f"${target.code:04X}"))

        if not saw_term:
            skipped_rows.append((block.label, "", "", "", "block_has_no_terminator_50"))

        rewritten_lines.append(f"{block.label}:")
        rewritten_lines.extend(codes_to_db_lines(new_codes, args.per_line))
        rewritten_lines.append("\t ; " + " ".join(display_chars))
        rewritten_lines.append(f"\t ; {len(new_codes)}")

    rewritten = "\n".join(rewritten_lines) + "\n"
    patched_path = out_dir / "IMECharTable.asm"
    patched_path.write_text(rewritten, encoding="utf-8")

    with (out_dir / "changed.tsv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["label", "old_code", "old_char", "traditional_char", "new_code"])
        w.writerows(changed_rows)

    with (out_dir / "skipped.tsv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["label", "old_code", "old_char", "converted", "reason"])
        w.writerows(skipped_rows)

    summary_lines = [
        f"version={VERSION}",
        f"repo={repo}",
        f"base_charmap={base_charmap}",
        f"current_charmap={current_charmap}",
        f"ime={ime_path}",
        f"opencc_mode={args.mode}",
        f"blocks={len(blocks)}",
        f"candidates={total_candidates}",
        f"changed={total_changed}",
        f"skipped={len(skipped_rows)}",
        f"patched_file={patched_path}",
    ]
    (out_dir / "summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    print("IME codepoint patch")
    for line in summary_lines:
        print("  " + line)

    if args.apply:
        backup_dir = repo / "backups" / f"ime_codepoint_patch_{time.strftime('%Y%m%d_%H%M%S')}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = backup_dir / "IMECharTable.asm"
        shutil.copy2(ime_path, backup_file)
        shutil.copy2(patched_path, ime_path)
        print(f"applied; backup={backup_file}")
    else:
        print("dry-run/output only. Add --apply to replace src/dfs/IMECharTable.asm.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
