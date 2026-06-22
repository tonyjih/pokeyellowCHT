# XLSX / TSV 文本工作流程

本專案的文字原始資料目前放在 `src/xlsx/*.xlsx`。為了讓 Git diff、文字 review、以及 ChatGPT 協作更方便，可以把每個 `.xlsx` 拆成一個資料夾，每個 sheet 對應一個 `.tsv` 檔。

建議工作模式是：

```text
src/xlsx_tsv/**/*.tsv  作為主要文本編輯來源
src/xlsx/*.xlsx        作為 Excel 檢視與原 build tools 使用的中間檔
```

---

## 0. 工具位置

工具放在：

```bash
src/tools/xlsx_sheetdir.py
```

確認工具可用：

```bash
python3 src/tools/xlsx_sheetdir.py --version
```

---

## 1. 將 XLSX 轉成 TSV

### 轉換所有 xlsxList.txt 裡列出的 workbook

```bash
cd /mnt/d/pokeyellowCHT

python3 src/tools/xlsx_sheetdir.py export-all \
  --repo . \
  --xlsx-list src/xlsx/xlsxList.txt \
  --out-root src/xlsx_tsv \
  --format tsv \
  --overwrite
```

轉換後會產生類似：

```text
src/xlsx_tsv/core/
  workbook.json
  000__Main.tsv
  001__SomeSheet.tsv

src/xlsx_tsv/data/
  workbook.json
  000__Items.tsv
  001__Moves.tsv
  ...
```

其中：

```text
workbook.json
```

用來記錄原本 workbook 的 sheet 順序、sheet 名稱、欄列尺寸與 cell 型別。一般編輯文本時不要手動改它。

---

### 只轉單一 workbook

例如只轉 `core.xlsx`：

```bash
python3 src/tools/xlsx_sheetdir.py export \
  --repo . \
  --xlsx src/xlsx/core.xlsx \
  --out-root src/xlsx_tsv \
  --format tsv \
  --overwrite
```

輸出會在：

```text
src/xlsx_tsv/core/
```

---

## 2. 編輯 TSV

之後可以直接編輯：

```text
src/xlsx_tsv/<workbook 名稱>/<sheet>.tsv
```

例如：

```text
src/xlsx_tsv/core/000__Main.tsv
src/xlsx_tsv/data/000__Items.tsv
src/xlsx_tsv/data/001__Moves.tsv
```

TSV 的好處是：

```text
1. Git diff 比 xlsx 乾淨很多
2. 可以直接貼一小段給 ChatGPT 討論
3. 不容易因為 Excel 二進位格式產生大量無意義差異
```

注意：
TSV 是 tab 分隔格式，編輯時不要把 tab 轉成空白。

---

## 3. 將 TSV 轉回 XLSX

### 將所有 TSV workbook 組回 xlsx

```bash
cd /mnt/d/pokeyellowCHT

python3 src/tools/xlsx_sheetdir.py import-all \
  --repo . \
  --tsv-root src/xlsx_tsv \
  --out-dir src/xlsx \
  --template-dir src/xlsx \
  --xlsx-list src/xlsx/xlsxList.txt
```

這會根據 `src/xlsx_tsv/` 的內容重建：

```text
src/xlsx/*.xlsx
```

`--template-dir src/xlsx` 會讓工具盡量保留原本 `.xlsx` 的格式、欄寬、sheet 設定。
實際文字內容則以 TSV 為準。

---

### 只組回單一 workbook

例如只把 `src/xlsx_tsv/core/` 組回 `src/xlsx/core.xlsx`：

```bash
python3 src/tools/xlsx_sheetdir.py import \
  --repo . \
  --book-dir src/xlsx_tsv/core \
  --output src/xlsx/core.xlsx \
  --template src/xlsx/core.xlsx
```

---

## 4. 從 TSV 修改後快速 build

一般文本測試流程：

```bash
cd /mnt/d/pokeyellowCHT

# 1. TSV 轉回 xlsx
python3 src/tools/xlsx_sheetdir.py import-all \
  --repo . \
  --tsv-root src/xlsx_tsv \
  --out-dir src/xlsx \
  --template-dir src/xlsx \
  --xlsx-list src/xlsx/xlsxList.txt

# 2. 快速重讀 xlsx 並 incremental build
./quick_xlsx_build_fast.sh 1
```

如果已經建立 `quick_tsv_build_fast.sh`，可以直接跑：

```bash
./quick_tsv_build_fast.sh 1
```

這會自動執行：

```text
TSV → XLSX → 重新 import xlsx 文字 → incremental make
```

---

## 5. 日常建議流程

### 修改文本

```bash
vim src/xlsx_tsv/core/000__Main.tsv
```

### Build 測試

```bash
./quick_tsv_build_fast.sh 1
```

### 檢查錯誤

```bash
grep -nE "error:|Assertion failed|Unmapped character|Unknown symbol|No rule|overflow" \
  reports/quick_tsv_build_fast.log || echo "OK: no fatal build errors"
```

---

## 6. Git 版本控制建議

建議主要追蹤：

```bash
git add src/xlsx_tsv
git add src/tools/xlsx_sheetdir.py
git add quick_tsv_build_fast.sh
git add quick_xlsx_build_fast.sh
```

短期內可以同時保留 `src/xlsx/*.xlsx`，因為原本 build tools 還是吃 xlsx。

等 TSV round-trip 穩定後，可以考慮把 `.xlsx` 視為 generated artifact，只在需要 Excel 檢視或 build 前由 TSV 產生。

---

## 7. 注意事項

1. 執行 TSV → XLSX 前，請先關閉 Excel，避免 `.xlsx` 被鎖住。
2. 一般文本修改只改 `.tsv`，不要直接改 `.xlsx`，避免兩邊不同步。
3. `workbook.json` 用來維持 sheet 順序與 metadata，通常不要手動修改。
4. 如果新增或刪除 sheet，要同時確認 `workbook.json` 是否符合預期。
5. 如果只是改既有文字，不需要重建 Big5 字庫；如果新增目前 charmap 沒有的字，才需要重新跑字庫流程。
