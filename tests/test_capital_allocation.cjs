const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const template = fs.readFileSync(path.join(root, "template.html"), "utf8");
const exportHelpers = source => source.slice(
  source.indexOf("    function excelColumnName("),
  source.indexOf("    function crc32(")
);
const allocLogic = source => source.slice(
  source.indexOf("    // capital-allocation-logic-start"),
  source.indexOf("    // capital-allocation-logic-end")
);
const context = vm.createContext({});
vm.runInContext(exportHelpers(template), context);
vm.runInContext(allocLogic(template), context);

test("capitalStageForPL maps PL ratio to the deepest rolling stage", () => {
  // vm 跨 realm 物件的原型不同，轉為本 realm 物件再比較
  const stageOf = pl => {
    const r = context.capitalStageForPL(pl);
    return { stage: r.stage, amount: r.amount, note: r.note };
  };
  assert.deepEqual(stageOf(0.299), { stage: 0, amount: 0, note: "" });
  assert.deepEqual(stageOf(-0.005), { stage: 0, amount: 0, note: "" });
  assert.deepEqual(stageOf(-0.05), { stage: 1, amount: 200000, note: "" });
  assert.deepEqual(stageOf(-0.057), { stage: 1, amount: 200000, note: "" });
  assert.deepEqual(stageOf(-0.098), { stage: 1, amount: 200000, note: "" });
  assert.deepEqual(stageOf(-0.10), { stage: 2, amount: 400000, note: "" });
  assert.deepEqual(stageOf(-0.118), { stage: 2, amount: 400000, note: "" });
  assert.deepEqual(stageOf(-0.163), { stage: 3, amount: 600000, note: "" });
  assert.deepEqual(stageOf(-0.198), { stage: 3, amount: 600000, note: "" });
  assert.deepEqual(stageOf(-0.20), { stage: 4, amount: 0, note: "停止加碼" });
  assert.deepEqual(stageOf(-1), { stage: 4, amount: 0, note: "停止加碼" });
  assert.deepEqual(stageOf(""), { stage: "", amount: 0, note: "" });
  assert.deepEqual(stageOf("abc"), { stage: "", amount: 0, note: "" });
});

test("executedStageFromCost infers executed stages from cumulative cost", () => {
  const executed = context.executedStageFromCost;
  assert.equal(executed(0), 0);
  assert.equal(executed(19823), 0);
  assert.equal(executed(200000), 0);
  assert.equal(executed(359999), 0);
  assert.equal(executed(360000), 1);
  assert.equal(executed(522678), 1);
  assert.equal(executed(719999), 1);
  assert.equal(executed(720000), 2);
  assert.equal(executed(838093), 2);
  assert.equal(executed(1259999), 2);
  assert.equal(executed(1260000), 3);
  assert.equal(executed(2402330), 3);
  assert.equal(executed(""), 0);
  assert.equal(executed("abc"), 0);
});

test("allocatableAmount tops up to the cumulative target capped by the limit", () => {
  const alloc = context.allocatableAmount;
  assert.equal(alloc(200000, 2, 2000000), 600000);
  assert.equal(alloc(522678, 3, 2000000), 877322);
  assert.equal(alloc(838093, 3, 2000000), 561907);
  assert.equal(alloc(1950000, 3, 2000000), 0);
  assert.equal(alloc(2402330, 3, 2000000), 0);
  assert.equal(alloc(838093, 3, 0), 561907);
  assert.equal(alloc(838093, 3, "not-a-number"), 561907);
  assert.equal(alloc(838093, 0, 2000000), 0);
  assert.equal(alloc(838093, 4, 2000000), 0);
});

test("holdingCodeFromName extracts the leading ticker from 商品名稱", () => {
  const codeOf = context.holdingCodeFromName;
  assert.equal(codeOf("00662 富邦NASDAQ"), "00662");
  assert.equal(codeOf("1101 台　泥"), "1101");
  assert.equal(codeOf("00687B 國泰20年美債"), "00687B");
  assert.equal(codeOf(""), "");
  assert.equal(codeOf(null), "");
});

test("plRatioFromCell parses decimal ratios and percent text", () => {
  const parse = context.plRatioFromCell;
  assert.equal(parse("-0.005"), -0.005);
  assert.equal(parse("-0.198"), -0.198);
  assert.equal(parse("-5%"), -0.05);
  assert.ok(Number.isNaN(parse("")));
  assert.ok(Number.isNaN(parse(null)));
});

const HOLDINGS = [
  ["項次", "商品名稱", "股數", "成本價", "現價", "投資成本", "損益", "損益率", "試算價"],
  ["1", "00662 富邦NASDAQ", "160", "123.88", "123.6", "19823", "-107", "-0.005", ""],
  ["7", "1101 台　泥", "26338", "31.82", "28.2", "838093", "-98842", "-0.118", ""],
  ["9", "1532 勤　美", "19000", "27.5", "22.15", "522678", "-103685", "-0.198", ""],
  ["10", "1602 太　電", "436", "0.52", "0", "226", "-226", "-1", ""],
  ["13", "2330 台積電", "874", "1950.07", "2415", "1704966", "395913", "0.232", ""],
  ["26", "5871 中租-KY", "14853", "132.04", "107", "1961324", "-379654", "-0.194", ""],
  ["小計", "", "", "", "", "36275351", "28583467", "0.788", ""],
];

function cellText(xml, reference) {
  const match = xml.match(new RegExp(`<c r="${reference}"[^>]*?(?:/>|>(.*?)</c>)`));
  return match ? (match[1] ?? "").replace(/<[^>]*>/g, "") : "";
}

test("buildCapitalAllocWorksheet appends KD值/階段/可投入資金 in J/K/L", () => {
  const kdLookup = code => (code === "2330" ? "K 25.5 / D 30.1" : "");
  const { worksheet, count } = context.buildCapitalAllocWorksheet(HOLDINGS, 2000000, kdLookup);
  assert.equal(count, 6);
  assert.ok(worksheet.includes("<sheetData>"));
  // Header row J1/K1/L1
  assert.equal(cellText(worksheet, "J1"), "KD值");
  assert.equal(cellText(worksheet, "K1"), "階段");
  assert.equal(cellText(worksheet, "L1"), "可投入資金");
  // Row 2: PL -0.5% → 階段 0，無可投入資金
  assert.equal(cellText(worksheet, "J2"), "");
  assert.equal(cellText(worksheet, "K2"), "0");
  assert.equal(cellText(worksheet, "L2"), "");
  // Row 3: PL -11.8% → 階段 2，成本 838,093 已達階段 2 → 已執行，可投入空白
  assert.equal(cellText(worksheet, "K3"), "2（已執行）");
  assert.equal(cellText(worksheet, "L3"), "");
  // Row 4: PL -19.8% → 階段 3，成本 522,678 僅達階段 1 → 補齊至 140 萬 = 877,322
  assert.equal(cellText(worksheet, "K4"), "3");
  assert.ok(worksheet.includes('<c r="L4" s="0"><v>877322</v></c>'));
  // Row 5: PL -100% → 階段 4 停止加碼，可投入空白
  assert.equal(cellText(worksheet, "K5"), "4（停止加碼）");
  assert.equal(cellText(worksheet, "L5"), "");
  // Row 6: 2330 有 KD，PL +23.2% → 階段 0
  assert.equal(cellText(worksheet, "J6"), "K 25.5 / D 30.1");
  assert.equal(cellText(worksheet, "K6"), "0");
  // Row 7: 成本 1,961,324 已達階段 3 → 已執行，可投入空白
  assert.equal(cellText(worksheet, "K7"), "3（已執行）");
  assert.equal(cellText(worksheet, "L7"), "");
  // 小計列保留原值，J/K/L 空白
  assert.equal(cellText(worksheet, "A8"), "小計");
  assert.equal(cellText(worksheet, "K8"), "");
});

test("buildCapitalAllocWorksheet falls back to 損益/投資成本 when 損益率 missing", () => {
  const rows = [
    ["項次", "商品名稱", "股數", "成本價", "現價", "投資成本", "損益", "損益率", "試算價"],
    ["1", "2330 台積電", "874", "1950.07", "2415", "1000000", "-120000", "", ""],
  ];
  const { worksheet } = context.buildCapitalAllocWorksheet(rows, 2000000, () => "");
  // 成本 1,000,000 已達階段 2 → 已執行，可投入空白
  assert.equal(cellText(worksheet, "K2"), "2（已執行）");
  assert.equal(cellText(worksheet, "L2"), "");
});

test("buildCapitalAllocWorksheet tops up skipped stages to the cumulative target", () => {
  const rows = [
    ["項次", "商品名稱", "股數", "成本價", "現價", "投資成本", "損益", "損益率", "試算價"],
    ["1", "2330 台積電", "100", "2000", "1760", "200000", "-24000", "-0.12", ""],
  ];
  const { worksheet } = context.buildCapitalAllocWorksheet(rows, 2000000, () => "");
  // 成本 20 萬僅初始，PL -12% 落階段 2 → 補齊至累計 80 萬 = 600,000
  assert.equal(cellText(worksheet, "K2"), "2");
  assert.ok(worksheet.includes('<c r="L2" s="0"><v>600000</v></c>'));
});

test("capitalAllocCellXml normalizes scientific-notation numbers", () => {
  assert.equal(context.capitalAllocCellXml("-5.0000000000000001E-3", "H2"), '<c r="H2" s="0"><v>-0.005</v></c>');
  assert.equal(context.capitalAllocCellXml("00662 富邦NASDAQ", "B2").includes("t=\"inlineStr\""), true);
  assert.equal(context.capitalAllocCellXml("", "I2"), '<c r="I2" s="0"/>');
});

test("buildCapitalAllocWorksheet writes the rolling-stage explanation in column N", () => {
  const { worksheet } = context.buildCapitalAllocWorksheet(HOLDINGS, 2000000, () => "");
  assert.equal(cellText(worksheet, "N1"), "階段說明");
  // Row 2: PL -0.5% → 階段 0
  assert.equal(cellText(worksheet, "N2"), "PL -0.5% 未達 -5%，階段 0 不加碼");
  // Row 3: 階段 2 已執行
  assert.equal(cellText(worksheet, "N3"), "PL -11.8% ≤ -10% → 階段 2，成本 838,093 已達累計目標 800,000（已執行）");
  // Row 4: 階段 3 未執行，補齊至 140 萬
  assert.equal(cellText(worksheet, "N4"), "PL -19.8% ≤ -15% → 階段 3，成本 522,678 未達目標，可投入 = 累計目標 1,400,000 − 成本 = 877,322");
  // Row 5: 停止加碼
  assert.equal(cellText(worksheet, "N5"), "PL -100.0% ≤ -20% 最大容許虧損，停止加碼");
  // 小計列 N 欄空白
  assert.equal(cellText(worksheet, "N8"), "");
});

test("capitalStageNote shows the limit as ceiling when it is below the cumulative target", () => {
  const alloc = context.allocatableAmount(200000, 2, 600000);
  assert.equal(alloc, 400000);
  const note = context.capitalStageNote(-0.12, 200000, 2, 0, alloc, 600000);
  assert.equal(note, "PL -12.0% ≤ -10% → 階段 2，成本 200,000 未達目標，可投入 = 上限 600,000 − 成本 = 400,000");
});

test("buildCapitalAllocWorksheet rejects files without 商品名稱 header", () => {
  assert.throws(
    () => context.buildCapitalAllocWorksheet([["A", "B"], ["1", "2"]], 2000000, () => ""),
    /商品名稱/
  );
});
