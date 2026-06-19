#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rebuild_full_cjk_charmap.py v2

Rebuild src/charmap.txt from a clean original CHS/base charmap plus Big5
compatibility/extension maps.

Fixes from v1:
  * Do not strip Unicode whitespace from charmap keys; preserves U+3000 IDEOGRAPHIC SPACE.
  * Preserve non-numeric control token mappings such as ć="<PLAYER>".
  * Do not write inline comments after entries, because the legacy import tools are simple.
  * Add safe OpenCC aliases from the original base charmap for non-Big5 Traditional chars
    such as 锃 -> 鋥, while skipping chars that already have explicit extension mappings.
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "full-cjk-charmap-2026-06-18-v2"
HEX_RE = re.compile(r"\$([0-9A-Fa-f]{1,4})")
ASCII_WS = " \t\r\n"


def fmt_code(n: int) -> str:
    return f"${n:04X}"


def parse_code(s: str) -> Optional[int]:
    s = (s or "").strip(ASCII_WS)
    if not s:
        return None
    m = HEX_RE.search(s)
    if m:
        return int(m.group(1), 16)
    if s.lower().startswith("0x"):
        try:
            return int(s, 16)
        except ValueError:
            return None
    try:
        return int(s, 0)
    except ValueError:
        return None


def is_cjkish(ch: str) -> bool:
    return any(
        "\u3400" <= c <= "\u4dbf" or
        "\u4e00" <= c <= "\u9fff" or
        "\uf900" <= c <= "\ufaff"
        for c in ch
    )


def repo_path(repo: Path, p: str | Path) -> Path:
    q = Path(p)
    return q if q.is_absolute() else repo / q


def read_tsv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_tsv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


@dataclass
class Row:
    char: str
    rhs: str
    code_int: Optional[int]
    source: str
    order: int
    note: str = ""

    @property
    def dfs_code(self) -> str:
        return fmt_code(self.code_int) if self.code_int is not None else self.rhs


def strip_ascii_outer(s: str) -> str:
    return s.strip(ASCII_WS)


def parse_charmap_lines(path: Path) -> Tuple[List[Row], Dict[str, Row], Dict[int, List[str]]]:
    rows: List[Row] = []
    char_to_row: Dict[str, Row] = {}
    code_to_chars: Dict[int, List[str]] = defaultdict(list)
    if not path.exists():
        raise FileNotFoundError(path)
    order = 0
    for line_no, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        # Important: do NOT raw.strip(); Python strips U+3000.
        line = raw.lstrip(" \t\r")
        if not line or line.startswith(";") or "=" not in line:
            continue
        left, right = line.split("=", 1)
        # Remove ASCII padding only. U+3000 is a valid key.
        ch = strip_ascii_outer(left)
        rhs = strip_ascii_outer(right.split(";", 1)[0])
        if not ch or not rhs:
            continue
        code = parse_code(rhs)
        row = Row(ch, rhs if code is None else fmt_code(code), code, "base-original", order, f"base line {line_no}")
        order += 1
        rows.append(row)
        char_to_row.setdefault(ch, row)
        if code is not None:
            code_to_chars[code].append(ch)
    return rows, char_to_row, code_to_chars


def add_mapping(
    rows: List[Row],
    char_to_row: Dict[str, Row],
    code_to_chars: Dict[int, List[str]],
    ch: str,
    code: int,
    source: str,
    note: str,
    conflicts: List[dict],
    order_base: int,
) -> bool:
    if not ch:
        return False
    old = char_to_row.get(ch)
    if old is not None:
        if old.code_int is not None and old.code_int != code:
            conflicts.append({
                "char": ch,
                "existing_code": fmt_code(old.code_int),
                "new_code": fmt_code(code),
                "existing_source": old.source,
                "new_source": source,
                "note": note,
            })
        return False
    row = Row(ch, fmt_code(code), code, source, order_base + len(rows), note)
    char_to_row[ch] = row
    code_to_chars[code].append(ch)
    rows.append(row)
    return True


def load_map(path: Path, default_class: str) -> List[Tuple[str, int, str, str]]:
    out: List[Tuple[str, int, str, str]] = []
    for r in read_tsv(path):
        ch = r.get("char") or r.get("ch") or ""
        code = parse_code(r.get("dfs_code", ""))
        if ch and code is not None:
            cls = r.get("compat_class") or r.get("slot_source") or default_class
            note = r.get("note", "")
            out.append((ch, code, cls, note))
    return out


def load_opencc(mode: str):
    if mode == "none":
        return None, "disabled"
    try:
        from opencc import OpenCC  # type: ignore
        return OpenCC(mode), "ok"
    except Exception as e:
        return None, f"unavailable: {e}"


def generate_base_opencc_aliases(
    base_rows: Sequence[Row],
    cc,
    extension_chars: set[str],
    existing_chars: set[str],
) -> Tuple[List[Tuple[str, int, str, str]], List[dict]]:
    aliases: List[Tuple[str, int, str, str]] = []
    skipped: List[dict] = []
    if cc is None:
        return aliases, skipped
    seen: set[Tuple[str, int]] = set()
    for row in base_rows:
        if row.code_int is None:
            continue
        src = row.char
        if len(src) != 1 or not is_cjkish(src):
            continue
        try:
            dst = cc.convert(src)
        except Exception as e:
            skipped.append({"base_char": src, "dfs_code": row.dfs_code, "converted": "", "reason": f"opencc error: {e}"})
            continue
        if not dst or dst == src or len(dst) != 1:
            continue
        key = (dst, row.code_int)
        if key in seen:
            continue
        seen.add(key)
        if dst in existing_chars:
            skipped.append({"base_char": src, "dfs_code": row.dfs_code, "converted": dst, "reason": "converted char already mapped"})
            continue
        if dst in extension_chars:
            # Do not steal one-simplified-to-many-traditional chars from the explicit Big5 extension map.
            skipped.append({"base_char": src, "dfs_code": row.dfs_code, "converted": dst, "reason": "converted char has explicit extension mapping"})
            continue
        aliases.append((dst, row.code_int, "base-opencc-alias", f"{src}->{dst}"))
    return aliases, skipped


def write_charmap(path: Path, rows: Sequence[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    lines.append("; Auto-generated full CJK DFS charmap.")
    lines.append(f"; Generated by rebuild_full_cjk_charmap.py {VERSION}.")
    lines.append("; Complete encoder table: original CHS/base + Traditional aliases + Big5 extension.")
    lines.append("; Do not put inline comments after mapping entries; legacy import tools are simple.")
    lines.append("")

    special = [r for r in rows if r.code_int is None]
    numeric = [r for r in rows if r.code_int is not None]
    # Preserve base order for original rows, then sort generated rows by source/code.
    base = [r for r in numeric if r.source == "base-original"]
    generated = [r for r in numeric if r.source != "base-original"]

    sections: List[Tuple[str, List[Row]]] = [
        ("base-special", special),
        ("base-original", base),
    ]
    for source in sorted({r.source for r in generated}):
        sections.append((source, sorted([r for r in generated if r.source == source], key=lambda r: (r.code_int or 0, r.char))))

    for title, rs in sections:
        if not rs:
            continue
        lines.append("")
        lines.append(f"; ---- {title} ----")
        if title == "base-original":
            rs = sorted(rs, key=lambda r: r.order)
        for r in rs:
            lines.append(f"{r.char}={r.rhs}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild a full CJK DFS charmap from base plus Big5 extension maps.")
    ap.add_argument("--version", action="store_true", help="Print version and exit")
    ap.add_argument("--repo", default=".", help="Repository root")
    ap.add_argument("--base-charmap", default="reports/charmap_chs_base.txt")
    ap.add_argument("--compat-map", default="src/dfs/maps/big5_compat_base_map.tsv")
    ap.add_argument("--extension-map", default="src/dfs/maps/big5_extension_physical_map.tsv")
    ap.add_argument("--out", default="reports/full_cjk_charmap/charmap.txt")
    ap.add_argument("--report-dir", default="reports/full_cjk_charmap")
    ap.add_argument("--opencc-mode", default="s2twp", help="OpenCC mode for safe base aliases; use 'none' to disable")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.version:
        print(VERSION)
        return 0

    repo = Path(args.repo).resolve()
    base_path = repo_path(repo, args.base_charmap)
    compat_path = repo_path(repo, args.compat_map)
    ext_path = repo_path(repo, args.extension_map)
    out_path = repo_path(repo, args.out)
    report_dir = repo_path(repo, args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    rows, char_to_row, code_to_chars = parse_charmap_lines(base_path)
    conflicts: List[dict] = []

    compat_mappings = load_map(compat_path, "base-compatible")
    ext_mappings = load_map(ext_path, "big5-extension")
    extension_chars = {ch for ch, _, _, _ in ext_mappings}

    compat_count = 0
    for ch, code, cls, note in compat_mappings:
        if add_mapping(rows, char_to_row, code_to_chars, ch, code, "base-compatible-alias", cls if not note else f"{cls}; {note}", conflicts, 100000):
            compat_count += 1

    cc, opencc_status = load_opencc(args.opencc_mode)
    opencc_aliases, opencc_skipped = generate_base_opencc_aliases(rows, cc, extension_chars, set(char_to_row.keys()))
    opencc_count = 0
    for ch, code, cls, note in opencc_aliases:
        if add_mapping(rows, char_to_row, code_to_chars, ch, code, "base-opencc-alias", note, conflicts, 200000):
            opencc_count += 1

    ext_count = 0
    for ch, code, cls, note in ext_mappings:
        if add_mapping(rows, char_to_row, code_to_chars, ch, code, "big5-extension", cls if not note else f"{cls}; {note}", conflicts, 300000):
            ext_count += 1

    write_charmap(out_path, rows)
    if args.apply:
        write_charmap(repo / "src/charmap.txt", rows)

    alias_rows = []
    for code, chars in sorted(code_to_chars.items()):
        if len(chars) > 1:
            alias_rows.append({"dfs_code": fmt_code(code), "alias_count": len(chars), "chars": "".join(chars)})
    write_tsv(report_dir / "code_aliases.tsv", ["dfs_code", "alias_count", "chars"], alias_rows)
    write_tsv(report_dir / "char_conflicts.tsv", ["char", "existing_code", "new_code", "existing_source", "new_source", "note"], conflicts)
    write_tsv(report_dir / "base_opencc_aliases.tsv", ["char", "dfs_code", "compat_class", "note"], [
        {"char": ch, "dfs_code": fmt_code(code), "compat_class": cls, "note": note} for ch, code, cls, note in opencc_aliases
    ])
    write_tsv(report_dir / "base_opencc_aliases_skipped.tsv", ["base_char", "dfs_code", "converted", "reason"], opencc_skipped)

    source_counts = Counter(r.source for r in rows)
    checks = []
    for ch in ["　", "ć", "č", "骉", "犇", "鋥", "ˉ", "隻", "髮", "麵", "寶", "怀", "懷"]:
        row = char_to_row.get(ch)
        checks.append({"char": ch, "mapped": "yes" if row else "no", "rhs": row.rhs if row else "", "source": row.source if row else ""})
    write_tsv(report_dir / "important_char_check.tsv", ["char", "mapped", "rhs", "source"], checks)

    lines: List[str] = []
    lines.append(f"rebuild_full_cjk_charmap {VERSION}")
    lines.append(f"base_charmap={args.base_charmap}")
    lines.append(f"compat_map={args.compat_map}")
    lines.append(f"extension_map={args.extension_map}")
    lines.append(f"opencc_mode={args.opencc_mode}")
    lines.append(f"opencc_status={opencc_status}")
    lines.append(f"out={out_path}")
    lines.append(f"applied_to_src_charmap={args.apply}")
    lines.append("")
    lines.append("counts:")
    lines.append(f"  total mappings: {len(rows)}")
    for k, v in sorted(source_counts.items()):
        lines.append(f"  {k}: {v}")
    lines.append(f"  base-compatible aliases newly added: {compat_count}")
    lines.append(f"  base-opencc aliases newly added: {opencc_count}")
    lines.append(f"  big5 extension mappings newly added: {ext_count}")
    lines.append(f"  codepoint aliases: {len(alias_rows)}")
    lines.append(f"  char conflicts skipped: {len(conflicts)}")
    lines.append("")
    lines.append("important char check:")
    for c in checks:
        lines.append(f"  {c['char']}: {c['mapped']} {c['rhs']} {c['source']}")
    lines.append("")
    lines.append("outputs:")
    for name in ["summary.txt", "code_aliases.tsv", "char_conflicts.tsv", "base_opencc_aliases.tsv", "base_opencc_aliases_skipped.tsv", "important_char_check.tsv"]:
        lines.append(f"  {report_dir / name}")
    lines.append(f"  {out_path}")
    if args.apply:
        lines.append("  src/charmap.txt")
    lines.append("")
    lines.append("notes:")
    lines.append("  U+3000 and control token mappings are preserved from the base charmap.")
    lines.append("  Non-Big5 Traditional aliases such as 鋥 can come from base OpenCC aliases.")
    lines.append("  If unwanted simplified source text should be rejected, use a separate text QA audit.")
    summary = "\n".join(lines) + "\n"
    (report_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
