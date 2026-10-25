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
const context = vm.createContext({
  Node: { ELEMENT_NODE: 1, TEXT_NODE: 3 },
  getComputedStyle: element => element.style,
});
vm.runInContext(exportHelpers(template), context);

function element(tagName, children = [], options = {}) {
  const node = {
    nodeType: 1,
    tagName,
    childNodes: children,
    children: children.filter(child => child.nodeType === 1),
    style: { display: tagName === "BR" ? "inline" : "block", color: "rgb(139, 148, 158)", ...options.style },
    classList: { contains: name => (options.classes ?? []).includes(name) },
  };
  children.forEach(child => { child.parentElement = node; });
  return node;
}

const text = nodeValue => ({ nodeType: 3, nodeValue });
const span = (value, color) => element("SPAN", [text(value)], {
  style: color ? { color } : {},
});
const cellText = cell => context.excelCellRuns(cell).map(run => run.text).join("");

test("price LH grid rows keep dates and both prices on four logical lines", () => {
  const row = children => element("DIV", children, {
    classes: ["table-lv2", "price-lh-line"],
    style: { display: "grid" },
  });
  const cell = element("TD", [
    text("\n  "),
    row([span("高高", "rgb(255, 0, 0)"), span(""), span("")]),
    text("\n  "),
    row([span("1020："), span(" 207.50", "rgb(0, 255, 0)"), span("/ 228.50")]),
    text("\n  "),
    row([span("1019："), span(" 184"), span("/ 215")]),
    text("\n  "),
    row([span("1016："), span(" 190"), span("/ 201.50")]),
    text("\n"),
  ]);
  const expected = "高高\n1020： 207.50/ 228.50\n1019： 184/ 215\n1016： 190/ 201.50";
  assert.equal(cellText(cell), expected);
  const runs = context.excelCellRuns(cell);
  assert.ok(runs.some(run => run.text.includes("高高") && run.color === "FF0000"));
  assert.ok(runs.some(run => run.text.includes("207.50") && run.color === "00FF00"));
  const xml = context.excelCellXml(runs, "C2");
  assert.ok(xml.includes('r="C2" s="0" t="inlineStr"'));
  assert.ok(xml.includes('<color rgb="FF00FF00"/>'));
  assert.equal(xml.replace(/<[^>]*>/g, ""), expected);
});

test("ordinary blocks and explicit BR retain existing line breaks", () => {
  const cell = element("TD", [
    element("DIV", [text("first"), element("BR"), text("second")]),
    text("\n  "),
    element("DIV", [text("third")]),
  ]);
  assert.equal(cellText(cell), "first\nsecond\nthird");
});

test("gross margin source indentation does not add lines to populated metrics", () => {
  const cell = element("TD", [
    text("\n  "),
    element("DIV", [
      text("\n    "),
      element("DIV", [text("35.50")]),
      text("\n    "),
      element("DIV", [
        text("\n      "),
        element("DIV", [text("M -")]),
        text("\n      \n      "),
        element("DIV", [text("\n        Q ↑ 0.50%\n      ")], {
          style: { color: "rgb(255, 0, 0)" },
        }),
        text("\n    \n      "),
        element("DIV", [text("\n        Y ↓↓ -2%\n      ")], {
          style: { color: "rgb(0, 255, 0)" },
        }),
        text("\n    \n    "),
      ], { style: { display: "flex" } }),
      text("\n  "),
    ]),
  ]);
  const expected = "35.50\nM -\nQ ↑ 0.50%\nY ↓↓ -2%";
  const runs = context.excelCellRuns(cell);
  assert.equal(cellText(cell), expected);
  assert.equal(context.excelCellXml(runs, "C2").replace(/<[^>]*>/g, ""), expected);
  assert.ok(runs.some(run => run.text.includes("Q ↑ 0.50%") && run.color === "FF0000"));
  assert.ok(runs.some(run => run.text.includes("Y ↓↓ -2%") && run.color === "00FF00"));
});

test("inline text, missing values and XML escaping remain intact", () => {
  const cell = element("TD", [
    element("SPAN", [text("<&>")], { style: { display: "inline" } }),
    text(" / -"),
  ]);
  assert.equal(cellText(cell), "<&> / -");
  assert.ok(context.excelCellXml(context.excelCellRuns(cell), "A1")
    .includes("&lt;&amp;&gt;"));
});

test("current report and template share the same Excel text helpers", () => {
  const report = fs.readFileSync(path.join(root, "index.html"), "utf8");
  assert.equal(exportHelpers(report), exportHelpers(template));
});
