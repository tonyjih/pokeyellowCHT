#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_big5_extension_feasibility.py

Audit how many Big5/CP950 characters can be represented by the existing
pokeyellowCHS DFS codepoints and how many would need an append-only DFS
extension area.

This script does not modify the repo. It only writes reports under:
  reports/big5_extension/

Typical usage:
  python3 src/tools/audit_big5_extension_feasibility.py \
    --repo . \
    --base-charmap reports/charmap_chs_base.txt \
    --current-charmap src/charmap.txt \
    --base-ime reports/IMECharTable_chs_base.asm

Optional Taiwan/Windows superset audit:
  python3 src/tools/audit_big5_extension_feasibility.py --repo . --encoding cp950
"""

from __future__ import annotations

import argparse
import codecs
import csv
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERSION = "big5-extension-audit-2026-06-18-v1"

CODE_RE = re.compile(r"\$([0-9A-Fa-f]{4})")
CHARMAP_LINE_RE = re.compile(r"^(.*?)\s*=\s*(.*)$")


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def parse_int_auto(s: str) -> int:
    s = s.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    if s.startswith("$"):
        return int(s[1:], 16)
    return int(s, 0)


def fmt_code(n: int) -> str:
    return f"${n:04X}"


def fmt_big5(bs: bytes) -> str:
    return "".join(f"{b:02X}" for b in bs)


def unicode_codepoint(ch: str) -> str:
    return " ".join(f"U+{ord(c):04X}" for c in ch)


def unicode_name(ch: str) -> str:
    try:
        return unicodedata.name(ch)
    except ValueError:
        return "<unassigned>"


def is_cjk_char(ch: str) -> bool:
    if len(ch) != 1:
        return False
    o = ord(ch)
    return (
        0x3400 <= o <= 0x4DBF or   # CJK Unified Ideographs Extension A
        0x4E00 <= o <= 0x9FFF or   # CJK Unified Ideographs
        0xF900 <= o <= 0xFAFF or   # CJK Compatibility Ideographs
        0x20000 <= o <= 0x2A6DF or
        0x2A700 <= o <= 0x2B73F or
        0x2B740 <= o <= 0x2B81F or
        0x2B820 <= o <= 0x2CEAF or
        0x2CEB0 <= o <= 0x2EBEF or
        0x30000 <= o <= 0x3134F
    )


def char_category(ch: str) -> str:
    if len(ch) != 1:
        return "multi"
    o = ord(ch)
    name = unicodedata.name(ch, "")
    cat = unicodedata.category(ch)
    if is_cjk_char(ch):
        return "cjk"
    if 0x3100 <= o <= 0x312F or 0x31A0 <= o <= 0x31BF:
        return "bopomofo"
    if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:
        return "kana"
    if 0xFF00 <= o <= 0xFFEF:
        return "fullwidth"
    if cat.startswith("P"):
        return "punctuation"
    if cat.startswith("S"):
        return "symbol"
    if "LATIN" in name:
        return "latin"
    if cat.startswith("N"):
        return "number"
    return cat or "other"


@dataclass(frozen=True)
class Big5Entry:
    ch: str
    primary_bytes: bytes
    aliases: Tuple[bytes, ...]


def enumerate_legacy_double_byte_encoding(
    encoding: str,
    lead_min: int,
    lead_max: int,
    trail_ranges: Sequence[Tuple[int, int]],
) -> List[Big5Entry]:
    """Enumerate decodable two-byte characters. Deduplicate by Unicode char."""
    char_to_bytes: Dict[str, List[bytes]] = defaultdict(list)
    for lead in range(lead_min, lead_max + 1):
        for start, end in trail_ranges:
            for trail in range(start, end + 1):
                bs = bytes([lead, trail])
                try:
                    text = bs.decode(encoding)
                except UnicodeDecodeError:
                    continue
                # Some encodings may decode to combining sequences. Keep them in the full
                # report, but most DFS glyph planning is for single visible codepoints.
                if not text:
                    continue
                char_to_bytes[text].append(bs)

    entries: List[Big5Entry] = []
    for ch, byte_list in char_to_bytes.items():
        byte_list_sorted = sorted(byte_list)
        entries.append(Big5Entry(ch=ch, primary_bytes=byte_list_sorted[0], aliases=tuple(byte_list_sorted[1:])))
    entries.sort(key=lambda e: e.primary_bytes)
    return entries


def parse_charmap(path: Path) -> Tuple[Dict[str, int], Dict[int, List[str]]]:
    char_to_code: Dict[str, int] = {}
    code_to_chars: Dict[int, List[str]] = defaultdict(list)
    if not path.exists():
        raise FileNotFoundError(path)
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";") or "=" not in line:
            continue
        m = CHARMAP_LINE_RE.match(line)
        if not m:
            continue
        left, right = m.group(1).strip(), m.group(2).strip()
        # Only parse numeric DFS codepoint entries like 字=$052E.
        if not right.startswith("$"):
            continue
        try:
            code = int(right[1:5], 16)
        except ValueError:
            continue
        if not left:
            continue
        # charmap control aliases may be multiple visible chars such as <PLAYER> entries;
        # numeric CJK entries are expected to be one displayed char. Keep the left side as-is
        # to preserve special symbols.
        char_to_code[left] = code
        code_to_chars[code].append(left)
    return char_to_code, code_to_chars


def parse_ime_codes(path: Optional[Path]) -> Tuple[Set[int], int]:
    """Parse DFS codepoints referenced by IMECharTable.asm.

    The original table stores candidates as raw byte pairs, for example:
      db $04,$C5,$04,$CA,$50
    where $50 is the text terminator. Some generated variants may instead
    contain $04C5 style constants, so support both forms.
    """
    if path is None:
        return set(), 0
    if not path.exists():
        raise FileNotFoundError(path)

    codes: List[int] = []
    text = path.read_text(encoding="utf-8", errors="replace")

    # First parse normal two-byte db lines.
    for raw_line in text.splitlines():
        line = raw_line.split(";", 1)[0]
        if "db" not in line:
            continue
        # If this line uses $04C5-style constants, handle those separately below.
        if re.search(r"\$[0-9A-Fa-f]{4}", line):
            continue
        vals = [int(x, 16) for x in re.findall(r"\$([0-9A-Fa-f]{2})", line)]
        i = 0
        while i < len(vals):
            if vals[i] == 0x50:
                i += 1
                continue
            if i + 1 >= len(vals):
                break
            hi, lo = vals[i], vals[i + 1]
            # Candidate codepoints in this DFS are two bytes. The IME table
            # terminator is a single $50 byte, not a high-byte candidate.
            codes.append((hi << 8) | lo)
            i += 2

    # Also support generated tables that may spell candidates as $04C5.
    for m in CODE_RE.finditer(text):
        codes.append(int(m.group(1), 16))

    return set(codes), len(codes)


class Converter:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.available = False
        self.reason = ""
        self._converter = None
        if mode == "none":
            self.available = True
            return
        try:
            from opencc import OpenCC  # type: ignore
            self._converter = OpenCC(mode)
            self.available = True
        except Exception as exc:  # pragma: no cover - environment-dependent
            self.reason = f"OpenCC unavailable for mode={mode}: {exc}"
            self.available = False

    def convert(self, s: str) -> str:
        if self.mode == "none" or not self._converter:
            return s
        return self._converter.convert(s)


@dataclass
class Classification:
    ch: str
    big5: str
    aliases: str
    unicode: str
    name: str
    category: str
    t2s: str
    class_name: str
    dfs_code: str
    base_char_at_code: str
    current_char_at_code: str
    ime_reserved: str
    extension_needed: str
    note: str


def classify_entries(
    entries: Sequence[Big5Entry],
    base_char_to_code: Dict[str, int],
    base_code_to_chars: Dict[int, List[str]],
    current_code_to_chars: Dict[int, List[str]],
    ime_codes: Set[int],
    converter: Converter,
) -> List[Classification]:
    rows: List[Classification] = []
    for ent in entries:
        ch = ent.ch
        category = char_category(ch)
        aliases = ",".join(fmt_big5(b) for b in ent.aliases)
        t2s = converter.convert(ch) if converter.available else ch
        code: Optional[int] = None
        class_name = "extension-needed"
        note = ""

        if ch in base_char_to_code:
            code = base_char_to_code[ch]
            class_name = "base-exact"
        elif len(ch) == 1 and converter.available and len(t2s) == 1 and t2s in base_char_to_code:
            code = base_char_to_code[t2s]
            class_name = "base-t2s-compatible"
            note = f"glyph patch {t2s}->{ch}"
        elif converter.available and t2s != ch and (len(t2s) != 1):
            class_name = "extension-needed-multi-t2s"
            note = f"t2s produced {repr(t2s)}"
        elif not converter.available and converter.mode != "none":
            class_name = "extension-needed-no-opencc"
            note = converter.reason

        if code is not None:
            base_chars = "".join(base_code_to_chars.get(code, []))
            current_chars = "".join(current_code_to_chars.get(code, []))
            ime = "yes" if code in ime_codes else "no"
            dfs_code = fmt_code(code)
            ext = "no"
            if class_name == "base-t2s-compatible" and ime == "yes":
                class_name = "base-t2s-ime-glyph-patch"
            elif class_name == "base-t2s-compatible":
                class_name = "base-t2s-glyph-patch"
        else:
            base_chars = ""
            current_chars = ""
            ime = "no"
            dfs_code = ""
            ext = "yes"

        rows.append(Classification(
            ch=ch,
            big5=fmt_big5(ent.primary_bytes),
            aliases=aliases,
            unicode=unicode_codepoint(ch),
            name=unicode_name(ch) if len(ch) == 1 else "<sequence>",
            category=category,
            t2s=t2s,
            class_name=class_name,
            dfs_code=dfs_code,
            base_char_at_code=base_chars,
            current_char_at_code=current_chars,
            ime_reserved=ime,
            extension_needed=ext,
            note=note,
        ))
    return rows


def write_tsv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def row_dataclass_to_dict(obj: object) -> dict:
    return {k: getattr(obj, k) for k in obj.__dataclass_fields__.keys()}  # type: ignore[attr-defined]


def build_extension_rows(
    classes: Sequence[Classification],
    include_categories: Set[str],
    page_size: int,
    start_index: int = 0,
) -> List[dict]:
    rows: List[dict] = []
    idx = start_index
    for r in classes:
        if r.extension_needed != "yes":
            continue
        if include_categories and r.category not in include_categories:
            continue
        page = idx // page_size
        slot = idx % page_size
        rows.append({
            "ext_index": idx,
            "ext_page": page,
            "ext_slot": slot,
            "logical_ext_code": f"EXT+{idx:04X}",
            "char": r.ch,
            "unicode": r.unicode,
            "big5": r.big5,
            "category": r.category,
            "t2s": r.t2s,
            "note": "logical extension index only; physical DFS codepoint is TBD after dfs.asm audit",
        })
        idx += 1
    return rows


def current_collision_rows(
    base_code_to_chars: Dict[int, List[str]],
    current_code_to_chars: Dict[int, List[str]],
    ime_codes: Set[int],
) -> List[dict]:
    rows: List[dict] = []
    for code in sorted(set(base_code_to_chars) | set(current_code_to_chars)):
        base = "".join(base_code_to_chars.get(code, []))
        cur = "".join(current_code_to_chars.get(code, []))
        if not base or not cur or base == cur:
            continue
        base_cat = char_category(base) if len(base) == 1 else "multi"
        cur_cat = char_category(cur) if len(cur) == 1 else "multi"
        if code in ime_codes:
            severity = "review-ime-codepoint"
        elif base_cat != "cjk" and cur_cat == "cjk":
            severity = "danger-symbol-to-cjk"
        elif base_cat == "cjk" and cur_cat == "cjk":
            severity = "cjk-glyph-patch-or-reuse"
        else:
            severity = "review"
        rows.append({
            "code": fmt_code(code),
            "base_char": base,
            "base_category": base_cat,
            "current_char": cur,
            "current_category": cur_cat,
            "ime_reserved": "yes" if code in ime_codes else "no",
            "severity": severity,
        })
    return rows


def write_summary(
    path: Path,
    args: argparse.Namespace,
    entries: Sequence[Big5Entry],
    classes: Sequence[Classification],
    extension_rows_all: Sequence[dict],
    extension_rows_cjk: Sequence[dict],
    ime_unique_count: int,
    ime_ref_count: int,
    converter: Converter,
) -> None:
    category_counts = Counter(r.category for r in classes)
    class_counts = Counter(r.class_name for r in classes)
    ext_category_counts = Counter(r.category for r in classes if r.extension_needed == "yes")
    cjk_count = sum(1 for r in classes if r.category == "cjk")
    cjk_ext_count = sum(1 for r in classes if r.category == "cjk" and r.extension_needed == "yes")
    ext_all_count = len(extension_rows_all)
    ext_cjk_count = len(extension_rows_cjk)
    glyph_bytes = args.glyph_bytes
    bank_size = args.bank_size

    lines: List[str] = []
    lines.append(f"audit_big5_extension_feasibility {VERSION}")
    lines.append(f"encoding={args.encoding}")
    lines.append(f"lead_range=${args.lead_min:02X}-${args.lead_max:02X}")
    lines.append(f"base_charmap={args.base_charmap}")
    lines.append(f"current_charmap={args.current_charmap or ''}")
    lines.append(f"base_ime={args.base_ime or ''}")
    lines.append(f"opencc_mode={args.opencc_mode}")
    lines.append(f"opencc_available={converter.available}")
    if converter.reason:
        lines.append(f"opencc_reason={converter.reason}")
    lines.append("")
    lines.append("counts:")
    lines.append(f"  unique decoded chars: {len(entries)}")
    lines.append(f"  unique CJK chars: {cjk_count}")
    lines.append(f"  extension-needed chars, all categories: {sum(1 for r in classes if r.extension_needed == 'yes')}")
    lines.append(f"  extension-needed CJK chars: {cjk_ext_count}")
    lines.append(f"  IME unique reserved codepoints: {ime_unique_count}")
    lines.append(f"  IME candidate refs: {ime_ref_count}")
    lines.append("")
    lines.append("classification counts:")
    for key, value in sorted(class_counts.items()):
        lines.append(f"  {key}: {value}")
    lines.append("")
    lines.append("category counts:")
    for key, value in sorted(category_counts.items()):
        lines.append(f"  {key}: {value}")
    lines.append("")
    lines.append("extension-needed by category:")
    for key, value in sorted(ext_category_counts.items()):
        lines.append(f"  {key}: {value}")
    lines.append("")
    lines.append("storage estimates:")
    for label, count in [("all extension-needed", ext_all_count), ("CJK extension-needed", ext_cjk_count)]:
        byte_count = count * glyph_bytes
        lines.append(f"  {label}: {count} glyphs")
        lines.append(f"    glyph bytes (@{glyph_bytes}/glyph): {byte_count}")
        lines.append(f"    approx KiB: {byte_count / 1024:.2f}")
        lines.append(f"    approx {bank_size}-byte ROM banks: {math.ceil(byte_count / bank_size) if byte_count else 0}")
    lines.append("")
    lines.append("outputs:")
    lines.append("  big5_chars.tsv")
    lines.append("  classification.tsv")
    lines.append("  proposed_extension_map.tsv")
    lines.append("  proposed_extension_map_cjk.tsv")
    lines.append("  current_codepoint_collision_audit.tsv")
    lines.append("")
    lines.append("notes:")
    lines.append("  proposed_extension_map.tsv uses logical EXT+xxxx indices only.")
    lines.append("  Physical DFS codepoints must be assigned after dfs.asm/page-table audit.")
    lines.append("  base-t2s-* rows are compatibility candidates: keep original DFS byte and redraw glyph.")
    lines.append("  extension-needed rows are Big5 chars that require append-only extension slots.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def default_path(repo: Path, rel: str) -> Path:
    return repo / rel


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Audit Big5 feasibility for pokeyellowCHS/CHT DFS extension.")
    ap.add_argument("--version", action="store_true", help="Print version and exit.")
    ap.add_argument("--repo", default=".", help="Repository root. Default: current directory.")
    ap.add_argument("--base-charmap", default=None, help="Original CHS charmap.txt. Default: reports/charmap_chs_base.txt then src/charmap.txt.")
    ap.add_argument("--current-charmap", default=None, help="Current CHT charmap.txt. Default: src/charmap.txt if present.")
    ap.add_argument("--base-ime", default=None, help="Original CHS IMECharTable.asm for reserved-code reporting.")
    ap.add_argument("--out-dir", default="reports/big5_extension", help="Output directory relative to repo unless absolute.")
    ap.add_argument("--encoding", default="big5", choices=["big5", "cp950", "big5hkscs"], help="Encoding to enumerate. Default: big5.")
    ap.add_argument("--lead-min", default=None, help="Lead byte min, e.g. 0xA1. Default depends on encoding.")
    ap.add_argument("--lead-max", default=None, help="Lead byte max, e.g. 0xF9. Default depends on encoding.")
    ap.add_argument("--opencc-mode", default="t2s", help="OpenCC mode for Traditional -> Simplified compatibility lookup. Use none to disable.")
    ap.add_argument("--page-size", type=int, default=256, help="Logical extension page size for report grouping. Default: 256.")
    ap.add_argument("--glyph-bytes", type=int, default=18, help="Bytes per 12x12 DFS glyph. Default: 18.")
    ap.add_argument("--bank-size", type=int, default=0x4000, help="ROM bank size for estimate. Default: 16384.")
    ap.add_argument("--include-extension-categories", default="cjk,bopomofo,kana,fullwidth,punctuation,symbol,latin,number,other", help="Comma-separated categories for proposed_extension_map.tsv. Default includes common visible categories.")
    args = ap.parse_args(argv)

    if args.version:
        print(VERSION)
        return 0

    repo = Path(args.repo).resolve()
    if args.base_charmap:
        base_charmap = Path(args.base_charmap)
        if not base_charmap.is_absolute():
            base_charmap = repo / base_charmap
    else:
        candidate = repo / "reports/charmap_chs_base.txt"
        base_charmap = candidate if candidate.exists() else repo / "src/charmap.txt"

    if args.current_charmap:
        current_charmap = Path(args.current_charmap)
        if not current_charmap.is_absolute():
            current_charmap = repo / current_charmap
    else:
        current_charmap = repo / "src/charmap.txt"
        if not current_charmap.exists():
            current_charmap = base_charmap

    base_ime: Optional[Path]
    if args.base_ime:
        base_ime = Path(args.base_ime)
        if not base_ime.is_absolute():
            base_ime = repo / base_ime
    else:
        candidate = repo / "reports/IMECharTable_chs_base.asm"
        base_ime = candidate if candidate.exists() else None

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = repo / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.lead_min is None or args.lead_max is None:
        if args.encoding == "big5":
            lead_min = 0xA1
            lead_max = 0xF9
        else:
            lead_min = 0x81
            lead_max = 0xFE
    else:
        lead_min = parse_int_auto(args.lead_min)
        lead_max = parse_int_auto(args.lead_max)
    args.lead_min = lead_min
    args.lead_max = lead_max

    print(f"enumerating {args.encoding} two-byte chars...")
    entries = enumerate_legacy_double_byte_encoding(
        encoding=args.encoding,
        lead_min=lead_min,
        lead_max=lead_max,
        trail_ranges=[(0x40, 0x7E), (0xA1, 0xFE)],
    )
    print(f"  decoded unique chars: {len(entries)}")

    print(f"loading base charmap: {base_charmap}")
    base_char_to_code, base_code_to_chars = parse_charmap(base_charmap)
    print(f"  base charmap chars: {len(base_char_to_code)}")

    print(f"loading current charmap: {current_charmap}")
    current_char_to_code, current_code_to_chars = parse_charmap(current_charmap)
    print(f"  current charmap chars: {len(current_char_to_code)}")

    if base_ime:
        print(f"loading base IME reserve codes: {base_ime}")
    ime_codes, ime_ref_count = parse_ime_codes(base_ime)
    print(f"  IME unique reserved codepoints: {len(ime_codes)}")
    print(f"  IME candidate refs: {ime_ref_count}")

    converter = Converter(args.opencc_mode)
    print(f"OpenCC mode={args.opencc_mode} available={converter.available}")
    if converter.reason:
        print(f"  {converter.reason}")

    print("classifying...")
    classes = classify_entries(entries, base_char_to_code, base_code_to_chars, current_code_to_chars, ime_codes, converter)

    include_categories = {x.strip() for x in args.include_extension_categories.split(",") if x.strip()}
    extension_rows_all = build_extension_rows(classes, include_categories=include_categories, page_size=args.page_size)
    extension_rows_cjk = build_extension_rows(classes, include_categories={"cjk"}, page_size=args.page_size)
    collisions = current_collision_rows(base_code_to_chars, current_code_to_chars, ime_codes)

    print(f"writing reports to: {out_dir}")
    write_tsv(
        out_dir / "big5_chars.tsv",
        ["char", "unicode", "big5", "aliases", "category", "name"],
        ({
            "char": e.ch,
            "unicode": unicode_codepoint(e.ch),
            "big5": fmt_big5(e.primary_bytes),
            "aliases": ",".join(fmt_big5(b) for b in e.aliases),
            "category": char_category(e.ch),
            "name": unicode_name(e.ch) if len(e.ch) == 1 else "<sequence>",
        } for e in entries),
    )
    write_tsv(
        out_dir / "classification.tsv",
        list(row_dataclass_to_dict(classes[0]).keys()) if classes else [],
        (row_dataclass_to_dict(r) for r in classes),
    )
    write_tsv(
        out_dir / "proposed_extension_map.tsv",
        ["ext_index", "ext_page", "ext_slot", "logical_ext_code", "char", "unicode", "big5", "category", "t2s", "note"],
        extension_rows_all,
    )
    write_tsv(
        out_dir / "proposed_extension_map_cjk.tsv",
        ["ext_index", "ext_page", "ext_slot", "logical_ext_code", "char", "unicode", "big5", "category", "t2s", "note"],
        extension_rows_cjk,
    )
    write_tsv(
        out_dir / "current_codepoint_collision_audit.tsv",
        ["code", "base_char", "base_category", "current_char", "current_category", "ime_reserved", "severity"],
        collisions,
    )
    write_summary(
        out_dir / "summary.txt",
        args,
        entries,
        classes,
        extension_rows_all,
        extension_rows_cjk,
        len(ime_codes),
        ime_ref_count,
        converter,
    )

    print("done.")
    print(f"summary: {out_dir / 'summary.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
