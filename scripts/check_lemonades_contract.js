const fs = require("fs");
const path = require("path");
const assert = require("assert");

const root = path.resolve(__dirname, "..");
const productsPath = path.join(root, "tma_static", "products.json");
const shopPath = path.join(root, "tma_static_v2", "index.html");
const assetDir = path.join(root, "tma_static", "assets", "lemonades");

const data = JSON.parse(fs.readFileSync(productsPath, "utf8"));
const shop = fs.readFileSync(shopPath, "utf8");

const expected = new Map([
  ["ORGANIC COLA", 12],
  ["ГРЕЙПФРУТ", 24],
  ["ГРИНКЕЛЬ ВОВЕЛЬ", 12],
  ["ГРУША И ЖАСМИН", 24],
  ["ГРУША, РОЗМАРИН И ПЕРЕЦ", 12],
  ["ГУАВА И БЕРГАМОТ", 12],
  ["ЕЖЕВИКА И КАРКАДЕ", 24],
  ["ИВАНЧАЙ И МЯТА", 12],
  ["МАРАКУЙЯ И ПЕРСИК", 24],
  ["ПОРТОВЫЙ ГЛЁГ", 12],
  ["ТОНИК", 12],
  ["ТОНИК ИЛАНГ-ИЛАНГ", 12],
  ["ТОНИК ЮДЗУ", 12],
  ["ЯГОДЫ И ЁЛКИ", 12],
]);

assert.ok(data.categories.some(c => c.id === "lemonade" && c.name === "Лимонады"), "lemonade category must exist");
assert.ok(data.subcategories.some(s => s.id === "lemonade_bakunin" && s.parent === "lemonade"), "Bakunin subcategory must exist");

const products = data.products.filter(p => p.category === "lemonade" && p.subcategory === "lemonade_bakunin");
assert.strictEqual(products.length, expected.size, "must import exactly 14 Bakunin lemonade SKUs");

for (const [name, stock] of expected) {
  const p = products.find(item => item.name === name);
  assert.ok(p, `${name} must exist`);
  assert.strictEqual(p.stock, stock, `${name} stock must come from invoice`);
  assert.strictEqual(p.hidden, false, `${name} must be visible after arrival`);
  assert.ok(Array.isArray(p.fasovka) && p.fasovka.length === 1, `${name} must have one fasovka`);
  assert.strictEqual(p.fasovka[0].size, "0,33 л", `${name} fasovka must be 0,33 л`);
  assert.strictEqual(p.fasovka[0].price, 125, `${name} base shop price must be 125`);
  assert.ok(typeof p.photo === "string" && p.photo.startsWith("assets/lemonades/"), `${name} must have lemonade asset photo`);
  assert.ok(fs.existsSync(path.join(root, "tma_static", p.photo)), `${name} photo file must exist`);
}

assert.ok(fs.existsSync(path.join(assetDir, "bakunin_lemonades_hero.jpg")), "hero image must exist");
assert.ok(fs.existsSync(path.join(assetDir, "bakunin_lemonades_promo.png")), "promo image must exist");
assert.ok(fs.existsSync(path.join(assetDir, "bakunin_lemonades_price_2026.pdf")), "price PDF must exist");
assert.ok(fs.existsSync(path.join(assetDir, "bakunin_drinks_lineup_2026.html")), "standalone HTML presentation must exist");
const presentation = fs.readFileSync(path.join(assetDir, "bakunin_drinks_lineup_2026.html"), "utf8");
assert.ok(!presentation.includes("__bundler"), "HTML presentation must not use artifact bundler");
assert.ok(!presentation.includes("assets/cans/"), "HTML presentation must not reference missing cans bundle");
for (let i = 1; i <= 9; i += 1) {
  const name = `slide_${String(i).padStart(2, "0")}.jpg`;
  assert.ok(presentation.includes(`presentation/${name}`), `${name} must be linked from HTML presentation`);
  assert.ok(fs.existsSync(path.join(assetDir, "presentation", name)), `${name} must exist`);
}
assert.match(shop, /cat:\s*'lemonade'/, "home category tiles must know about lemonade after products become visible");
assert.match(shop, /bakunin_lemonades_promo\.png/, "home hero must use lemonade promo image");
assert.match(shop, /bakunin_drinks_lineup_2026\.html/, "home hero must link HTML presentation");

console.log("lemonades contract ok");
