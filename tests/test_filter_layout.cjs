const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

for (const name of ["template.html", "index.html"]) {
  test(`${name}: price LH label and controls share a four-column row`, () => {
    const source = fs.readFileSync(path.join(__dirname, "..", name), "utf8");
    const rule = source.match(/\.filter-item\.filter-item-price-lh\s*\{([^}]*)\}/);
    assert.ok(rule);
    const columns = rule[1].match(/grid-template-columns:\s*([^;]+);/);
    assert.equal(columns[1].trim().split(/\s+/).length, 4);
    const item = source.match(/<div class="filter-item filter-item-price-lh">([\s\S]*?)<\/div>/);
    assert.ok(item);
    assert.match(item[1], /<label>價格120日LH距離<\/label>[\s\S]*id="priceLhBaseFilter"[\s\S]*id="priceLhOperator"[\s\S]*id="priceLhDistanceFilter"/);
  });
}
