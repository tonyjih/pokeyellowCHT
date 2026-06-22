#!/usr/bin/env python3
"""
Patch DFS glyphs for original CHS IME candidate codepoints so they display as
Traditional Chinese while keeping the original byte/codepoint values.

This is a post-processing pass after build_cht_text_assets.py V13+ has generated
an IME-compatible charmap/font. It does not modify IME tables and does not
reassign charmap codepoints; it only redraws old IME candidate glyph slots with
their Traditional equivalents.
"""
from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import importlib.util
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence, Tuple

try:
    from opencc import OpenCC
except ImportError:  # pragma: no cover
    OpenCC = None  # type: ignore

VERSION = "patch-ime-traditional-glyphs-2026-06-18-v1"
CHARMAP_TXT_RE = re.compile(r"^(.+?)=\$?([0-9A-Fa-f]{4})\s*(?:;.*)?$")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

@dataclasses.dataclass(frozen=True)
class CharmapEntryLite:
    char: str
    code: int
    line_no: int

    @property
    def hi(self) -> int:
        return (self.code >> 8) & 0xFF

    @property
    def lo(self) -> int:
        return self.code & 0xFF

    @property
    def code_hex(self) -> str:
        return f"${self.code:04X}"


def is_cjk(ch: str) -> bool:
    return len(ch) == 1 and bool(CJK_RE.fullmatch(ch))


def load_charmap(path: Path) -> Tuple[Dict[str, CharmapEntryLite], Dict[int, CharmapEntryLite]]:
    by_char: Dict[str, CharmapEntryLite] = {}
    by_code: Dict[int, CharmapEntryLite] = {}
    for line_no, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = CHARMAP_TXT_RE.match(raw.strip())
        if not m:
            continue
        ch = m.group(1).strip()
        if len(ch) != 1:
            continue
        code = int(m.group(2), 16)
        entry = CharmapEntryLite(ch, code, line_no)
        by_char.setdefault(ch, entry)
        by_code.setdefault(code, entry)
    return by_char, by_code


def parse_ime_codes(path: Path) -> Tuple[List[int], Dict[int, int]]:
    label_re = re.compile(r"^\s*(IME_[A-Za-z0-9_]+_Char):")
    byte_re = re.compile(r"\$([0-9A-Fa-f]{2})")
    counts: Dict[int, int] = collections.Counter()
    order: List[int] = []
    current: Optional[str] = None
    current_bytes: List[int] = []
    ended = False

    def flush() -> None:
        nonlocal current, current_bytes, ended
        if current is None:
            return
        usable: List[int] = []
        for b in current_bytes:
            if b == 0x50:
                break
            usable.append(b)
        for i in range(0, len(usable) - 1, 2):
            code = (usable[i] << 8) | usable[i + 1]
            if code not in counts:
                order.append(code)
            counts[code] += 1
        current = None
        current_bytes = []
        ended = False

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = label_re.match(raw)
        if m:
            flush()
            current = m.group(1)
            current_bytes = []
            ended = False
            continue
        if current is None or ended:
            continue
        line = raw.split(";", 1)[0]
        if "db" not in line:
            continue
        vals = [int(x, 16) for x in byte_re.findall(line)]
        if not vals:
            continue
        current_bytes.extend(vals)
        if 0x50 in vals:
            ended = True
            flush()
    flush()
    return order, dict(counts)


def make_opencc(mode: str):
    if mode.lower() in {"none", "identity", "raw"}:
        class Identity:
            def convert(self, s: str) -> str:
                return s
        return Identity()
    if OpenCC is None:
        raise SystemExit("Missing dependency: opencc-python-reimplemented. Install with: python3 -m pip install --user opencc-python-reimplemented")
    try:
        return OpenCC(mode)
    except Exception:
        return OpenCC(mode + ".json")


def import_font_helper(repo: Path):
    candidate_paths = [
        repo / "src" / "tools" / "build_dfs_font_from_fusion.py",
        Path(__file__).resolve().with_name("build_dfs_font_from_fusion.py"),
        Path("/mnt/data/build_dfs_font_from_fusion_autodl_v5.py"),
    ]
    for path in candidate_paths:
        if path.exists():
            spec = importlib.util.spec_from_file_location("build_dfs_font_from_fusion_helper", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    raise SystemExit("Could not find build_dfs_font_from_fusion.py helper")


def resolve_font(repo: Path, helper, args: argparse.Namespace):
    font_args = SimpleNamespace(
        font_path=args.font_path,
        font_release=args.font_release,
        font_url=args.font_url,
        font_cache=args.font_cache,
        fusion_pixel_size=args.fusion_pixel_size,
        fusion_width=args.fusion_width,
        fusion_lang=args.fusion_lang,
        fusion_format=args.fusion_format,
        force_download=args.force_download,
    )
    return helper.resolve_font_path(font_args, repo)


def backup_and_apply(repo: Path, dfs_out: Path) -> None:
    backup_dir = repo / "backups" / f"ime_traditional_glyphs_{time.strftime('%Y%m%d_%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for src_file in sorted(dfs_out.glob("ChineseFonts_*_[HL].bin")):
        target = repo / "src" / "dfs" / src_file.name
        if target.exists():
            dest = backup_dir / "src" / "dfs" / target.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
        shutil.copy2(src_file, target)
    print(f"applied IME traditional glyphs; backups written to {backup_dir}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Patch old CHS IME candidate glyph slots to Traditional glyphs without changing codepoints.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base-charmap", default="reports/charmap_chs_base.txt")
    ap.add_argument("--base-ime", default="reports/IMECharTable_chs_base.asm")
    ap.add_argument("--current-charmap", default="src/charmap.txt")
    ap.add_argument("--mode", default="s2twp", help="OpenCC mode for old IME char -> displayed Traditional char. Default: s2twp")
    ap.add_argument("--out", default="reports/cht_ime_glyphs")
    ap.add_argument("--preview", default="reports/cht_ime_glyphs/ime_traditional_glyph_preview.png")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--overwrite-current-allocation", action="store_true", help="Allow patching a codepoint even if current charmap maps another generated CJK char to it. Normally leave off.")
    ap.add_argument("--font-path")
    ap.add_argument("--font-release", default="latest")
    ap.add_argument("--font-url")
    ap.add_argument("--font-cache", default=".cache/fusion-pixel-font")
    ap.add_argument("--fusion-pixel-size", default="12px")
    ap.add_argument("--fusion-width", default="monospaced")
    ap.add_argument("--fusion-lang", default="zh_hant")
    ap.add_argument("--fusion-format", default="ttf")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--font-size", type=int, default=12)
    ap.add_argument("--threshold", type=int, default=128)
    ap.add_argument("--x-offset", type=int, default=0)
    ap.add_argument("--y-offset", type=int, default=0)
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--cols", type=int, default=32)
    ap.add_argument("--scale", type=int, default=4)
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    base_charmap = Path(args.base_charmap)
    if not base_charmap.is_absolute():
        base_charmap = repo / base_charmap
    base_ime = Path(args.base_ime)
    if not base_ime.is_absolute():
        base_ime = repo / base_ime
    current_charmap = Path(args.current_charmap)
    if not current_charmap.is_absolute():
        current_charmap = repo / current_charmap
    out = Path(args.out)
    if not out.is_absolute():
        out = repo / out
    out.mkdir(parents=True, exist_ok=True)

    _, base_by_code = load_charmap(base_charmap)
    _, current_by_code = load_charmap(current_charmap)
    ime_order, ime_counts = parse_ime_codes(base_ime)
    cc = make_opencc(args.mode)

    patch_plan: List[Tuple[int, str, str, int]] = []
    skipped_rows: List[Tuple[str, str, str, str, str]] = []
    for code in ime_order:
        base = base_by_code.get(code)
        if base is None:
            skipped_rows.append((f"${code:04X}", "", "", "no-base-charmap-entry", str(ime_counts.get(code, 0))))
            continue
        if not is_cjk(base.char):
            skipped_rows.append((base.code_hex, base.char, base.char, "non-cjk", str(ime_counts.get(code, 0))))
            continue
        trad = cc.convert(base.char)
        if len(trad) != 1 or not is_cjk(trad):
            skipped_rows.append((base.code_hex, base.char, trad, "conversion-not-single-cjk", str(ime_counts.get(code, 0))))
            continue
        if trad == base.char:
            skipped_rows.append((base.code_hex, base.char, trad, "same-after-conversion", str(ime_counts.get(code, 0))))
            continue
        current = current_by_code.get(code)
        if current is not None and is_cjk(current.char) and current.char != trad and not args.overwrite_current_allocation:
            skipped_rows.append((base.code_hex, base.char, trad, f"current-charmap-uses-code-for-{current.char}", str(ime_counts.get(code, 0))))
            continue
        patch_plan.append((code, base.char, trad, ime_counts.get(code, 0)))

    helper = import_font_helper(repo)
    font_source = resolve_font(repo, helper, args)
    font = helper.ImageFont.truetype(str(font_source.font_path), args.font_size)

    dfs_out = out / "dfs"
    patcher = helper.DFSFontPatcher(repo, dfs_out, False)
    patcher.prepare()

    rendered = []
    for code, old_ch, trad_ch, refs in patch_plan:
        entry = helper.CharmapEntry(trad_ch, code, 0)
        glyph = helper.render_char_12x12(font, trad_ch, args.threshold, args.x_offset, args.y_offset, args.invert)
        patcher.patch_glyph(entry, glyph)
        rendered.append((trad_ch, entry, glyph))
    patcher.save()

    preview = Path(args.preview)
    if not preview.is_absolute():
        preview = repo / preview
    helper.draw_preview(rendered, preview, args.cols, args.scale)

    with (out / "ime_traditional_glyph_patches.tsv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["code", "base_char", "render_char", "ime_refs"])
        for code, old_ch, trad_ch, refs in patch_plan:
            w.writerow([f"${code:04X}", old_ch, trad_ch, refs])
    with (out / "ime_traditional_glyph_skipped.tsv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["code", "base_char", "converted", "reason", "ime_refs"])
        w.writerows(skipped_rows)
    summary = [
        f"version={VERSION}",
        f"base_charmap={base_charmap}",
        f"base_ime={base_ime}",
        f"current_charmap={current_charmap}",
        f"opencc_mode={args.mode}",
        f"ime_unique_codes={len(ime_order)}",
        f"patched_glyphs={len(patch_plan)}",
        f"skipped={len(skipped_rows)}",
        f"font_path={font_source.font_path}",
        f"preview={preview}",
    ]
    (out / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")

    print("IME traditional glyph pass")
    for line in summary:
        print("  " + line)
    print(f"  report={out / 'ime_traditional_glyph_patches.tsv'}")
    print(f"  skipped={out / 'ime_traditional_glyph_skipped.tsv'}")

    if args.apply:
        backup_and_apply(repo, dfs_out)
    else:
        print("dry-run/output only. Add --apply to copy patched ChineseFonts_*.bin into src/dfs.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
