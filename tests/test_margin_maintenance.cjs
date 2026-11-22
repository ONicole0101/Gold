const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

for (const name of ["template.html", "index.html"]) {
  const source = fs.readFileSync(path.join(__dirname, "..", name), "utf8");
  const context = vm.createContext({});
  for (const functionName of [
    "isNil", "fmt", "fmtTrend", "trendClass", "fmtMonthDay", "chipDateMMDD",
    "pickFirst", "comparisonPct", "signedFmt", "renderMarginMaintenanceContent",
  ]) {
    const start = source.indexOf(`    function ${functionName}(`);
    const end = source.indexOf("\n    function ", start + 1);
    assert.ok(start >= 0 && end > start, `Missing ${functionName} in ${name}`);
    vm.runInContext(source.slice(start, end), context);
  }

  test(`${name}: index maintenance changes keep stock trend colors`, () => {
    assert.match(source, /\.index-row td \.margin-maintenance \.table-lv2\.up\s*\{\s*color:\s*#f85149;/);
    assert.match(source, /\.index-row td \.margin-maintenance \.table-lv2\.dn\s*\{\s*color:\s*#3fb950;/);
    for (const code of ["TAIEX", "TPEX", "2330"]) {
      for (const [value, expected] of [[150, "up strong"], [100, "dn strong"], [125, ""]]) {
        const html = context.renderMarginMaintenanceContent({
          code, is_index: code !== "2330", margin_maintenance_t0: value, margin_maintenance_ma5: 125,
        });
        assert.ok(html.includes(`class="table-lv2 ${expected}"`), html);
      }
    }
  });

  test(`${name}: only current maintenance values below 130% are green`, () => {
    for (const [value, expectedClass, formatted] of [
      [129.99, "dn", "129.99%"],
      ["129", "dn", "129%"],
      [0, "dn", "0%"],
      [130, "", "130%"],
      [131, "", "131%"],
      [null, "", "-"],
    ]) {
      const html = context.renderMarginMaintenanceContent({ margin_maintenance_t0: value });
      assert.ok(html.includes(`<span class="${expectedClass}">${formatted}</span>`), html);
    }
    const fallback = context.renderMarginMaintenanceContent({ margin_maintenance: 120 });
    assert.ok(fallback.includes('<span class="dn">120%</span>'));
  });
}
