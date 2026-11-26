const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

for (const name of ["template.html", "index.html"]) {
  const source = fs.readFileSync(path.join(__dirname, "..", name), "utf8");
  const section = (start, end) => {
    const first = source.indexOf(start);
    const last = source.indexOf(end, first + start.length);
    assert.ok(first >= 0 && last > first, `Missing ${start} in ${name}`);
    return source.slice(first, last);
  };
  const context = vm.createContext({ currentTab: "fundamental" });
  for (const functionName of ["isNil", "fmt", "calculatePeg", "pegMissingReason", "renderPegContent", "renderCell", "getSortValue"]) {
    vm.runInContext(section(`    function ${functionName}(`, "\n    function "), context);
  }

  if (name === "template.html") test("template: stale daily PER dates appear in fundamental, buy and sell views", () => {
    const source = fs.readFileSync(path.join(__dirname, "..", "template.html"), "utf8");
    const fn = name => {
      const start = source.indexOf(`    function ${name}(`);
      const end = source.indexOf("\n    function ", start + 1);
      assert.ok(start >= 0 && end > start);
      return source.slice(start, end);
    };
    const context = vm.createContext({ currentTab: "fundamental" });
    for (const name of ["isNil", "fmt", "signedFmt", "pickFirst", "perDateNote", "renderPerRangeMetric",
      "buyValueText", "renderBuySignalCell", "renderSellSignalCell", "renderCell"]) {
      vm.runInContext(fn(name), context);
    }
    const stock = {
      per_latest: 17.93, per_latest_date: "2026-11-25",
      price_date_t0: "2026-11-26", per_120d_low: 17.46, per_120d_high: 39.44,
    };
    for (const html of [
      context.renderCell(stock, "per_latest"),
      context.renderBuySignalCell(stock, "buy_per"),
      context.renderSellSignalCell(stock, "sell_per"),
      context.renderPerRangeMetric(stock.per_latest, stock.per_120d_low, stock.per_120d_high, null, context.perDateNote(stock)),
    ]) {
      assert.match(html, /17\.93（2026-11-25）/);
    }
    assert.equal(context.perDateNote({ ...stock, per_latest_date: "2026-11-26" }), "");
    assert.equal(context.perDateNote({ ...stock, price_date_t0: "2026-11-25", per_latest_is_prev: true }), "（2026-11-25）");
    assert.equal(context.perDateNote({ ...stock, per_latest_date: null }), "（資料日期未知）");
    assert.equal(context.perDateNote({ ...stock, per_latest: null }), "");
    assert.equal(context.perDateNote({ ...stock, price_date_t0: null }), "（2026-11-25）");
  });
  vm.runInContext(section("    const TAB_CONFIG =", "    function isNil("), context);

  test(`${name}: fundamental PER and PEG follow price on desktop and mobile`, () => {
    const keys = Array.from(vm.runInContext("TAB_CONFIG.fundamental.columns", context), column => column.key);
    const price = keys.indexOf("price");
    assert.deepEqual(keys.slice(price, price + 3), ["price", "per_latest", "peg"]);
    assert.equal(keys.filter(key => key === "per_latest").length, 1);
    const grid = section("      const fundamentalGrid = `", "      const buyGrid = `");
    assert.ok(grid.indexOf("renderPerRangeMetric") < grid.indexOf("renderPegContent"));
    assert.ok(grid.indexOf("renderPegContent") < grid.indexOf("符合自選分類"));
  });

  test(`${name}: historical and forward PEG use distinct growth rates`, () => {
    const stock = { per_latest: 30, per_ttm: 40, eps_5y_cagr: 20, eps_forward_growth: 10, eps_yoy: 100 };
    const html = context.renderCell(stock, "peg");
    assert.match(html, /日 1\.50/);
    assert.match(html, /TTM 4/);
    assert.equal(context.getSortValue(stock, "peg"), 1.5);
  });

  test(`${name}: invalid or unavailable inputs do not produce PEG`, () => {
    for (const growth of [null, "", 0, -5, NaN, Infinity, "invalid"]) {
      const stock = { per_latest: 30, per_ttm: 40, eps_5y_cagr: growth, eps_forward_growth: growth };
      const html = context.renderCell(stock, "peg");
      assert.match(html, /日 -/);
      assert.match(html, /TTM -/);
      assert.equal(context.getSortValue(stock, "peg"), -999999);
    }
    for (const per of [null, "", 0, -1, NaN, Infinity, "invalid"]) {
      assert.equal(context.calculatePeg(per, 20), null);
    }
    const partial = context.renderCell({ per_latest: 30, per_ttm: null, eps_5y_cagr: 20, eps_forward_growth: 10 }, "peg");
    assert.match(partial, /日 1\.50/);
    assert.match(partial, /TTM -/);
  });

  test(`${name}: unavailable PEG values explain why without changing valid values`, () => {
    const html = context.renderCell({
      per_latest: 20, eps_5y_cagr: null, per_ttm: 10, eps_forward_growth: -5,
    }, "peg");
    assert.match(html, /日 -/);
    assert.match(html, /五年資料未取得或不足／EPS非正/);
    assert.match(html, /TTM -/);
    assert.match(html, /成長率非正/);
    const missing = context.renderCell({}, "peg");
    assert.match(missing, /PER缺資料、五年資料未取得或不足／EPS非正/);
    assert.match(missing, /PER缺資料、八季資料未取得或不足／EPS非正/);
    const valid = context.renderCell({
      per_latest: 20, eps_5y_cagr: 10, per_ttm: 10, eps_forward_growth: 20,
    }, "peg");
    assert.match(valid, /日 2/);
    assert.match(valid, /TTM 0\.50/);
    assert.doesNotMatch(valid, /table-lv3/);
  });

  test(`${name}: PEG tooltip summarizes valuation ranges`, () => {
    const column = vm.runInContext("TAB_CONFIG.fundamental.columns.find(c => c.key === 'peg')", context);
    for (const text of ["＜1", "＜0.75", "約1", "＞1", "1.2～1.5"]) {
      assert.ok(column.tooltip.includes(text), text);
    }
    assert.match(column.tooltip, /並非買賣保證/);
  });
}
