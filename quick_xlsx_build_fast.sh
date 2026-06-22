#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./quick_xlsx_build_fast.sh [option] [target] [variant]
  ./quick_xlsx_build_fast.sh [option] [variant]
  YELLOW_VARIANT=jp ./quick_xlsx_build_fast.sh [option] [target]

Examples:
  ./quick_xlsx_build_fast.sh 1
  ./quick_xlsx_build_fast.sh 1 us
  ./quick_xlsx_build_fast.sh 1 jp
  ./quick_xlsx_build_fast.sh 2 pokeyellow.gbc sjp_col

Variants:
  us, yus, yeus, yellowus       -> buildYUS / YEUS / roms/yellowUS
  jp, sjp, yjp, yejp, yellowjp,
  sjp_col, chs_sjp_col          -> buildYJP / YEJP / roms/yellowJP
USAGE
}

is_variant_token() {
  case "${1,,}" in
    us|yus|yeus|yellowus|jp|sjp|yjp|yejp|yellowjp|sjp_col|chs_sjp_col)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

normalize_variant() {
  case "${1,,}" in
    us|yus|yeus|yellowus)
      build_dir="buildYUS"
      text_branch="YEUS"
      rom_dir="yellowUS"
      variant_name="US"
      ;;
    jp|sjp|yjp|yejp|yellowjp|sjp_col|chs_sjp_col)
      build_dir="buildYJP"
      text_branch="YEJP"
      rom_dir="yellowJP"
      variant_name="JP"
      ;;
    *)
      echo "ERROR: unknown variant '$1'" >&2
      usage >&2
      exit 1
      ;;
  esac
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

option="1"
target="pokeyellow.gbc"
variant="${YELLOW_VARIANT:-US}"

if [ "$#" -ge 1 ] && is_variant_token "$1"; then
  variant="$1"
  target="${2:-pokeyellow.gbc}"
else
  option="${1:-1}"
  if [ "$#" -ge 2 ] && is_variant_token "$2"; then
    variant="$2"
  else
    target="${2:-pokeyellow.gbc}"
    variant="${3:-${YELLOW_VARIANT:-US}}"
  fi
fi

if ! [[ "$option" =~ ^[0-9]+$ ]]; then
  echo "ERROR: option must be a number, got '$option'" >&2
  usage >&2
  exit 1
fi

normalize_variant "$variant"

repo="$(cd "$(dirname "$0")"; pwd)"

if [ ! -d "$repo/$build_dir" ]; then
  echo "ERROR: $build_dir not found. Run the matching ./_prepare.command once first." >&2
  exit 1
fi

if [ ! -d "$repo/$build_dir/xlsx" ]; then
  echo "ERROR: missing $build_dir/xlsx" >&2
  exit 1
fi

echo "variant: $variant_name ($build_dir / $text_branch)"
echo "[1/3] Copy xlsx into $build_dir..."
cp -f "$repo"/src/xlsx/*.xlsx "$repo/$build_dir/xlsx/"

echo "[2/3] Re-import xlsx text with $text_branch..."
cd "$repo/$build_dir"

python3 tools/_importText.py xlsx/outdoor.xlsx 5 "$text_branch" "$option"
python3 tools/_importText2.py xlsx/dex.xlsx 5 "$text_branch" "$option"
python3 tools/_importText.py xlsx/buildingsA.xlsx 5 "$text_branch" "$option"
python3 tools/_importText.py xlsx/buildingsB.xlsx 5 "$text_branch" "$option"
python3 tools/_importText.py xlsx/indoor.xlsx 5 "$text_branch" "$option"
python3 tools/_importText.py xlsx/routes.xlsx 5 "$text_branch" "$option"
python3 tools/_importText.py xlsx/core.xlsx 5 "$text_branch" "$option"
python3 tools/_importText.py xlsx/ratings.xlsx 5 "$text_branch" "$option"
python3 tools/_importDexEntry.py xlsx/dexEntry.xlsx 13 1 "$option" "$text_branch"
python3 tools/_importTextData.py xlsx/data.xlsx 1 "$text_branch" "$option"

echo "[3/3] Incremental make: $target"
if [ "$option" -eq 1 ]; then
  make "$target" CHAR_FLAGS=
else
  make "$target" RGBDS=rgbds-cn/ CHAR_FLAGS="-D RGBDS_WCHAR"
fi

mkdir -p "roms/$rom_dir"

if [ -f pokeyellow.gbc ]; then
  cp -f pokeyellow.gbc "roms/$rom_dir/pokeyellow.${option}.gbc"
fi

echo "done: $build_dir/roms/$rom_dir/pokeyellow.${option}.gbc"
