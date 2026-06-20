#!/usr/bin/env bash
set -euo pipefail

option="${1:-1}"
target="${2:-pokeyellow.gbc}"
repo="$(cd "$(dirname "$0")"; pwd)"

if [ ! -d "$repo/buildYUS" ]; then
  echo "ERROR: buildYUS not found. Run ./_prepare.command once first."
  exit 1
fi

echo "[1/3] Copy xlsx into buildYUS..."
cp -f "$repo"/src/xlsx/*.xlsx "$repo"/buildYUS/xlsx/

echo "[2/3] Re-import xlsx text..."
cd "$repo/buildYUS"

python3 tools/_importText.py xlsx/outdoor.xlsx 5 YEUS "$option"
python3 tools/_importText2.py xlsx/dex.xlsx 5 YEUS "$option"
python3 tools/_importText.py xlsx/buildingsA.xlsx 5 YEUS "$option"
python3 tools/_importText.py xlsx/buildingsB.xlsx 5 YEUS "$option"
python3 tools/_importText.py xlsx/indoor.xlsx 5 YEUS "$option"
python3 tools/_importText.py xlsx/routes.xlsx 5 YEUS "$option"
python3 tools/_importText.py xlsx/core.xlsx 5 YEUS "$option"
python3 tools/_importText.py xlsx/ratings.xlsx 5 YEUS "$option"
python3 tools/_importDexEntry.py xlsx/dexEntry.xlsx 13 1 "$option" YEUS
python3 tools/_importTextData.py xlsx/data.xlsx 1 YEUS "$option"

echo "[3/3] Incremental make: $target"
if [ "$option" -eq 1 ]; then
  make "$target" CHAR_FLAGS=
else
  make "$target" RGBDS=rgbds-cn/ CHAR_FLAGS="-D RGBDS_WCHAR"
fi

mkdir -p roms/yellowUS

if [ -f pokeyellow.gbc ]; then
  cp -f pokeyellow.gbc "roms/yellowUS/pokeyellow.${option}.gbc"
fi

echo "done: buildYUS/roms/yellowUS/pokeyellow.${option}.gbc"
