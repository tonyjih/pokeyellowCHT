#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_big5_extension_font_assets.py

Generate Big5 DFS extension font binaries and patch the DFS engine after the
physical Big5 extension map has been built.

Inputs normally come from:
  * src/dfs/maps/big5_compat_base_map.tsv
  * src/dfs/maps/big5_extension_physical_map.tsv
  * src/dfs/maps/extension_pages_used.tsv

What this script does:
  1. Starts from the existing src/dfs/ChineseFonts_XX_H/L.bin files.
  2. Re-renders base-compatible Traditional glyphs into the original CHS slots.
  3. Creates ChineseFonts_14..3F_H/L.bin extension page binaries and renders
     Big5-only glyphs into the assigned physical DFS codepoints.
  4. Optionally patches src/dfs/dfs.asm FontAB/cutoff and src/main.asm INCBIN
     labels so the new pages are visible to the runtime.

It does not modify xlsx files and does not rebuild src/charmap.txt. Run
rebuild_full_cjk_charmap.py before this if you want a full encoder table.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERSION = "big5-extension-font-assets-2026-06-18-v2"
FONT_SIZE_BYTES = 128 * 18
HEX_RE = re.compile(r"\$([0-9A-Fa-f]{1,4})")

# Low bytes used by the legacy DFS pages. Extension maps should already have
# been assigned from reports/dfs_pages/safe_low_template.tsv, but keeping the
# patcher format identical to the legacy font layout is important:
#   low bit7 selects _L/_H bin, low&0x7F selects 0..127 glyph index.


@dataclass(frozen=True)
class GlyphJob:
    char: str
    code: int
    source: str
    compat_class: str
    note: str = ""

    @property
    def code_hex(self) -> str:
        return f"${self.code:04X}"

    @property
    def page(self) -> int:
        return (self.code >> 8) & 0xFF

    @property
    def low(self) -> int:
        return self.code & 0xFF


@dataclass
class FontABBlock:
    args: List[str]
    start: int
    end: int
    snippet: str


def repo_path(repo: Path, rel_or_abs: str | Path) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else repo / p


def parse_code(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    m = HEX_RE.search(s)
    if m:
        return int(m.group(1), 16)
    if s.lower().startswith("0x"):
        return int(s, 16)
    try:
        return int(s, 0)
    except ValueError:
        return None


def fmt_code(n: int) -> str:
    return f"${n:04X}"


def fmt_page(n: int) -> str:
    return f"${n:02X}"


def read_tsv(path: Path) -> List[dict]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_tsv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})


def strip_asm_comments(line: str) -> str:
    return line.split(";", 1)[0]


def parse_fontab(text: str) -> FontABBlock:
    idx = text.find("FontAB:")
    if idx < 0:
        raise ValueError("FontAB: label not found in dfs.asm")
    tail = text[idx:]
    end_candidates = []
    for marker in ["Send4RawFontTo8FontLeft", "Send4RawFontTo8FontRight", "GetVramAddr"]:
        j = tail.find(marker)
        if j > 0:
            end_candidates.append(j)
    end_rel = min(end_candidates) if end_candidates else min(len(tail), 4096)
    snippet = tail[:end_rel]
    tokens: List[str] = []
    for raw_line in snippet.splitlines():
        line = strip_asm_comments(raw_line)
        if not re.search(r"\bfontab\b", line, flags=re.IGNORECASE):
            continue
        line = re.split(r"\bfontab\b", line, maxsplit=1, flags=re.IGNORECASE)[1]
        for tok in re.split(r"[\s,]+", line):
            tok = tok.strip().upper().removeprefix("$")
            if re.fullmatch(r"[0-9A-F]{2}", tok):
                tokens.append(tok)
    if not tokens:
        raise ValueError("could not parse FontAB fontab arguments")
    return FontABBlock(tokens, idx, idx + end_rel, snippet)


def format_fontab(args: Sequence[str]) -> str:
    lines = ["FontAB:\n"]
    for i in range(0, len(args), 16):
        chunk = args[i : i + 16]
        lines.append("\tfontab " + ", ".join(chunk) + "\n")
    return "".join(lines)


def patch_fontab_args(original: Sequence[str], pages_to_enable: Iterable[int], extend_to: int) -> List[str]:
    args = list(original)
    if len(args) < extend_to:
        args.extend(["FF"] * (extend_to - len(args)))
    for p in pages_to_enable:
        if p == 0:
            # Page 0 is intentionally left disabled because of control/NUL ambiguity.
            continue
        if p >= len(args):
            args.extend(["FF"] * (p + 1 - len(args)))
        args[p] = f"{p:02X}"
    return args


def patch_cutoffs(text: str, new_cutoff: Optional[int]) -> Tuple[str, int]:
    if new_cutoff is None:
        return text, 0
    # Accept an already-probed $30 as well as the original $2F.
    pattern = re.compile(r"(cp\s+a\s*,\s*)\$(2F|30)", flags=re.IGNORECASE)
    new_text, n = pattern.subn(lambda m: m.group(1) + f"${new_cutoff:02X}", text)
    return new_text, n


def patch_main_font_sections(main_text: str, file_codes: Sequence[str]) -> Tuple[str, List[str]]:
    added: List[str] = []
    blocks: List[str] = []
    for code in sorted({c.upper() for c in file_codes if c.upper() != "FF"}):
        if re.search(rf"\bDFS_C_{re.escape(code)}_L::", main_text) and re.search(rf"\bDFS_C_{re.escape(code)}_H::", main_text):
            continue
        added.append(code)
        blocks.append(
            f'\nSECTION "Chinese Fonts Extension {code}", ROMX\n'
            f'DFS_C_{code}_L::\n'
            f'\tINCBIN "dfs/ChineseFonts_{code}_L.bin"\n'
            f'DFS_C_{code}_H::\n'
            f'\tINCBIN "dfs/ChineseFonts_{code}_H.bin"\n'
        )
    if not blocks:
        return main_text, added
    block = "".join(blocks) + "\n"
    marker = 'SECTION "MISC"'
    pos = main_text.find(marker)
    if pos >= 0:
        return main_text[:pos] + block + main_text[pos:], added
    return main_text.rstrip() + "\n" + block, added


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
            try:
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
            except Exception:
                sys.modules.pop(spec.name, None)
                raise
            return mod
    raise SystemExit("Could not find src/tools/build_dfs_font_from_fusion.py v5 or later.")


class DFSBig5FontPatcher:
    def __init__(self, repo: Path, out_dfs: Path, fontab_args: Sequence[str]):
        self.repo = repo
        self.src_dfs = repo / "src" / "dfs"
        self.out_dfs = out_dfs
        self.fontab_args = [x.upper() for x in fontab_args]
        self.cache: Dict[Path, bytearray] = {}
        self.touched: Set[Path] = set()
        self.created: Set[Path] = set()

    def prepare(self, pages_to_ensure: Iterable[int]) -> None:
        self.out_dfs.mkdir(parents=True, exist_ok=True)
        for src in self.src_dfs.glob("ChineseFonts_*_[HL].bin"):
            shutil.copy2(src, self.out_dfs / src.name)
        for page in pages_to_ensure:
            if page >= len(self.fontab_args):
                continue
            file_code = self.fontab_args[page]
            if file_code == "FF":
                continue
            self.ensure_pair(file_code)

    def ensure_bin(self, file_code: str, half: str) -> Path:
        path = self.out_dfs / f"ChineseFonts_{file_code}_{half}.bin"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes([0]) * FONT_SIZE_BYTES)
            self.created.add(path)
        return path

    def ensure_pair(self, file_code: str) -> None:
        self.ensure_bin(file_code, "L")
        self.ensure_bin(file_code, "H")

    def _load_target(self, file_code: str, half: str) -> bytearray:
        path = self.ensure_bin(file_code, half)
        if path not in self.cache:
            self.cache[path] = bytearray(path.read_bytes())
        return self.cache[path]

    def patch_strip(self, hi: int, lo: int, raw6: bytes) -> None:
        base = hi & 0x3F
        if base >= len(self.fontab_args):
            raise ValueError(f"hi byte page ${base:02X} beyond FontAB length {len(self.fontab_args)}")
        file_code = self.fontab_args[base]
        if file_code == "FF":
            raise ValueError(f"hi byte page ${base:02X} maps to FF")
        half = "H" if (lo & 0x80) else "L"
        low_index = lo & 0x7F
        if hi >= 0x80:
            strip_selector = 2
        elif hi >= 0x40:
            strip_selector = 1
        else:
            strip_selector = 0
        offset = low_index * 18 + strip_selector * 6
        data = self._load_target(file_code, half)
        if offset + 6 > len(data):
            data.extend(bytes([0]) * (offset + 6 - len(data)))
        data[offset : offset + 6] = raw6
        self.touched.add(self.out_dfs / f"ChineseFonts_{file_code}_{half}.bin")

    def patch_glyph(self, code: int, glyph: Sequence[Sequence[int]]) -> None:
        hi = (code >> 8) & 0xFF
        lo = code & 0xFF
        raw0, raw1, raw2 = self.pack_glyph12(glyph)
        self.patch_strip((hi & 0x3F) | 0x00, lo, raw0)
        self.patch_strip((hi & 0x3F) | 0x40, lo, raw1)
        self.patch_strip((hi & 0x3F) | 0x80, lo, raw2)

    @staticmethod
    def pack_raw4(rows: Sequence[Sequence[int]]) -> bytes:
        bits: List[int] = []
        for row in rows:
            if len(row) != 4:
                raise ValueError("raw4 row must be 4 pixels wide")
            bits.extend(1 if x else 0 for x in row)
        out = bytearray()
        for i in range(0, 48, 8):
            v = 0
            for b in bits[i : i + 8]:
                v = (v << 1) | b
            out.append(v)
        return bytes(out)

    @classmethod
    def pack_glyph12(cls, glyph: Sequence[Sequence[int]]) -> Tuple[bytes, bytes, bytes]:
        if len(glyph) != 12 or any(len(row) != 12 for row in glyph):
            raise ValueError("glyph must be 12x12")
        strips = []
        for strip_idx in range(3):
            x0 = strip_idx * 4
            rows = [[glyph[y][x0 + x] for x in range(4)] for y in range(12)]
            strips.append(cls.pack_raw4(rows))
        return strips[0], strips[1], strips[2]

    def save(self) -> None:
        for path, data in self.cache.items():
            path.write_bytes(bytes(data))



def render_char_12x12_fast(helper, font, ch: str, threshold: int, x_offset: int, y_offset: int, invert: bool) -> List[List[int]]:
    """Fast equivalent of build_dfs_font_from_fusion.render_char_12x12.

    The older helper scans a 64x64 canvas pixel-by-pixel in Python to find ink.
    This version lets Pillow build the threshold mask and only loops over the
    final 12x12 bitmap. The output layout is intentionally kept compatible with
    the existing Fusion builder.
    """
    canvas_size = 64
    bg = 255 if not invert else 0
    fg = 0 if not invert else 255
    img = helper.Image.new("L", (canvas_size, canvas_size), bg)
    draw = helper.ImageDraw.Draw(img)
    try:
        draw.text((0, 0), ch, font=font, fill=fg, anchor="lt")
    except TypeError:
        draw.text((0, 0), ch, font=font, fill=fg)

    if invert:
        mask = img.point(lambda p: 255 if p >= threshold else 0)
    else:
        mask = img.point(lambda p: 255 if p <= threshold else 0)

    bbox = mask.getbbox()
    if bbox is None:
        return [[0 for _ in range(12)] for _ in range(12)]

    glyph_img = mask.crop(bbox)
    gw, gh = glyph_img.size
    dst_x = (12 - gw) // 2 + x_offset
    dst_y = (12 - gh) // 2 + y_offset

    out_img = helper.Image.new("L", (12, 12), 0)
    src_x0 = max(0, -dst_x)
    src_y0 = max(0, -dst_y)
    dst_x0 = max(0, dst_x)
    dst_y0 = max(0, dst_y)
    w = min(gw - src_x0, 12 - dst_x0)
    h = min(gh - src_y0, 12 - dst_y0)
    if w > 0 and h > 0:
        piece = glyph_img.crop((src_x0, src_y0, src_x0 + w, src_y0 + h))
        out_img.paste(piece, (dst_x0, dst_y0))

    pix = out_img.load()
    return [[1 if pix[x, y] else 0 for x in range(12)] for y in range(12)]

def load_compat_jobs(path: Path, mode: str) -> List[GlyphJob]:
    jobs: List[GlyphJob] = []
    for r in read_tsv(path):
        cls = (r.get("compat_class") or "").strip()
        ch = r.get("char") or r.get("ch") or ""
        code = parse_code(r.get("dfs_code", ""))
        if not ch or code is None:
            continue
        if mode == "none":
            continue
        if mode == "patches" and cls == "base-exact":
            continue
        # mode == all patches base-exact too, mostly harmless but slower. Default avoids it.
        jobs.append(GlyphJob(ch, code, "base-compatible", cls, r.get("note", "")))
    return jobs


def load_extension_jobs(path: Path) -> List[GlyphJob]:
    jobs: List[GlyphJob] = []
    for r in read_tsv(path):
        ch = r.get("char") or r.get("ch") or ""
        code = parse_code(r.get("dfs_code", ""))
        if not ch or code is None:
            continue
        jobs.append(GlyphJob(ch, code, r.get("slot_source", "big5-extension"), r.get("compat_class", "big5-extension"), r.get("note", "")))
    return jobs


def dedupe_jobs(jobs: Sequence[GlyphJob]) -> Tuple[List[GlyphJob], List[dict]]:
    # A codepoint has one glyph. If duplicate jobs point to same code, keep the
    # first non-base-exact job because it is usually the Traditional glyph patch.
    by_code: Dict[int, GlyphJob] = {}
    conflicts: List[dict] = []
    for job in jobs:
        old = by_code.get(job.code)
        if old is None:
            by_code[job.code] = job
            continue
        if old.char != job.char:
            conflicts.append({
                "dfs_code": old.code_hex,
                "kept_char": old.char,
                "skipped_char": job.char,
                "kept_class": old.compat_class,
                "skipped_class": job.compat_class,
                "note": "one glyph per DFS codepoint",
            })
        # Keep existing.
    return [by_code[k] for k in sorted(by_code)], conflicts


def write_jobs(path: Path, jobs: Sequence[GlyphJob]) -> None:
    write_tsv(path, ["char", "dfs_code", "page", "low", "source", "compat_class", "note"], [
        {
            "char": j.char,
            "dfs_code": j.code_hex,
            "page": fmt_page(j.page),
            "low": fmt_page(j.low),
            "source": j.source,
            "compat_class": j.compat_class,
            "note": j.note,
        }
        for j in jobs
    ])


def apply_generated_fonts(repo: Path, out_dfs: Path, backup_dir: Path) -> None:
    src_dfs = repo / "src" / "dfs"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(out_dfs.glob("ChineseFonts_*_[HL].bin")):
        dst = src_dfs / f.name
        if dst.exists():
            b = backup_dir / "src" / "dfs" / f.name
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, b)
        shutil.copy2(f, dst)


def patch_engine(repo: Path, pages: Sequence[int], report_dir: Path, apply: bool) -> List[str]:
    dfs_path = repo / "src" / "dfs" / "dfs.asm"
    main_path = repo / "src" / "main.asm"
    if not dfs_path.exists() or not main_path.exists():
        raise SystemExit("Missing src/dfs/dfs.asm or src/main.asm")

    dfs_text = dfs_path.read_text(encoding="utf-8", errors="replace")
    fontab = parse_fontab(dfs_text)
    max_page = max(pages) if pages else 0
    extend_to = max(len(fontab.args), max_page + 1)
    if max_page >= 0x30:
        extend_to = max(extend_to, 0x40)
    new_args = patch_fontab_args(fontab.args, pages, extend_to)
    patched_dfs = dfs_text[:fontab.start] + format_fontab(new_args) + dfs_text[fontab.end:]

    cutoff_target: Optional[int]
    if max_page >= 0x30:
        cutoff_target = 0x40
    elif max_page >= 0x2F:
        cutoff_target = 0x30
    else:
        cutoff_target = None
    patched_dfs, cutoff_patches = patch_cutoffs(patched_dfs, cutoff_target)

    file_codes = [new_args[p] for p in sorted(set(pages)) if p < len(new_args) and new_args[p].upper() != "FF"]
    main_text = main_path.read_text(encoding="utf-8", errors="replace")
    patched_main, added_main = patch_main_font_sections(main_text, file_codes)

    preview_dir = report_dir / "engine_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    (preview_dir / "dfs.asm").write_text(patched_dfs, encoding="utf-8")
    (preview_dir / "main.asm").write_text(patched_main, encoding="utf-8")

    lines = []
    lines.append("engine patch plan:")
    lines.append(f"  pages enabled: {', '.join(fmt_page(p) for p in sorted(set(pages)))}")
    lines.append(f"  original FontAB entries: {len(fontab.args)}")
    lines.append(f"  patched FontAB entries: {len(new_args)}")
    lines.append(f"  cutoff target: {'unchanged' if cutoff_target is None else fmt_page(cutoff_target)}")
    lines.append(f"  cutoff replacements: {cutoff_patches}")
    lines.append(f"  main.asm labels added: {', '.join(added_main) if added_main else 'none'}")

    if apply:
        backup_dir = report_dir / "backup_engine"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dfs_path, backup_dir / "dfs.asm.before_big5_extension")
        shutil.copy2(main_path, backup_dir / "main.asm.before_big5_extension")
        dfs_path.write_text(patched_dfs, encoding="utf-8")
        main_path.write_text(patched_main, encoding="utf-8")
        lines.append("  applied: yes")
    else:
        lines.append("  applied: no")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Build Big5 DFS extension font binaries and optionally patch DFS engine.")
    ap.add_argument("--version", action="store_true", help="Print version and exit")
    ap.add_argument("--repo", default=".", help="Repository root")
    ap.add_argument("--compat-map", default="src/dfs/maps/big5_compat_base_map.tsv")
    ap.add_argument("--extension-map", default="src/dfs/maps/big5_extension_physical_map.tsv")
    ap.add_argument("--out", default="reports/big5_extension_assets")
    ap.add_argument("--base-compat-mode", choices=["patches", "all", "none"], default="patches", help="Render base compat glyph patches; default skips base-exact rows.")
    ap.add_argument("--apply", action="store_true", help="Apply both generated font bins and engine patches to src/.")
    ap.add_argument("--apply-fonts", action="store_true", help="Apply generated ChineseFonts_*.bin files to src/dfs/.")
    ap.add_argument("--patch-engine", action="store_true", help="Patch src/dfs/dfs.asm and src/main.asm for extension pages.")
    ap.add_argument("--preview", default="reports/big5_extension_assets/preview.png")
    ap.add_argument("--preview-limit", type=int, default=0, help="Max glyphs to draw in preview; 0 disables preview. Default is 0 for faster iteration.")
    ap.add_argument("--cols", type=int, default=32)
    ap.add_argument("--scale", type=int, default=2)
    # Fusion font options matching build_dfs_font_from_fusion.py.
    ap.add_argument("--font-path", default=None)
    ap.add_argument("--font-release", default="latest")
    ap.add_argument("--font-url", default=None)
    ap.add_argument("--font-cache", default=".cache/fusion-pixel-font")
    ap.add_argument("--fusion-pixel-size", type=int, default=12)
    ap.add_argument("--fusion-width", default="monospaced")
    ap.add_argument("--fusion-lang", default="zh_hant")
    ap.add_argument("--fusion-format", default="ttf")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--font-size", type=int, default=12)
    ap.add_argument("--threshold", type=int, default=128)
    ap.add_argument("--x-offset", type=int, default=0)
    ap.add_argument("--y-offset", type=int, default=0)
    ap.add_argument("--invert", action="store_true")
    args = ap.parse_args()

    if args.version:
        print(VERSION)
        return 0

    repo = Path(args.repo).resolve()
    out = repo_path(repo, args.out)
    out.mkdir(parents=True, exist_ok=True)
    out_dfs = out / "dfs"

    helper = import_font_helper(repo)
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
    font_source = helper.resolve_font_path(font_args, repo)
    font = helper.ImageFont.truetype(str(font_source.font_path), args.font_size)

    dfs_path = repo / "src" / "dfs" / "dfs.asm"
    fontab = parse_fontab(dfs_path.read_text(encoding="utf-8", errors="replace"))

    compat_jobs = load_compat_jobs(repo_path(repo, args.compat_map), args.base_compat_mode)
    ext_jobs = load_extension_jobs(repo_path(repo, args.extension_map))
    jobs, conflicts = dedupe_jobs(compat_jobs + ext_jobs)
    pages_used = sorted({j.page & 0x3F for j in ext_jobs})

    max_page = max(pages_used) if pages_used else 0
    extend_to = max(len(fontab.args), max_page + 1)
    if max_page >= 0x30:
        extend_to = max(extend_to, 0x40)
    patched_fontab = patch_fontab_args(fontab.args, pages_used, extend_to)

    patcher = DFSBig5FontPatcher(repo, out_dfs, patched_fontab)
    patcher.prepare(pages_used)

    preview_rows = []
    render_failures: List[dict] = []
    for idx, job in enumerate(jobs):
        try:
            glyph = render_char_12x12_fast(helper, font, job.char, args.threshold, args.x_offset, args.y_offset, args.invert)
            patcher.patch_glyph(job.code, glyph)
            if args.preview_limit and len(preview_rows) < args.preview_limit:
                entry = helper.CharmapEntry(job.char, job.code, 0)
                preview_rows.append((job.char, entry, glyph))
        except Exception as exc:
            render_failures.append({"char": job.char, "dfs_code": job.code_hex, "error": str(exc)})
    patcher.save()

    if args.preview_limit and preview_rows:
        preview_path = repo_path(repo, args.preview)
        helper.draw_preview(preview_rows, preview_path, args.cols, args.scale)
    else:
        preview_path = Path("")

    write_jobs(out / "glyph_jobs.tsv", jobs)
    write_tsv(out / "codepoint_job_conflicts.tsv", ["dfs_code", "kept_char", "skipped_char", "kept_class", "skipped_class", "note"], conflicts)
    write_tsv(out / "render_failures.tsv", ["char", "dfs_code", "error"], render_failures)

    touched_rows = []
    for p in sorted(patcher.touched | patcher.created):
        touched_rows.append({"file": str(p.relative_to(repo) if p.is_relative_to(repo) else p), "created": "yes" if p in patcher.created else "no"})
    write_tsv(out / "touched_font_bins.tsv", ["file", "created"], touched_rows)

    apply_fonts = args.apply or args.apply_fonts
    apply_engine = args.apply or args.patch_engine
    if apply_fonts:
        apply_generated_fonts(repo, out_dfs, out / "backup_fonts")

    engine_lines = patch_engine(repo, pages_used, out, apply_engine)

    by_source = Counter(j.source for j in jobs)
    by_class = Counter(j.compat_class for j in jobs)
    by_page = Counter(fmt_page(j.page & 0x3F) for j in ext_jobs)

    lines: List[str] = []
    lines.append(f"build_big5_extension_font_assets {VERSION}")
    lines.append(f"repo={repo}")
    lines.append(f"compat_map={args.compat_map}")
    lines.append(f"extension_map={args.extension_map}")
    lines.append(f"font_path={font_source.font_path}")
    lines.append(f"font_release={getattr(font_source, 'release_tag', '')}")
    lines.append(f"preview={preview_path if preview_path else '(disabled)'}")
    lines.append("")
    lines.append("counts:")
    lines.append(f"  base compat jobs loaded: {len(compat_jobs)}")
    lines.append(f"  extension jobs loaded: {len(ext_jobs)}")
    lines.append(f"  unique glyph jobs rendered: {len(jobs)}")
    lines.append(f"  codepoint job conflicts skipped: {len(conflicts)}")
    lines.append(f"  render failures: {len(render_failures)}")
    lines.append(f"  extension pages used: {len(pages_used)} ({', '.join(fmt_page(p) for p in pages_used)})")
    lines.append(f"  font bins touched/created: {len(patcher.touched)} / {len(patcher.created)}")
    lines.append(f"  apply_fonts: {'yes' if apply_fonts else 'no'}")
    lines.append(f"  patch_engine: {'yes' if apply_engine else 'no'}")
    lines.append("")
    lines.append("jobs by source:")
    for k, v in sorted(by_source.items()):
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("jobs by compat_class:")
    for k, v in sorted(by_class.items()):
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("extension rows by page:")
    for k, v in sorted(by_page.items()):
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.extend(engine_lines)
    lines.append("")
    lines.append("outputs:")
    lines.append("  dfs/ChineseFonts_XX_H/L.bin under output dir")
    lines.append("  glyph_jobs.tsv")
    lines.append("  touched_font_bins.tsv")
    lines.append("  codepoint_job_conflicts.tsv")
    lines.append("  render_failures.tsv")
    lines.append("  engine_preview/dfs.asm")
    lines.append("  engine_preview/main.asm")
    if apply_fonts:
        lines.append("  src/dfs/ChineseFonts_*.bin")
    if apply_engine:
        lines.append("  src/dfs/dfs.asm")
        lines.append("  src/main.asm")
    lines.append("")
    lines.append("notes:")
    lines.append("  Start from clean base ChineseFonts bins before using this for an upstream-quality asset rebuild.")
    lines.append("  Re-run _prepare.command from a clean buildYUS after applying, so new INCBIN dependencies are copied.")

    (out / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

    if render_failures:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
