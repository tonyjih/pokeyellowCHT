#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_dfs_physical_pages.py

Audit the physical DFS page/high-byte capacity of the pokeyellowCHS/CHT DFS font
system before assigning Big5 extension codepoints.

This script does not modify the repo. It produces reports under reports/dfs_pages/
by default.

It tries to answer:
  * Which DFS lead-byte pages are already enabled in FontAB?
  * Which pages are present but disabled as FF in FontAB?
  * What lead-byte upper bound does _dfsUnion currently enforce?
  * How many extension glyph slots can we probably get by enabling dormant pages?
  * How many additional pages are addressable if the lead-byte limit is raised to $40?

The capacity estimate is conservative: by default it uses the largest existing
legacy page occupancy from the base charmap as the per-page slot template. For
this project that is usually 226 glyphs/page.
"""
from __future__ import annotations

import argparse
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERSION = "dfs-page-audit-2026-06-18-v1"

HEX_RE = re.compile(r"\$([0-9A-Fa-f]{1,4})")


@dataclass
class CharmapEntry:
    char: str
    code: int
    line_no: int


@dataclass
class FontABInfo:
    args: List[str]
    start_offset: int
    end_offset: int
    raw_snippet: str


def repo_path(repo: Path, rel_or_abs: str | Path) -> Path:
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    return repo / p


def load_charmap(path: Path) -> Tuple[List[CharmapEntry], Dict[str, int], Dict[int, List[str]]]:
    entries: List[CharmapEntry] = []
    char_to_code: Dict[str, int] = {}
    code_to_chars: Dict[int, List[str]] = defaultdict(list)
    if not path.exists():
        return entries, char_to_code, code_to_chars
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith(";") or "=" not in s:
            continue
        ch, code_s = s.split("=", 1)
        ch = ch.strip()
        code_s = code_s.strip()
        m = HEX_RE.fullmatch(code_s)
        if not m:
            continue
        code = int(m.group(1), 16)
        e = CharmapEntry(ch, code, i)
        entries.append(e)
        char_to_code[ch] = code
        code_to_chars[code].append(ch)
    return entries, char_to_code, code_to_chars


def page_low_counts(entries: Sequence[CharmapEntry]) -> Dict[int, Counter]:
    out: Dict[int, Counter] = defaultdict(Counter)
    for e in entries:
        if e.code < 0 or e.code > 0xFFFF:
            continue
        out[e.code >> 8][e.code & 0xFF] += 1
    return out


def strip_asm_comments(text: str) -> str:
    # Preserve newlines. Remove RGBASM ; comments conservatively.
    out_lines = []
    for line in text.splitlines():
        if ";" in line:
            line = line.split(";", 1)[0]
        out_lines.append(line)
    return "\n".join(out_lines)


def parse_fontab(text: str) -> Optional[FontABInfo]:
    idx = text.find("FontAB:")
    if idx < 0:
        return None
    tail = text[idx:]

    # Most versions have Send4RawFontTo8FontLeft after FontAB. Use it as a clean stop.
    end_rel_candidates = []
    for marker in ["Send4RawFontTo8FontLeft", "Send4RawFontTo8FontRight", "GetVramAddr"]:
        j = tail.find(marker)
        if j > 0:
            end_rel_candidates.append(j)
    end_rel = min(end_rel_candidates) if end_rel_candidates else min(len(tail), 4000)
    snippet = tail[:end_rel]
    snippet_nc = strip_asm_comments(snippet)

    m = re.search(r"\bfontab\b(?P<body>.*)", snippet_nc, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    body = m.group("body")
    # Extract tokens like FF, 01, 0A, $0A from the macro invocation body.
    # Stop words from labels/instructions will be ignored.
    tokens: List[str] = []
    for tok in re.split(r"[\s,]+", body):
        tok = tok.strip()
        if not tok:
            continue
        tok_u = tok.upper().removeprefix("$")
        if re.fullmatch(r"[0-9A-F]{2}", tok_u):
            tokens.append(tok_u)
    return FontABInfo(args=tokens, start_offset=idx, end_offset=idx + end_rel, raw_snippet=snippet)


def parse_lead_limit(text: str) -> Tuple[Optional[int], List[str]]:
    lines = text.splitlines()
    hits: List[str] = []
    values: List[int] = []
    for i, line in enumerate(lines, 1):
        if "StaticSingleCode" not in line:
            continue
        window = "\n".join(lines[max(0, i - 4): min(len(lines), i + 2)])
        for m in re.finditer(r"cp\s+a\s*,\s*\$([0-9A-Fa-f]{2})", window, flags=re.IGNORECASE):
            v = int(m.group(1), 16)
            # The lead-byte DFS cutoff is normally $2F; ignore the $EC direct-char cutoff.
            if v < 0x80:
                values.append(v)
                hits.append(f"around line {i}: cp a, ${v:02X} -> StaticSingleCode")
    if values:
        # Use the smallest sub-$80 static cutoff; usually 0x2F.
        return min(values), hits
    # Fallback: direct search for known pattern.
    m = re.search(r"cp\s+a\s*,\s*\$([0-9A-Fa-f]{2}).{0,80}?StaticSingleCode", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        v = int(m.group(1), 16)
        if v < 0x80:
            return v, [f"regex fallback: cp a, ${v:02X} -> StaticSingleCode"]
    return None, []


def parse_page_mask(text: str) -> Tuple[Optional[int], List[str]]:
    lines = text.splitlines()
    hits: List[str] = []
    values: List[int] = []
    for i, line in enumerate(lines, 1):
        m = re.search(r"and\s+a\s*,\s*\$([0-9A-Fa-f]{2})", line, flags=re.IGNORECASE)
        if not m:
            continue
        v = int(m.group(1), 16)
        if v in (0x1F, 0x3F, 0x7F):
            window = "\n".join(lines[max(0, i - 6): min(len(lines), i + 8)])
            if "FontAB" in window or "Send4RawFontToSRAM" in window or "sla c" in window:
                values.append(v)
                hits.append(f"line {i}: and a, ${v:02X}")
    if values:
        # The DFS page mask should be the largest common hit, normally $3F.
        return max(values), hits
    m = re.search(r"and\s+a\s*,\s*\$([0-9A-Fa-f]{2}).{0,200}?FontAB", text, flags=re.IGNORECASE | re.DOTALL)
    if m:
        v = int(m.group(1), 16)
        return v, [f"regex fallback: and a, ${v:02X}"]
    return None, []


def parse_big5_summary(path: Path) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "extension_needed_all": r"extension-needed chars, all categories:\s*(\d+)",
        "extension_needed_cjk": r"extension-needed CJK chars:\s*(\d+)",
        "storage_glyphs_all": r"all extension-needed:\s*(\d+) glyphs",
        "storage_glyphs_cjk": r"CJK extension-needed:\s*(\d+) glyphs",
    }
    for k, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            out[k] = int(m.group(1))
    return out


def fmt_page_set(pages: Iterable[int]) -> str:
    vals = sorted(set(pages))
    if not vals:
        return "(none)"
    ranges: List[str] = []
    start = prev = vals[0]
    for v in vals[1:]:
        if v == prev + 1:
            prev = v
            continue
        ranges.append(f"${start:02X}" if start == prev else f"${start:02X}-${prev:02X}")
        start = prev = v
    ranges.append(f"${start:02X}" if start == prev else f"${start:02X}-${prev:02X}")
    return ", ".join(ranges)


def low_template_stats(base_page_lows: Dict[int, Counter], active_pages: Set[int]) -> Dict[str, object]:
    counts = []
    candidate_sets: Dict[int, Set[int]] = {}
    for hi, lows in base_page_lows.items():
        if hi in active_pages and len(lows) >= 100:
            counts.append(len(lows))
            candidate_sets[hi] = set(lows)
    if not counts:
        return {
            "capacity_per_page_conservative": 0,
            "capacity_per_page_max_existing": 0,
            "capacity_per_page_median": 0,
            "template_page": None,
            "template_lows": set(),
            "page_counts": counts,
        }
    max_count = max(counts)
    template_page = sorted([hi for hi, lows in candidate_sets.items() if len(lows) == max_count])[0]
    return {
        "capacity_per_page_conservative": max_count,
        "capacity_per_page_max_existing": max_count,
        "capacity_per_page_median": int(statistics.median(counts)),
        "template_page": template_page,
        "template_lows": candidate_sets[template_page],
        "page_counts": counts,
    }


def write_tsv(path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("\t".join(header) + "\n")
        for row in rows:
            f.write("\t".join(str(x) for x in row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit DFS physical pages/high-byte capacity for CJK/Big5 extension.")
    ap.add_argument("--repo", default=".", help="Repository root")
    ap.add_argument("--dfs", default="src/dfs/dfs.asm", help="Path to dfs.asm relative to repo")
    ap.add_argument("--base-charmap", default="reports/charmap_chs_base.txt", help="Original CHS/base charmap")
    ap.add_argument("--current-charmap", default="src/charmap.txt", help="Current charmap")
    ap.add_argument("--big5-summary", default="reports/big5_extension/summary.txt", help="Big5 audit summary.txt")
    ap.add_argument("--out-dir", default="reports/dfs_pages", help="Output directory")
    ap.add_argument("--needed-all", type=int, default=None, help="Override all-category extension glyph count")
    ap.add_argument("--needed-cjk", type=int, default=None, help="Override CJK-only extension glyph count")
    ap.add_argument("--assume-capacity-per-page", type=int, default=None, help="Override slots/page estimate")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    dfs_path = repo_path(repo, args.dfs)
    base_charmap_path = repo_path(repo, args.base_charmap)
    current_charmap_path = repo_path(repo, args.current_charmap)
    big5_summary_path = repo_path(repo, args.big5_summary)
    out_dir = repo_path(repo, args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not dfs_path.exists():
        raise SystemExit(f"dfs.asm not found: {dfs_path}")
    if not base_charmap_path.exists():
        raise SystemExit(f"base charmap not found: {base_charmap_path}")

    dfs_text = dfs_path.read_text(encoding="utf-8", errors="replace")
    base_entries, base_char_to_code, base_code_to_chars = load_charmap(base_charmap_path)
    current_entries, current_char_to_code, current_code_to_chars = load_charmap(current_charmap_path)
    base_lows = page_low_counts(base_entries)
    current_lows = page_low_counts(current_entries)

    fontab = parse_fontab(dfs_text)
    if fontab is None:
        raise SystemExit("Could not parse FontAB macro invocation in dfs.asm")

    lead_limit, lead_hits = parse_lead_limit(dfs_text)
    page_mask, mask_hits = parse_page_mask(dfs_text)

    fontab_args = fontab.args
    table_pages = list(range(len(fontab_args)))
    active_pages: Set[int] = {i for i, arg in enumerate(fontab_args) if arg.upper() != "FF"}
    disabled_pages: Set[int] = {i for i, arg in enumerate(fontab_args) if arg.upper() == "FF"}
    used_base_pages = set(base_lows.keys())
    used_current_pages = set(current_lows.keys())

    if lead_limit is None:
        # Conservative fallback from known CHS DFS.
        lead_limit = 0x2F
    current_dfs_lead_pages = set(range(1, lead_limit))  # $01..limit-1
    disabled_under_current_limit = sorted(p for p in disabled_pages if p in current_dfs_lead_pages)

    # If page_mask is $3F, the physical page index can address $00-$3F internally.
    # Base text lead bytes still need _dfsUnion threshold changed to accept them.
    theoretical_max_page = page_mask if page_mask is not None else 0x3F
    addressable_pages_if_raised_to_40 = set(range(1, min(theoretical_max_page, 0x3F) + 1))
    currently_not_accepted_but_addressable = sorted(
        p for p in addressable_pages_if_raised_to_40
        if p >= lead_limit
    )
    fontab_needs_extension_pages = sorted(p for p in addressable_pages_if_raised_to_40 if p >= len(fontab_args))

    stats = low_template_stats(base_lows, active_pages)
    capacity_per_page = args.assume_capacity_per_page or int(stats["capacity_per_page_conservative"] or 226)

    dormant_current_capacity = len(disabled_under_current_limit) * capacity_per_page
    raise_to_40_existing_table_pages = [p for p in currently_not_accepted_but_addressable if p < len(fontab_args)]
    raise_to_40_new_table_pages = [p for p in currently_not_accepted_but_addressable if p >= len(fontab_args)]
    total_pages_if_raise_to_40 = len(disabled_under_current_limit) + len(raise_to_40_existing_table_pages) + len(raise_to_40_new_table_pages)
    total_capacity_if_raise_to_40 = total_pages_if_raise_to_40 * capacity_per_page

    big5 = parse_big5_summary(big5_summary_path)
    needed_all = args.needed_all if args.needed_all is not None else big5.get("storage_glyphs_all") or big5.get("extension_needed_all")
    needed_cjk = args.needed_cjk if args.needed_cjk is not None else big5.get("storage_glyphs_cjk") or big5.get("extension_needed_cjk")

    # existing_pages.tsv
    rows = []
    max_page = max([len(fontab_args) - 1, *(base_lows.keys() or [0]), *(current_lows.keys() or [0]), theoretical_max_page])
    for p in range(0, max_page + 1):
        arg = fontab_args[p] if p < len(fontab_args) else "(no FontAB entry)"
        if p in active_pages:
            status = "enabled"
        elif p < len(fontab_args):
            status = "fontab-ff-disabled"
        else:
            status = "missing-fontab-entry"
        accepted_now = 1 <= p < lead_limit
        addressable_by_mask = p <= theoretical_max_page
        b_lows = sorted(base_lows.get(p, {}).keys())
        c_lows = sorted(current_lows.get(p, {}).keys())
        rows.append([
            f"${p:02X}",
            arg,
            status,
            "yes" if accepted_now else "no",
            "yes" if addressable_by_mask else "no",
            len(b_lows),
            f"${min(b_lows):02X}" if b_lows else "",
            f"${max(b_lows):02X}" if b_lows else "",
            len(c_lows),
            f"${min(c_lows):02X}" if c_lows else "",
            f"${max(c_lows):02X}" if c_lows else "",
        ])
    write_tsv(
        out_dir / "existing_pages.tsv",
        ["page", "fontab_arg", "status", "accepted_as_text_lead_now", "addressable_by_page_mask", "base_used_lows", "base_low_min", "base_low_max", "current_used_lows", "current_low_min", "current_low_max"],
        rows,
    )

    # Proposed pages.
    prop_rows = []
    for p in disabled_under_current_limit:
        prop_rows.append([f"${p:02X}", "enable-existing-fontab-slot", "fill FF with page label and add font bin", capacity_per_page])
    for p in raise_to_40_existing_table_pages:
        prop_rows.append([f"${p:02X}", "raise-lead-limit-and-enable-fontab-slot", "change _dfsUnion cutoff to $40; fill FF with page label and add font bin", capacity_per_page])
    for p in raise_to_40_new_table_pages:
        prop_rows.append([f"${p:02X}", "raise-lead-limit-and-extend-fontab", "change _dfsUnion cutoff to $40; extend FontAB to 64 entries; add font bin", capacity_per_page])
    write_tsv(
        out_dir / "proposed_extension_pages.tsv",
        ["page", "proposal", "required_change", "estimated_slots"],
        prop_rows,
    )

    # Low template.
    template_lows = sorted(stats.get("template_lows") or [])
    write_tsv(out_dir / "safe_low_template.tsv", ["low_byte"], [[f"${x:02X}"] for x in template_lows])

    # Patch notes.
    patch_notes = []
    patch_notes.append("# DFS page audit patch notes\n")
    patch_notes.append("This file is generated by `audit_dfs_physical_pages.py`. It is advisory only.\n")
    patch_notes.append("## Relevant code observations\n")
    patch_notes.append(f"- Current DFS text lead cutoff: `${lead_limit:02X}`; lead bytes below this value are candidates for double/quad DFS decoding.\n")
    if lead_hits:
        patch_notes.append("- Cutoff parse hits:\n")
        for h in lead_hits:
            patch_notes.append(f"  - {h}\n")
    if page_mask is not None:
        patch_notes.append(f"- Font page mask appears to be `${page_mask:02X}`, so the font fetch path is likely 6-bit page-index based.\n")
    if mask_hits:
        patch_notes.append("- Page-mask parse hits:\n")
        for h in mask_hits:
            patch_notes.append(f"  - {h}\n")
    patch_notes.append("\n## Suggested order\n")
    patch_notes.append("1. Enable dormant pages below the current cutoff first.\n")
    patch_notes.append("2. If not enough, raise the DFS lead cutoff from `$2F` to `$40` and extend/fill FontAB up to page `$3F`.\n")
    patch_notes.append("3. Keep page `$00` disabled unless the text parser is audited for NUL/control conflicts.\n")
    patch_notes.append("4. Do not use lead bytes `$40-$7F` without deeper changes: bit 6 is used internally when fetching the paired 4x12 glyph strip.\n")
    (out_dir / "patch_notes.md").write_text("".join(patch_notes), encoding="utf-8")

    summary_lines: List[str] = []
    summary_lines.append(f"audit_dfs_physical_pages {VERSION}\n")
    summary_lines.append(f"repo={repo}\n")
    summary_lines.append(f"dfs={dfs_path.relative_to(repo) if dfs_path.is_relative_to(repo) else dfs_path}\n")
    summary_lines.append(f"base_charmap={base_charmap_path.relative_to(repo) if base_charmap_path.is_relative_to(repo) else base_charmap_path}\n")
    summary_lines.append(f"current_charmap={current_charmap_path.relative_to(repo) if current_charmap_path.is_relative_to(repo) else current_charmap_path}\n")
    summary_lines.append("\ncode observations:\n")
    summary_lines.append(f"  FontAB entries: {len(fontab_args)}\n")
    summary_lines.append(f"  enabled FontAB pages: {len(active_pages)} ({fmt_page_set(active_pages)})\n")
    summary_lines.append(f"  disabled FontAB pages: {len(disabled_pages)} ({fmt_page_set(disabled_pages)})\n")
    summary_lines.append(f"  current DFS lead cutoff: ${lead_limit:02X} (accepted lead pages: $01-${lead_limit-1:02X})\n")
    if page_mask is not None:
        summary_lines.append(f"  inferred physical page mask: ${page_mask:02X}\n")
    else:
        summary_lines.append("  inferred physical page mask: unknown\n")
    summary_lines.append(f"  theoretical page range under mask: $00-${theoretical_max_page:02X}\n")
    summary_lines.append("\ncharmap page observations:\n")
    summary_lines.append(f"  base charmap entries: {len(base_entries)}\n")
    summary_lines.append(f"  base charmap pages: {len(base_lows)} ({fmt_page_set(base_lows.keys())})\n")
    summary_lines.append(f"  current charmap entries: {len(current_entries)}\n")
    summary_lines.append(f"  current charmap pages: {len(current_lows)} ({fmt_page_set(current_lows.keys())})\n")
    summary_lines.append("\nslot template estimate:\n")
    summary_lines.append(f"  template page: ${stats['template_page']:02X}\n" if stats.get("template_page") is not None else "  template page: unknown\n")
    summary_lines.append(f"  estimated slots/page: {capacity_per_page}\n")
    summary_lines.append("\nextension capacity estimates:\n")
    summary_lines.append(f"  dormant pages accepted by current parser: {len(disabled_under_current_limit)} ({fmt_page_set(disabled_under_current_limit)})\n")
    summary_lines.append(f"  dormant current-parser capacity: {dormant_current_capacity} glyphs\n")
    summary_lines.append(f"  additional pages if raising cutoff to $40, existing FontAB entries: {len(raise_to_40_existing_table_pages)} ({fmt_page_set(raise_to_40_existing_table_pages)})\n")
    summary_lines.append(f"  additional pages if raising cutoff to $40, requiring FontAB extension: {len(raise_to_40_new_table_pages)} ({fmt_page_set(raise_to_40_new_table_pages)})\n")
    summary_lines.append(f"  total extension pages if cutoff=$40 and FontAB reaches $3F: {total_pages_if_raise_to_40}\n")
    summary_lines.append(f"  total estimated extension capacity if cutoff=$40: {total_capacity_if_raise_to_40} glyphs\n")
    summary_lines.append("\nBig5 target comparison:\n")
    if needed_cjk:
        summary_lines.append(f"  needed CJK extension glyphs: {needed_cjk}\n")
        summary_lines.append(f"  CJK fits current dormant pages: {'yes' if dormant_current_capacity >= needed_cjk else 'no'}\n")
        summary_lines.append(f"  CJK fits cutoff=$40 plan: {'yes' if total_capacity_if_raise_to_40 >= needed_cjk else 'no'}\n")
    else:
        summary_lines.append("  needed CJK extension glyphs: unknown; pass --needed-cjk or run Big5 audit first\n")
    if needed_all:
        summary_lines.append(f"  needed all-category extension glyphs: {needed_all}\n")
        summary_lines.append(f"  all-category fits current dormant pages: {'yes' if dormant_current_capacity >= needed_all else 'no'}\n")
        summary_lines.append(f"  all-category fits cutoff=$40 plan: {'yes' if total_capacity_if_raise_to_40 >= needed_all else 'no'}\n")
        if total_capacity_if_raise_to_40 < needed_all:
            summary_lines.append(f"  all-category shortfall after cutoff=$40 plan: {needed_all - total_capacity_if_raise_to_40} glyphs\n")
    else:
        summary_lines.append("  needed all-category extension glyphs: unknown; pass --needed-all or run Big5 audit first\n")
    summary_lines.append("\noutputs:\n")
    summary_lines.append("  existing_pages.tsv\n")
    summary_lines.append("  proposed_extension_pages.tsv\n")
    summary_lines.append("  safe_low_template.tsv\n")
    summary_lines.append("  patch_notes.md\n")
    summary_lines.append("\nnotes:\n")
    summary_lines.append("  This is a static audit. A small ROM probe is still needed before committing actual extension codepoints.\n")
    summary_lines.append("  Do not treat all 16-bit values as valid DFS codepoints; current decode logic gates lead bytes.\n")

    (out_dir / "summary.txt").write_text("".join(summary_lines), encoding="utf-8")
    print("".join(summary_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
