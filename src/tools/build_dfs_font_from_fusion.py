#!/usr/bin/env python3
"""
Build / patch pokeyellowCHS/pokeyellowCHT DFS Chinese font binaries from Fusion Pixel Font.

This script can either use a local TTF/OTF file (--font-path) or automatically download
Fusion Pixel Font from GitHub Releases. Downloaded fonts are cached locally under
.cache/fusion-pixel-font by default and should not be committed.

Format handled here is the inverse of dump_dfs_font.py:
- One CJK glyph body is 12x12 pixels.
- It is split into three 4x12 vertical strips.
- Each 4x12 strip is packed into 6 bytes: two 4-bit rows per byte.
- The three strips are stored under hi-byte variants base, base|0x40, base|0x80.
- The low byte selects a 0..127 glyph index; bit 7 selects *_L.bin or *_H.bin.

Typical first test from repo root, using auto-download:

    python3 tools/build_dfs_font_from_fusion.py \
        --repo . \
        --chars 寶可夢訓練家 \
        --out-dfs reports/dfs_font_fusion_test \
        --preview reports/dfs_font_fusion_test.png

Equivalent manual-font mode:

    python3 tools/build_dfs_font_from_fusion.py \
        --repo . \
        --font-path path/to/FusionPixel12pxMonospaced...zh_hant...ttf \
        --chars 寶可夢訓練家

It writes patched copies of ChineseFonts_*.bin to --out-dfs by default. It does not modify
src/dfs unless you explicitly pass --in-place.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:
    raise SystemExit("Missing dependency: Pillow. Install with: python3 -m pip install pillow") from exc

try:
    from opencc import OpenCC  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    OpenCC = None  # type: ignore

FUSION_REPO = "TakWolf/fusion-pixel-font"
GITHUB_API = "https://api.github.com"
VERSION = "autodl-2026-06-18-v5"
USER_AGENT = f"pokeyellowCHT-dfs-font-builder/{VERSION}"

FONTAB: List[str] = [
    "FF", "01", "02", "03", "04", "05", "06", "07", "08", "09", "0A", "0B", "0C", "0D", "0E", "0F",
    "10", "11", "12", "13", "FF", "FF", "FF", "FF", "18", "19", "1A", "1B", "1C", "1D", "1E", "1F",
    "FF", "FF", "FF", "FF", "FF", "FF", "FF", "FF", "28", "29", "2A", "2B", "2C", "2D", "2E", "FF",
]

CHARMAP_RE = re.compile(r"^(.+?)=\$?([0-9A-Fa-f]{4})\s*(?:;.*)?$")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")
FONT_SUFFIXES = (".ttf", ".otf")

# Minimal fallback table for testing without OpenCC. OpenCC t2s is used when
# available; this table just covers common Pokemon text samples and early UI terms.
BUILTIN_TRAD_TO_SIMP = {
    "寶": "宝", "夢": "梦", "訓": "训", "練": "练", "龍": "龙", "圖": "图",
    "鑑": "鉴", "館": "馆", "隊": "队", "機": "机", "錄": "录", "儲": "储",
    "選": "选", "項": "项", "進": "进", "對": "对", "戰": "战", "鬥": "斗",
    "藥": "药", "傷": "伤", "級": "级", "葉": "叶", "鎮": "镇", "紅": "红",
    "聖": "圣", "號": "号", "島": "岛", "華": "华", "藍": "蓝", "淺": "浅",
    "黃": "黄", "滿": "满", "萬": "万", "復": "复", "離": "离", "繩": "绳",
    "燒": "烧", "凍": "冻", "電": "电", "腦": "脑", "學": "学", "習": "习",
    "傳": "传", "絕": "绝", "體": "体", "屬": "属", "氣": "气", "發": "发",
    "覺": "觉", "觀": "观", "這": "这", "個": "个", "說": "说", "沒": "没",
    "為": "为", "與": "与", "會": "会", "來": "来", "時": "时", "麼": "么",
}


@dataclasses.dataclass(frozen=True)
class CharmapEntry:
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


@dataclasses.dataclass(frozen=True)
class FontSourceInfo:
    font_path: Path
    release_tag: str
    asset_name: str
    asset_url: str
    extracted_from: str
    sha256: str


def parse_charmap(path: Path) -> Dict[str, CharmapEntry]:
    result: Dict[str, CharmapEntry] = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        match = CHARMAP_RE.match(line)
        if not match:
            continue
        token, hex_code = match.groups()
        token = token.strip()
        if len(token) != 1:
            continue
        if not CJK_RE.search(token):
            continue
        result.setdefault(token, CharmapEntry(token, int(hex_code, 16), line_no))
    return result


def load_alias_map(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"alias map not found: {p}")
    result: Dict[str, str] = {}
    for line_no, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" in line:
            left, right = line.split("=", 1)
        else:
            parts = line.split()
            if len(parts) < 2:
                raise SystemExit(f"bad alias map line {line_no}: {raw}")
            left, right = parts[0], parts[1]
        left = left.strip()
        right = right.strip()
        if len(left) != 1 or len(right) != 1:
            raise SystemExit(f"alias map line {line_no} must map one char to one char: {raw}")
        result[left] = right
    return result


def make_t2s_converter():
    if OpenCC is None:
        return None
    for mode in ("t2s", "t2s.json"):
        try:
            return OpenCC(mode)  # type: ignore[misc]
        except Exception:
            pass
    return None


def t2s_candidates(ch: str, converter) -> List[str]:
    candidates: List[str] = []
    if ch in BUILTIN_TRAD_TO_SIMP:
        candidates.append(BUILTIN_TRAD_TO_SIMP[ch])
    if converter is not None:
        try:
            converted = converter.convert(ch)
            if len(converted) == 1:
                candidates.append(converted)
        except Exception:
            pass
    # Deduplicate while preserving order.
    out: List[str] = []
    seen = set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def resolve_charmap_entries(
    chars: str,
    entries: Dict[str, CharmapEntry],
    alias_map: Dict[str, str],
    t2s_fallback: bool,
) -> Tuple[List[Tuple[str, str, CharmapEntry]], List[str], List[str]]:
    """Return (render char, lookup char, entry).

    render char is the glyph we draw from Fusion Pixel Font.
    lookup char is the existing charmap char whose codepoint/glyph slot we patch.
    """
    converter = make_t2s_converter() if t2s_fallback else None
    rows: List[Tuple[str, str, CharmapEntry]] = []
    missing: List[str] = []
    notes: List[str] = []
    for ch in chars:
        if ch in entries:
            rows.append((ch, ch, entries[ch]))
            continue
        mapped = alias_map.get(ch)
        if mapped and mapped in entries:
            rows.append((ch, mapped, entries[mapped]))
            notes.append(f"alias-map: {ch} -> {mapped} {entries[mapped].code_hex}")
            continue
        if t2s_fallback:
            found = False
            for cand in t2s_candidates(ch, converter):
                if cand in entries:
                    rows.append((ch, cand, entries[cand]))
                    notes.append(f"t2s-fallback: {ch} -> {cand} {entries[cand].code_hex}")
                    found = True
                    break
            if found:
                continue
        missing.append(ch)
    return rows, missing, notes


def unique_chars(chars: str) -> str:
    seen = set()
    out = []
    for ch in chars:
        if ch.isspace():
            continue
        if ch not in seen:
            seen.add(ch)
            out.append(ch)
    return "".join(out)


def load_chars(args: argparse.Namespace) -> str:
    buf = []
    if args.chars:
        buf.append(args.chars)
    if args.char_file:
        buf.append(Path(args.char_file).read_text(encoding="utf-8", errors="replace"))
    if not buf:
        raise SystemExit("Pass --chars or --char-file.")
    return unique_chars("".join(buf))


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def safe_file_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", name) or "downloaded_font_asset"


def has_archive_suffix(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def has_font_suffix(name: str) -> bool:
    return name.lower().endswith(FONT_SUFFIXES)


def has_webfont_marker(name: str) -> bool:
    lower = name.lower()
    return any(marker in lower for marker in (".woff", "-woff", "_woff", "woff2", "webfont"))


def wrong_format_marker(name: str, fmt: str) -> bool:
    lower = name.lower()
    fmt = fmt.lower().lstrip(".")
    if fmt == "ttf":
        return ("-otf" in lower or "_otf" in lower or ".otf" in lower) and "ttf" not in lower
    if fmt == "otf":
        return ("-ttf" in lower or "_ttf" in lower or ".ttf" in lower) and "otf" not in lower
    return False


def exact_release_archive_marker(name: str, pixel_size: int, width_mode: str, fmt: str) -> bool:
    # The current Fusion Pixel Font assets are named like:
    # fusion-pixel-font-12px-monospaced-ttf-v2026.05.07.zip
    lower = name.lower()
    return f"{pixel_size}px-{width_mode.lower()}-{fmt.lower().lstrip('.')}" in lower and not has_webfont_marker(lower)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def request_headers() -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json, application/octet-stream;q=0.9, */*;q=0.8",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_json(url: str) -> Dict[str, object]:
    req = urllib.request.Request(url, headers=request_headers())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API request failed: {exc.code} {exc.reason}\n{url}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network error while contacting GitHub: {exc}") from exc


def download_file(url: str, dest: Path, force: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force and dest.stat().st_size > 0:
        return
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    req = urllib.request.Request(url, headers=request_headers())
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        tmp.replace(dest)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"download failed: {exc.code} {exc.reason}\n{url}\n{body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"download failed: {exc}\n{url}") from exc
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def score_font_name(
    name: str,
    pixel_size: int,
    width_mode: str,
    lang: str,
    fmt: str,
    allow_archive: bool,
) -> int:
    """Higher is better; negative means not usable."""
    lower = name.lower()
    norm = normalize_name(name)
    fmt = fmt.lower().lstrip(".")
    lang_norm = normalize_name(lang)
    width_norm = normalize_name(width_mode)
    size_tokens = [f"{pixel_size}px", f"{pixel_size}pixel", f"{pixel_size}"]

    # Do not allow webfont archives such as ...ttf.woff2... when Pillow needs a real
    # local .ttf/.otf. This was the v2 bug: the release asset name contained both
    # "ttf" and "woff2", so it won the score sort but the archive had no .ttf.
    if has_webfont_marker(lower):
        return -1
    if wrong_format_marker(lower, fmt):
        return -1

    is_font = lower.endswith(f".{fmt}") or (fmt == "ttf" and lower.endswith(".ttf")) or (fmt == "otf" and lower.endswith(".otf"))
    is_archive = has_archive_suffix(lower)
    if not is_font and not (allow_archive and is_archive):
        return -1

    score = 0
    if allow_archive and is_archive and exact_release_archive_marker(name, pixel_size, width_mode, fmt):
        score += 1000
    if "fusion" in norm:
        score += 20
    if "pixel" in norm:
        score += 20
    if any(normalize_name(tok) in norm for tok in size_tokens):
        score += 120
    else:
        # Avoid accidentally using 8px/10px when we want 12px.
        if "8px" in norm or "10px" in norm or "12px" in norm:
            return -1
    if width_norm in norm:
        score += 100
    elif width_mode.lower() == "monospaced" and "mono" in norm:
        score += 60
    else:
        if "monospaced" in norm or "proportional" in norm:
            return -1
    if lang_norm in norm:
        score += 100
    else:
        # Assets/archives may contain every language. Penalize but keep candidates alive.
        score -= 40
    if is_font:
        score += 80
    if is_archive:
        score += 10
        if fmt in norm:
            score += 20
        if "all" in norm or "dist" in norm or "release" in norm:
            score += 10
    if lower.endswith(f".{fmt}"):
        score += 40
    return score


def github_release(font_release: str) -> Dict[str, object]:
    if font_release == "latest":
        url = f"{GITHUB_API}/repos/{FUSION_REPO}/releases/latest"
    else:
        url = f"{GITHUB_API}/repos/{FUSION_REPO}/releases/tags/{font_release}"
    data = fetch_json(url)
    if not isinstance(data.get("assets"), list):
        raise SystemExit("GitHub release response did not contain an assets list.")
    return data


def select_release_asset(
    release: Dict[str, object],
    pixel_size: int,
    width_mode: str,
    lang: str,
    fmt: str,
) -> Dict[str, object]:
    assets = release.get("assets", [])
    scored: List[Tuple[int, Dict[str, object]]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        score = score_font_name(name, pixel_size, width_mode, lang, fmt, allow_archive=True)
        if score >= 0:
            scored.append((score, asset))
    if not scored:
        names = "\n".join("  " + str(a.get("name", "")) for a in assets if isinstance(a, dict))
        raise SystemExit(
            "No matching Fusion Pixel Font release asset found.\n"
            f"Wanted: {pixel_size}px {width_mode} {lang} .{fmt}\n"
            "Available assets:\n" + names
        )
    scored.sort(key=lambda item: (item[0], str(item[1].get("name", ""))), reverse=True)
    return scored[0][1]


def extract_font_from_archive(
    archive_path: Path,
    cache_dir: Path,
    pixel_size: int,
    width_mode: str,
    lang: str,
    fmt: str,
    force: bool,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    candidates: List[Tuple[int, str, bytes]] = []

    def consider_member(name: str, data: bytes) -> None:
        score = score_font_name(name, pixel_size, width_mode, lang, fmt, allow_archive=False)
        if score >= 0:
            candidates.append((score, name, data))

    lower = archive_path.name.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if not has_font_suffix(name):
                    continue
                consider_member(name, zf.read(info))
    else:
        mode = "r:*"
        with tarfile.open(archive_path, mode) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                name = member.name
                if not has_font_suffix(name):
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                consider_member(name, fh.read())

    if not candidates:
        raise SystemExit(
            f"No matching font found inside archive: {archive_path}\n"
            f"Wanted: {pixel_size}px {width_mode} {lang} .{fmt}\n"
            "This usually means a webfont archive such as .woff2 was downloaded. "
            "Update to v3 or pass --font-url pointing at the non-woff ttf/otf release asset."
        )
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, member_name, data = candidates[0]
    out_name = safe_file_name(member_name)
    if not out_name.lower().endswith(f".{fmt}"):
        out_name += f".{fmt}"
    out_path = cache_dir / out_name
    if force or not out_path.exists() or out_path.stat().st_size == 0:
        out_path.write_bytes(data)
    return out_path


def resolve_font_path(args: argparse.Namespace, repo: Path) -> FontSourceInfo:
    if args.font_path:
        font_path = Path(args.font_path).expanduser().resolve()
        if not font_path.exists():
            raise SystemExit(f"font file not found: {font_path}")
        return FontSourceInfo(
            font_path=font_path,
            release_tag="local",
            asset_name=font_path.name,
            asset_url="local",
            extracted_from="local",
            sha256=file_sha256(font_path),
        )

    cache_dir = Path(args.font_cache).expanduser()
    if not cache_dir.is_absolute():
        cache_dir = repo / cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)

    release_tag = "custom-url"
    asset_name = "custom-url"
    asset_url = args.font_url or ""

    if args.font_url:
        guessed_name = safe_file_name(args.font_url.rstrip("/").split("/")[-1])
        if not guessed_name:
            guessed_name = "fusion_pixel_font_asset"
        asset_path = cache_dir / guessed_name
        download_file(args.font_url, asset_path, force=args.force_download)
    else:
        release = github_release(args.font_release)
        release_tag = str(release.get("tag_name", args.font_release))
        asset = select_release_asset(
            release,
            args.fusion_pixel_size,
            args.fusion_width,
            args.fusion_lang,
            args.fusion_format,
        )
        asset_name = str(asset.get("name", "fusion_pixel_font_asset"))
        browser_url = str(asset.get("browser_download_url", ""))
        if not browser_url:
            raise SystemExit(f"Selected release asset has no browser_download_url: {asset_name}")
        asset_url = browser_url
        asset_path = cache_dir / release_tag / safe_file_name(asset_name)
        download_file(asset_url, asset_path, force=args.force_download)

    if has_font_suffix(asset_path.name):
        font_path = asset_path
        extracted_from = asset_path.name
    elif has_archive_suffix(asset_path.name):
        extract_dir = asset_path.parent / (asset_path.stem + "_extracted")
        font_path = extract_font_from_archive(
            asset_path,
            extract_dir,
            args.fusion_pixel_size,
            args.fusion_width,
            args.fusion_lang,
            args.fusion_format,
            args.force_download,
        )
        extracted_from = asset_path.name
    else:
        # Some URLs omit a helpful extension. Try zip first, then fail clearly.
        try:
            with zipfile.ZipFile(asset_path) as zf:
                pass
            extract_dir = asset_path.parent / (asset_path.name + "_extracted")
            font_path = extract_font_from_archive(
                asset_path,
                extract_dir,
                args.fusion_pixel_size,
                args.fusion_width,
                args.fusion_lang,
                args.fusion_format,
                args.force_download,
            )
            extracted_from = asset_path.name
        except zipfile.BadZipFile as exc:
            raise SystemExit(f"Downloaded asset is neither a font nor a supported archive: {asset_path}") from exc

    if not font_path.exists():
        raise SystemExit(f"resolved font path does not exist: {font_path}")
    return FontSourceInfo(
        font_path=font_path,
        release_tag=release_tag,
        asset_name=asset_name,
        asset_url=asset_url,
        extracted_from=extracted_from,
        sha256=file_sha256(font_path),
    )


def render_char_12x12(
    font: ImageFont.FreeTypeFont,
    ch: str,
    threshold: int,
    x_offset: int,
    y_offset: int,
    invert: bool,
) -> List[List[int]]:
    # Render larger than needed, then crop/center into a strict 12x12 body.
    canvas_size = 64
    img = Image.new("L", (canvas_size, canvas_size), 255 if not invert else 0)
    draw = ImageDraw.Draw(img)

    try:
        draw.textbbox((0, 0), ch, font=font, anchor="lt")
        draw.text((0, 0), ch, font=font, fill=0 if not invert else 255, anchor="lt")
    except TypeError:
        draw.textbbox((0, 0), ch, font=font)
        draw.text((0, 0), ch, font=font, fill=0 if not invert else 255)

    # Compute ink bounding box for robust placement.
    ink = Image.new("L", img.size, 0)
    pix = img.load()
    ink_pix = ink.load()
    for y in range(img.height):
        for x in range(img.width):
            val = pix[x, y]
            is_ink = val >= threshold if invert else val <= threshold
            if is_ink:
                ink_pix[x, y] = 255
    ink_bbox = ink.getbbox()
    if ink_bbox is None:
        return [[0 for _ in range(12)] for _ in range(12)]

    glyph_img = img.crop(ink_bbox)
    gw, gh = glyph_img.size
    out = [[0 for _ in range(12)] for _ in range(12)]

    # Center by default, with optional offsets for tuning.
    dst_x = (12 - gw) // 2 + x_offset
    dst_y = (12 - gh) // 2 + y_offset

    for sy in range(gh):
        for sx in range(gw):
            dx = dst_x + sx
            dy = dst_y + sy
            if not (0 <= dx < 12 and 0 <= dy < 12):
                continue
            val = glyph_img.getpixel((sx, sy))
            is_ink = val >= threshold if invert else val <= threshold
            if is_ink:
                out[dy][dx] = 1
    return out


def pack_raw4(rows_12x4: Sequence[Sequence[int]]) -> bytes:
    if len(rows_12x4) != 12 or any(len(row) != 4 for row in rows_12x4):
        raise ValueError("raw4 source must be 12 rows x 4 columns")
    data = bytearray()
    for y in range(0, 12, 2):
        high = 0
        low = 0
        for x in range(4):
            high = (high << 1) | (1 if rows_12x4[y][x] else 0)
            low = (low << 1) | (1 if rows_12x4[y + 1][x] else 0)
        data.append((high << 4) | low)
    return bytes(data)


def pack_glyph12(glyph: Sequence[Sequence[int]]) -> Tuple[bytes, bytes, bytes]:
    if len(glyph) != 12 or any(len(row) != 12 for row in glyph):
        raise ValueError("glyph must be 12x12")
    strips = []
    for strip_idx in range(3):
        x0 = strip_idx * 4
        rows = [[glyph[y][x0 + x] for x in range(4)] for y in range(12)]
        strips.append(pack_raw4(rows))
    return strips[0], strips[1], strips[2]


class DFSFontPatcher:
    def __init__(self, repo: Path, out_dfs: Path, in_place: bool):
        self.repo = repo
        self.src_dfs = repo / "src" / "dfs"
        self.out_dfs = self.src_dfs if in_place else out_dfs
        self.in_place = in_place
        self.cache: Dict[Path, bytearray] = {}
        self.touched: set[Path] = set()

    def _target_path(self, file_code: str, half: str) -> Path:
        return self.out_dfs / f"ChineseFonts_{file_code}_{half}.bin"

    def _source_path(self, file_code: str, half: str) -> Path:
        return self.src_dfs / f"ChineseFonts_{file_code}_{half}.bin"

    def prepare(self) -> None:
        if self.in_place:
            return
        self.out_dfs.mkdir(parents=True, exist_ok=True)
        for src in self.src_dfs.glob("ChineseFonts_*_[HL].bin"):
            shutil.copy2(src, self.out_dfs / src.name)

    def _load_target(self, file_code: str, half: str) -> bytearray:
        path = self._target_path(file_code, half)
        if path not in self.cache:
            if not path.exists():
                raise FileNotFoundError(path)
            self.cache[path] = bytearray(path.read_bytes())
        return self.cache[path]

    def patch_strip(self, hi: int, lo: int, raw6: bytes) -> None:
        base = hi & 0x3F
        if base >= len(FONTAB):
            raise ValueError(f"hi byte out of FontAB range: ${hi:02X}")
        file_code = FONTAB[base]
        if file_code == "FF":
            raise ValueError(f"hi byte maps to empty FontAB slot: ${hi:02X}")
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
            raise ValueError(
                f"font offset out of range: ChineseFonts_{file_code}_{half}.bin "
                f"hi=${hi:02X} lo=${lo:02X} offset={offset} size={len(data)}"
            )
        data[offset : offset + 6] = raw6
        self.touched.add(self._target_path(file_code, half))

    def patch_glyph(self, entry: CharmapEntry, glyph: Sequence[Sequence[int]]) -> None:
        base_hi = entry.hi & 0x3F
        raw0, raw1, raw2 = pack_glyph12(glyph)
        self.patch_strip(base_hi | 0x00, entry.lo, raw0)
        self.patch_strip(base_hi | 0x40, entry.lo, raw1)
        self.patch_strip(base_hi | 0x80, entry.lo, raw2)

    def save(self) -> None:
        for path, data in self.cache.items():
            path.write_bytes(bytes(data))


def draw_preview(glyphs: List[Tuple[str, CharmapEntry, List[List[int]]]], out_path: Path, cols: int, scale: int) -> None:
    cell_w = 16
    cell_h = 16
    top_pad = 4
    rows = (len(glyphs) + cols - 1) // cols
    img = Image.new("RGB", (cols * cell_w * scale, max(1, rows) * cell_h * scale), "white")
    draw = ImageDraw.Draw(img)
    for i, (_, _, glyph) in enumerate(glyphs):
        col = i % cols
        row = i // cols
        ox = col * cell_w * scale
        oy = row * cell_h * scale
        draw.rectangle([ox, oy, ox + cell_w * scale - 1, oy + cell_h * scale - 1], outline=(220, 220, 220))
        for y in range(12):
            for x in range(12):
                if glyph[y][x]:
                    x0 = ox + x * scale
                    y0 = oy + (top_pad + y) * scale
                    draw.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill="black")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


def write_report(
    path: Path,
    rows: List[Tuple[str, str, CharmapEntry]],
    alias_notes: List[str],
    touched: Iterable[Path],
    repo: Path,
    font_source: FontSourceInfo,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "DFS font build report",
        f"repo={repo}",
        f"glyphs={len(rows)}",
        "",
        "font source:",
        f"  path={font_source.font_path}",
        f"  release_tag={font_source.release_tag}",
        f"  asset_name={font_source.asset_name}",
        f"  asset_url={font_source.asset_url}",
        f"  extracted_from={font_source.extracted_from}",
        f"  sha256={font_source.sha256}",
        "",
        "glyphs:",
    ]
    for row in rows:
        if len(row) == 3:
            render_ch, lookup_ch, entry = row
            if render_ch == lookup_ch:
                lines.append(f"{render_ch}\t{entry.code_hex}\tline={entry.line_no}")
            else:
                lines.append(f"{render_ch}\tvia={lookup_ch}\t{entry.code_hex}\tline={entry.line_no}")
        elif len(row) == 2:
            # Backward compatibility for older callers.
            render_ch, entry = row
            lines.append(f"{render_ch}\t{entry.code_hex}\tline={entry.line_no}")
        else:
            raise ValueError(f"Unexpected report row shape: {row!r}")
    lines.append("")
    if alias_notes:
        lines.append("fallback aliases:")
        lines.extend(f"  {note}" for note in alias_notes)
        lines.append("")
    lines.append("touched font files:")
    for p in sorted(touched):
        try:
            lines.append(str(p.relative_to(repo)))
        except ValueError:
            lines.append(str(p))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Patch DFS Chinese font binaries from Fusion Pixel Font.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--repo", default=".", help="Repo root. Default: current directory.")
    parser.add_argument("--charmap", default="src/charmap.txt", help="charmap path, relative to repo unless absolute.")
    parser.add_argument("--font-path", help="Local TTF/OTF path. If omitted, Fusion Pixel Font is downloaded from GitHub Releases.")
    parser.add_argument("--font-release", default="latest", help="Fusion Pixel Font release tag, or 'latest'. Default: latest.")
    parser.add_argument("--font-url", help="Explicit font/archive URL override. Useful if GitHub asset names change.")
    parser.add_argument("--font-cache", default=".cache/fusion-pixel-font", help="Font download cache dir. Default: .cache/fusion-pixel-font")
    parser.add_argument("--fusion-pixel-size", type=int, default=12, help="Fusion Pixel release pixel size to download. Default: 12.")
    parser.add_argument("--fusion-width", default="monospaced", choices=["monospaced", "proportional"], help="Fusion Pixel width mode. Default: monospaced.")
    parser.add_argument("--fusion-lang", default="zh_hant", help="Fusion Pixel language-specific glyph version. Default: zh_hant.")
    parser.add_argument("--fusion-format", default="ttf", choices=["ttf", "otf"], help="Downloaded font format. Default: ttf.")
    parser.add_argument("--force-download", action="store_true", help="Re-download even if the cached asset already exists.")
    parser.add_argument("--list-font-assets", action="store_true", help="List Fusion Pixel Font release assets with selection scores, then exit.")
    parser.add_argument("--font-size", type=int, default=12, help="Pillow render font size. Default: 12.")
    parser.add_argument("--chars", help="Characters to patch.")
    parser.add_argument("--char-file", help="UTF-8 file containing characters to patch.")
    parser.add_argument("--alias-map", help="Optional TSV/text map for missing chars, e.g. 寶=宝 or 寶<TAB>宝.")
    parser.add_argument("--no-t2s-fallback", action="store_true", help="Disable automatic Traditional->Simplified slot fallback for chars missing from charmap.")
    parser.add_argument("--threshold", type=int, default=128, help="Ink threshold 0..255. Default: 128.")
    parser.add_argument("--x-offset", type=int, default=0, help="Extra horizontal placement offset. Default: 0.")
    parser.add_argument("--y-offset", type=int, default=0, help="Extra vertical placement offset. Default: 0.")
    parser.add_argument("--invert", action="store_true", help="Use for fonts/images with inverted coverage, rarely needed.")
    parser.add_argument("--out-dfs", default="reports/dfs_font_from_fusion", help="Output DFS dir unless --in-place.")
    parser.add_argument("--in-place", action="store_true", help="Patch src/dfs directly. Not recommended until preview is verified.")
    parser.add_argument("--preview", default="reports/dfs_font_from_fusion_preview.png", help="Preview PNG path.")
    parser.add_argument("--report", default="reports/dfs_font_from_fusion_report.txt", help="Report path.")
    parser.add_argument("--cols", type=int, default=16, help="Preview columns. Default: 16.")
    parser.add_argument("--scale", type=int, default=4, help="Preview scale. Default: 4.")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()

    if args.list_font_assets:
        release = github_release(args.font_release)
        print(f"release: {release.get('tag_name', args.font_release)}")
        for asset in release.get("assets", []):
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name", ""))
            score = score_font_name(name, args.fusion_pixel_size, args.fusion_width, args.fusion_lang, args.fusion_format, allow_archive=True)
            print(f"{score:5d}  {name}")
        return 0

    charmap_path = Path(args.charmap)
    if not charmap_path.is_absolute():
        charmap_path = repo / charmap_path

    font_source = resolve_font_path(args, repo)
    font_path = font_source.font_path

    entries = parse_charmap(charmap_path)
    chars = load_chars(args)
    alias_map = load_alias_map(args.alias_map)
    resolved_rows, missing, alias_notes = resolve_charmap_entries(
        chars,
        entries,
        alias_map,
        t2s_fallback=not args.no_t2s_fallback,
    )
    if missing:
        raise SystemExit(
            "Chars not found in charmap and no alias/t2s fallback was available: "
            + "".join(missing)
            + "\nHint: create an alias file with lines like 寶=宝, or install "
            + "opencc-python-reimplemented for broader t2s fallback."
        )

    font = ImageFont.truetype(str(font_path), args.font_size)
    out_dfs = Path(args.out_dfs)
    if not out_dfs.is_absolute():
        out_dfs = repo / out_dfs
    patcher = DFSFontPatcher(repo, out_dfs, args.in_place)
    patcher.prepare()

    rendered: List[Tuple[str, CharmapEntry, List[List[int]]]] = []
    report_rows: List[Tuple[str, str, CharmapEntry]] = []
    for render_ch, lookup_ch, entry in resolved_rows:
        glyph = render_char_12x12(font, render_ch, args.threshold, args.x_offset, args.y_offset, args.invert)
        patcher.patch_glyph(entry, glyph)
        rendered.append((render_ch, entry, glyph))
        report_rows.append((render_ch, lookup_ch, entry))
    patcher.save()

    preview = Path(args.preview)
    if not preview.is_absolute():
        preview = repo / preview
    draw_preview(rendered, preview, args.cols, args.scale)

    report = Path(args.report)
    if not report.is_absolute():
        report = repo / report
    write_report(report, report_rows, alias_notes, patcher.touched, repo, font_source)

    print(f"font: {font_path}")
    print(f"font sha256: {font_source.sha256}")
    if font_source.release_tag != "local":
        print(f"release: {font_source.release_tag}")
        print(f"asset: {font_source.asset_name}")
    if alias_notes:
        print("fallback aliases:")
        for note in alias_notes:
            print(f"  {note}")
    print(f"patched glyphs: {len(rendered)}")
    print(f"out dfs: {patcher.out_dfs}")
    print(f"preview: {preview}")
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
