const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const pages = ["template.html", "index.html"].map(name => ({
  name,
  source: fs.readFileSync(path.join(root, name), "utf8"),
}));
const section = (source, start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first, `Missing section: ${start}`);
  return source.slice(first, last);
};

for (const { name, source } of pages) {
  test(`${name}: buy and sell tabs have no score or custom-category columns`, () => {
    const context = vm.createContext({});
    vm.runInContext(section(source, "    const TAB_CONFIG =", "    function isNil("), context);
    const config = vm.runInContext("TAB_CONFIG", context);
    const buyKeys = Array.from(config.buy.columns, column => column.key);
    const sellKeys = Array.from(config.sell.columns, column => column.key);
    assert.ok(!buyKeys.some(key => /score|categor/.test(key)));
    assert.ok(!sellKeys.some(key => /score|categor/.test(key)));
    for (const key of ["bias5", "bias20", "bias60"]) {
      assert.ok(buyKeys.includes(key));
      assert.ok(sellKeys.includes(key));
      assert.ok(config.technical.columns.some(column => column.key === key));
    }
    assert.ok(config.fundamental.columns.some(column => column.key === "custom_categories"));
    assert.match(config.buy.note, /不計分/);
    assert.match(config.sell.note, /不計分/);
  });

  test(`${name}: buy and sell tabs render metric values without scoring logic`, () => {
    assert.doesNotMatch(source, /buySignal|sellSignal|buy_score|sell_score|計分表|買\\(\\+\\)分數|賣\\(-\\)分數/);
    assert.doesNotMatch(source, /buyLowPricePct|buyHighTurnoverPct|buySupportPct|buyKdOversold/);
    const buyRenderer = section(source, "    function renderBuySignalCell(", "    function renderPriceLHTrendBlock(");
    const sellRenderer = section(source, "    function renderSellSignalCell(", "    function fibonacciLevels(");
    assert.doesNotMatch(buyRenderer, /score|qualified|\\+1/);
    assert.doesNotMatch(sellRenderer, /score|qualified|\\+1/);

    const mobileSignalGrids = section(source, "      const buyGrid = `", "      const cbGrid = `");
    assert.doesNotMatch(mobileSignalGrids, /分數|計分表|符合自選分類/);
    for (const key of ["bias5", "bias20", "bias60"]) {
      assert.ok(mobileSignalGrids.includes(`renderBiasTechnicalContent(s, "${key}")`));
    }
  });

  test(`${name}: buy and sell Bias columns use the technical renderer`, () => {
    const context = vm.createContext({
      currentTab: "buy",
      renderBiasTechnicalContent: (stock, key) => `${key}-range`,
    });
    vm.runInContext(section(source, "    function renderCell(", "    function renderHead("), context);
    for (const key of ["bias5", "bias20", "bias60"]) {
      assert.equal(context.renderCell({}, key), `<td>${key}-range</td>`);
    }
    context.currentTab = "sell";
    for (const key of ["bias5", "bias20", "bias60"]) {
      assert.equal(context.renderCell({}, key), `<td>${key}-range</td>`);
    }
  });

  test(`${name}: buy and sell price LH and KD values use technical color classes`, () => {
    const context = vm.createContext({
      chipValueClass: (current, previous) => current > previous ? "up" : current < previous ? "dn" : "",
      dayDateLabel: (_stock, idx) => `T-${idx}`,
      dayDateMMDD: (_stock, idx) => `date-${idx}`,
      escapeHtml: value => value,
      fmt: value => String(value),
      kdValue: (_stock, idx) => [60, 50, 55, 40][idx],
      pickFirst: (...values) => values.find(value => value != null),
      priceLHValue: (_stock, idx, kind) => {
        const values = kind === "low" ? [10, 9, 8, 7] : [12, 13, 11, 10];
        return values[idx];
      },
    });
    vm.runInContext(
      section(source, "    function renderPriceLHLine(", "    function renderPriceLHTrendCell("),
      context
    );
    vm.runInContext(section(source, "    function buyValueText(", "    function renderPriceLHTrendBlock("), context);

    for (const [tab, priceKey, kdKey] of [
      ["Buy", "buy_price_lh", "buy_kd_oversold"],
      ["Sell", "sell_price_lh", "sell_kd_overbought"],
    ]) {
      const priceHtml = context[`render${tab}SignalCell`]({}, priceKey);
      assert.ok(priceHtml.includes('class="up"> 10'));
      assert.ok(priceHtml.includes('class="dn">/ 12'));

      const kdHtml = context[`render${tab}SignalCell`]({}, kdKey);
      assert.ok(kdHtml.includes('class="table-lv2 up">T-0：K 60'));
      assert.ok(kdHtml.includes('class="table-lv2 dn">T-1：K 50'));
      assert.ok(kdHtml.includes('class="table-lv2 up">T-2：K 55'));
    }
  });

  test(`${name}: buy and sell PER cells show 120-day low and high without a label`, () => {
    const context = vm.createContext({
      fmt: value => String(value),
      pickFirst: (...values) => values.find(value => value != null),
    });
    vm.runInContext(section(source, "    function buyValueText(", "    function renderPriceLHTrendBlock("), context);
    for (const [tab, key] of [["Buy", "buy_per"], ["Sell", "sell_per"]]) {
      const html = context[`render${tab}SignalCell`]({ per_120d_low: 8, per_120d_high: 12 }, key);
      assert.ok(html.includes("<div class=\"table-lv2\">8 / 12</div>"));
      assert.doesNotMatch(html, /120日區間/);
    }
  });

  test(`${name}: self-selected categories remain in fundamental only`, () => {
    const context = vm.createContext({
      stockCustomCategories: stock => stock.custom_category_list ?? [],
      escapeHtml: value => value.replace(/&/g, "&amp;").replace(/</g, "&lt;"),
    });
    vm.runInContext(section(source, "    function renderCell(", "    function renderHead("), context);
    const html = context.renderCell({ custom_category_list: ["AI", "<A&B"] }, "custom_categories");
    assert.ok(html.includes("AI、&lt;A&amp;B"));
    const fundamentalGrid = section(source, "      const fundamentalGrid =", "      const buyGrid =");
    assert.ok(fundamentalGrid.includes("符合自選分類"));
  });
}

test("generated report and template stay synchronized for buy/sell UI", () => {
  const [template, report] = pages.map(page => page.source);
  for (const [start, end] of [
    ["      buy: {", "      cb: {"],
    ["    function buyValueText(", "    function fibonacciLevels("],
    ["    function renderBuySignalCell(", "    function renderPriceLHTrendBlock("],
    ["      const buyGrid = `", "      const cbGrid = `"],
  ]) {
    assert.equal(section(template, start, end), section(report, start, end));
  }
});
