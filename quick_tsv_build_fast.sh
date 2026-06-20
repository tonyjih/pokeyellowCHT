#!/usr/bin/env bash
set -euo pipefail

option="${1:-1}"
target="${2:-pokeyellow.gbc}"
repo="$(cd "$(dirname "$0")"; pwd)"

if [ ! -f "$repo/src/tools/xlsx_sheetdir.py" ]; then
  echo "ERROR: missing src/tools/xlsx_sheetdir.py"
  exit 1
fi

if [ ! -d "$repo/src/xlsx_tsv" ]; then
  echo "ERROR: missing src/xlsx_tsv"
  exit 1
fi

if [ ! -x "$repo/quick_xlsx_build_fast.sh" ]; then
  echo "ERROR: missing or non-executable quick_xlsx_build_fast.sh"
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
exec "$repo/quick_xlsx_build_fast.sh" "$option" "$target"
