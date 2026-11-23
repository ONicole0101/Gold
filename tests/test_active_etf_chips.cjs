const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

for (const name of ["template.html", "index.html"]) {
  const source = fs.readFileSync(path.join(__dirname, "..", name), "utf8");
  const context = vm.createContext({});
  for (const fn of ["isNil", "fmt", "distClass", "fmtMonthDay", "chipDateMMDD",
    "signedFmt", "renderActiveEtfNetContent"]) {
    const start = source.indexOf(`    function ${fn}(`);
    const end = source.indexOf("\n    function ", start + 1);
    assert.ok(start >= 0 && end > start, `Missing ${fn} in ${name}`);
    vm.runInContext(source.slice(start, end), context);
  }
  test(`${name}: active ETF field follows foreign investors and renders shares`, () => {
    assert.match(source, /key: "foreign_investor_net"[^}]*},\s*{ key: "active_etf_net"/);
    const html = context.renderActiveEtfNetContent({
      chip_date_t0: "2026-11-20", chip_date_t1: "2026-11-19",
      active_etf_net_t0: 1001, active_etf_net_t1: -200, active_etf_net_t2: null,
    });
    assert.match(html, /up">1120：\+1,001股/);
    assert.match(html, /dn">1119：-200股/);
    assert.match(html, /T-2：-/);
    assert.match(context.renderActiveEtfNetContent({active_etf_net_t0: 0}), /T：0股/);
  });
}
