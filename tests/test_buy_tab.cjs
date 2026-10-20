const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const sources = ["template.html", "index.html"].map(name =>
  fs.readFileSync(path.join(root, name), "utf8"));
const section = (source, start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first, `Missing section: ${start}`);
  return source.slice(first, last);
};

for (const [index, source] of sources.entries()) {
  const name = index === 0 ? "template" : "report";
  test(`${name}: buy columns follow KD and match technical definitions`, () => {
    const context = vm.createContext({});
    vm.runInContext(section(source, "    const TAB_CONFIG =", "    function isNil("), context);
    const config = vm.runInContext("TAB_CONFIG", context);
    const keys = Array.from(config.buy.columns, column => column.key);
    const kdIndex = keys.indexOf("buy_kd_oversold");
    assert.deepEqual(keys.slice(kdIndex + 1, kdIndex + 5),
      ["bb_pct", "macd_hist", "bias5", "bias20"]);
    assert.deepEqual(Array.from(config.fundamental.columns.slice(0, 3), column => column.key),
      ["code", "custom_categories", "price"]);
    assert.ok(!keys.some(key => key.includes("categories")));
    assert.ok(!keys.some(key => key.includes("divergence")));
    for (const key of ["bb_pct", "macd_hist", "bias5", "bias20"]) {
      assert.deepEqual(
        config.buy.columns.find(column => column.key === key),
        config.technical.columns.find(column => column.key === key)
      );
    }
    assert.ok(config.buy.columns.find(column => column.key === "buy_score").tooltip.includes("10.5"));
    assert.equal(config.sell.columns.filter(column => column.key.includes("divergence")).length, 2);
  });

  test(`${name}: buy scoring ignores bullish flags and never calculates divergence`, () => {
    const context = vm.createContext({
      buySignalParams: () => ({ lowPricePct: 10, highTurnoverPct: 3, supportPct: 10, kdOversold: 30 }),
      priceLHValue: (stock, day) => [100, 110, 120][day],
      kdValue: (stock, day) => [10, 20][day],
      dayMetricValue: () => 4,
      pickFirst: (...values) => values.find(value => value != null),
      isNil: value => value == null,
      buySignalGrossBonus: () => ({ value: 30, qualified: true }),
      buySignalPerBonus: () => ({ qualified: true }),
      buySignalTrend: () => true,
      stockCustomCategories: () => [],
      signalDivergence: () => { throw new Error("Buy must not calculate divergence"); },
    });
    vm.runInContext(section(source, "    function calculateBuySignal(", "    function buySignalResult("), context);
    const stock = { price: 100, support_distance_pct: 5, institutional_total_net: 1 };
    const plain = context.calculateBuySignal(stock);
    const flagged = context.calculateBuySignal({
      ...stock, kd_bullish_divergence: true, rsi_bullish_divergence: true,
    });
    assert.equal(plain.baseScore, 5);
    assert.equal(plain.bonusScore, 5.5);
    assert.equal(plain.score, 10.5);
    assert.deepEqual(plain, flagged);
    assert.equal(Object.keys(plain.checks).length, 5);
    assert.ok(!source.includes("buy_kd_divergence"));
    assert.ok(!source.includes("buy_rsi_divergence"));
  });

  test(`${name}: buy uses technical renderers for the four reference metrics`, () => {
    const context = vm.createContext({
      currentTab: "buy",
      renderBBTechnicalContent: () => "bb-three-days",
      renderMacdTechnicalContent: () => "macd-three-days",
      renderBiasTechnicalContent: (stock, key) => `${key}-range`,
    });

    vm.runInContext(section(source, "    function renderCell(", "    function renderHead("), context);
    for (const [key, expected] of Object.entries({
      bb_pct: "bb-three-days", macd_hist: "macd-three-days",
      bias5: "bias5-range", bias20: "bias20-range",
    })) {
      assert.equal(context.renderCell({}, key), `<td>${expected}</td>`);
    }
  });

  test(`${name}: fundamental categories render independently of buy scoring`, () => {
    const context = vm.createContext({
      stockCustomCategories: stock => stock.custom_category_list ?? [],
      escapeHtml: value => value.replace(/&/g, "&amp;").replace(/</g, "&lt;"),
    });
    vm.runInContext(section(source, "    function renderCell(", "    function renderHead("), context);
    const html = context.renderCell({ custom_category_list: ["AI", "<A&B"] }, "custom_categories");
    assert.ok(html.includes("AI、&lt;A&amp;B"));
    assert.ok(context.renderCell({}, "custom_categories").includes('class="category-list-text">-</div>'));
    const fundamentalGrid = section(source, "      const fundamentalGrid =", "      const buyResult =");
    const buyGrid = section(source, "      const buyGrid =", "      const sellResult =");
    assert.ok(fundamentalGrid.includes("符合自選分類"));
    assert.ok(!buyGrid.includes("符合自選分類"));
    assert.ok(!source.includes("buy_categories"));
  });
}

test("report and template keep buy calculation and rendering in sync", () => {
  for (const [start, end] of [
    ["    function calculateBuySignal(", "    function buySignalResult("],
    ["    function renderBuyScoreDetail(", "    function renderSellScoreDetail("],
    ["    function renderBuySignalCell(", "    function "],
  ]) {
    assert.equal(section(sources[0], start, end), section(sources[1], start, end));
  }
});
