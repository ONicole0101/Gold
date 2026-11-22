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
  for (const functionName of ["isNil", "fmt", "calculatePeg", "renderPegContent", "renderCell", "getSortValue"]) {
    vm.runInContext(section(`    function ${functionName}(`, "\n    function "), context);
  }
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
}
