#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  ./quick_tsv_build_fast.sh [option] [target] [variant]
  ./quick_tsv_build_fast.sh [option] [variant]
  YELLOW_VARIANT=jp ./quick_tsv_build_fast.sh [option] [target]

Examples:
  ./quick_tsv_build_fast.sh 1
  ./quick_tsv_build_fast.sh 1 us
  ./quick_tsv_build_fast.sh 1 jp
  ./quick_tsv_build_fast.sh 2 pokeyellow.gbc sjp_col
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

repo="$(cd "$(dirname "$0")"; pwd)"

if [ ! -f "$repo/src/tools/xlsx_sheetdir.py" ]; then
  echo "ERROR: missing src/tools/xlsx_sheetdir.py" >&2
  exit 1
fi

if [ ! -d "$repo/src/xlsx_tsv" ]; then
  echo "ERROR: missing src/xlsx_tsv" >&2
  exit 1
fi

if [ ! -x "$repo/quick_xlsx_build_fast.sh" ]; then
  echo "ERROR: missing or non-executable quick_xlsx_build_fast.sh" >&2
  exit 1
fi

echo "[0/4] Rebuild src/xlsx/*.xlsx from src/xlsx_tsv..."
python3 "$repo/src/tools/xlsx_sheetdir.py" import-all \
  --repo "$repo" \
  --tsv-root src/xlsx_tsv \
  --out-dir src/xlsx \
  --template-dir src/xlsx \
  --xlsx-list src/xlsx/xlsxList.txt

echo "[1/4] Run incremental xlsx build..."
exec "$repo/quick_xlsx_build_fast.sh" "$option" "$target" "$variant"
