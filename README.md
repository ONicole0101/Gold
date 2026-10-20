# StockHolding-report
TW Stcok Holding

## Excel 匯出

報表的「匯出」按鈕會將目前頁籤、篩選及排序後的表格輸出為 Excel，
保留儲存格內的換行與文字顏色。「價格LH3天趨勢 T/T-1/T-2」保留
趨勢摘要及三天資料共四行，每天的日期、低價與高價維持在同一行。
此修正已同步至 `index.html`；既有日期命名的歷史報表需重新產生才會套用。

離線匯出回歸測試（需 Node.js，無額外套件）：

```powershell
node --test tests\test_excel_export.cjs
```

## 買(+)分頁

- 「符合自選分類」已移至基本面分頁，位於代碼／名稱後方；手機卡片及 Excel 匯出同步調整。
- 買(+)與賣(-)分頁不再顯示總分、條件分數或計分明細，並移除相應計算與參數設定；
  各條件欄位改為直接顯示價格、KD、支撐／壓力、法人與指標數值。
- 買(+)與賣(-)分頁均顯示日Bias5、日Bias20、日Bias60（120 日 LH），沿用技術面的
  計算、格式及呈現方式；桌面表格、手機卡片及 Excel 匯出同步提供。

離線買賣欄位回歸測試：

```powershell
node --test tests\test_buy_tab.cjs tests\test_excel_export.cjs
```

## 基本面資料增補

`generate_static_csv.py` 以 `AllStatic.csv` 為基底增量更新：

- `ok` / `partial_ok` 資料預設每 24 小時重新下載；成功回覆無資料的
  `no_data` 來源也會在下次週期重查，不會永久略過。
- `eps_Y` 是當年度已公告各季 EPS 合計，`eps_Y_quarters` 是已取得季數；
  來源標記為 `ok` 但缺少必要欄位（包括 EPS 季數）時，仍會重新補齊。
  `eps_yoy` 等衍生數值可能因歷史資料不足而留空，不會因此無限重試。
- HTTP/API 錯誤、額度限制、逾時及無效回覆不視為 `no_data`。更新失敗時，
  保留該來源上次成功的數值，並記錄 `error` / `api_limited`，數值不代表本次更新成功。
- 未到更新週期且資料完整時直接重用，不額外下載 EPS；`--max-rows` 限制本次實際更新股票數。
- `--retry-errors` 重試錯誤資料；排程與一鍵流程均已啟用。
  `--retry-no-data` 提前重查無資料來源，`--force` 忽略更新條件。
  `--refresh-hours 0` 停用週期刷新，但仍會增補不完整資料。
  `--repair-only` 僅修正狀態，不下載資料。

可指定股票驗證更新，未指定的既有資料會保留：

```powershell
python generate_static_csv.py --stock-id 2330 --force
```

季末不表示所有季報已可下載；季數取決於公司申報與 FinMind 收錄進度，
不能僅以當前月份推定應有三季。`static_updated_at` 是更新嘗試時間，
`ok` 表示必要欄位齊全，不保證來源已收錄最新一季。

### 營收、三率與 ROE 的期別及口徑

- 月營收優先使用 FinMind 的 `revenue_year` / `revenue_month` 判定所屬月份，
  不將公告月份當作營收月份；同月重複資料採最新修訂。
  MoM、QoQ、YoY 分別比較前 1、3、12 個月，缺少對應月份時留空，
  不拿其他月份代替。這裡的 QoQ 沿用單月對三個月前的比較，並非季度營收合計。
- 三率以金額科目計算的百分點優先，缺少的季度才以同季比率科目補齊；
  不依數值大小猜測單位，避免將 0.5% 錯放大成 50%。
  季增、年增差比較前一季及去年同季，缺季不使用其他期別。
  最新財報季缺少某一率時，舊值仍標記 `is_prev`，來源標記 `incomplete` 待補。
- ROE 優先使用母公司業主淨利及母公司業主權益；無此組合時，
  才使用整體淨利及整體權益，不交叉混用。
  TTM 必須包含截至最新損益表季度的連續四季，
  期初、期末權益須存在於正確季度；缺資料時留空，不挪用較舊權益。
- 非空回覆若缺少必要欄位或含無效日期、數值，會回報錯誤並保留既有資料，
  不再當成成功下載但無資料。

離線回歸測試（不呼叫外部 API、不修改正式 CSV）：

```powershell
python -m unittest discover -s tests -p "test_fundamental_refresh.py"
```
