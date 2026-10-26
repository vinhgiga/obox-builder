import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:net";
import { after, before, test } from "node:test";
import { chromium, firefox, webkit } from "playwright";

let server;
let origin;
before(async () => {
  const socket = createServer().listen(0, "127.0.0.1");
  await once(socket, "listening");
  const port = socket.address().port;
  await new Promise((resolve) => socket.close(resolve));
  origin = `http://127.0.0.1:${port}`;
  server = spawn("uv", ["run", "python", "-m", "tests.browser.server"], {
    env: {...process.env, OBOX_BROWSER_PORT: String(port)}, stdio: ["ignore", "pipe", "pipe"]
  });
  await new Promise((resolve, reject) => {
    let output = "";
    server.stderr.on("data", (chunk) => {
      output += chunk;
      if (output.includes("Uvicorn running")) resolve();
    });
    server.once("exit", () => reject(new Error(output)));
  });
});
after(async () => {
  if (server?.exitCode === null) {
    server.kill("SIGTERM");
    await once(server, "exit");
  }
});

async function login(page) {
  await page.goto(`${origin}/admin/login`);
  await page.locator('[name="username"]').fill("browser-user");
  await page.locator('[name="password"]').fill("Browser-test-password-2026!");
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(`${origin}/admin/`);
  await page.waitForFunction(() => window.htmx);
  await page.evaluate(() => { window.documentMarker = "same-document"; });
}
async function ready(page, title) {
  await page.waitForFunction((title) => document.title.startsWith(title), title);
  assert.equal(await page.locator("#admin-main").count(), 1);
  assert.equal(await page.locator("#admin-sidebar-shell").count(), 1);
  assert.equal(await page.evaluate(() => window.documentMarker), "same-document");
}

for (const [engine, browserType] of Object.entries({chromium, firefox, webkit})) {
  test(`${engine}: navigation, preload, history, forms and recovery`, {timeout: 90000}, async () => {
    const browser = await browserType.launch();
    let page;
    try {
      const context = await browser.newContext({reducedMotion: "reduce"});
      page = await context.newPage();
      page.setDefaultTimeout(10000);
      await page.clock.install();
      const errors = [];
      const requests = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("request", (request) => requests.push(request));
      await login(page);
      const listing = "/admin/shop/product";
      await page.locator(`#admin-sidebar-shell a[href="${listing}"]`).click();
      await ready(page, "Products");
      assert.equal(await page.locator(`#admin-sidebar-shell a[href="${listing}"]`).getAttribute("aria-current"), "page");
      assert.ok(requests.some((request) => request.headers()["hx-preloaded"] === "true"));
      assert.equal(await page.locator("[data-query-filters] button").count(), 2);
      const tab = await page.context().newPage();
      await tab.goto(`${origin}${listing}`);
      assert.match(await tab.title(), /^Products/);
      assert.equal(await tab.locator("#admin-main").count(), 1);
      await tab.close();
      const tableViewport = page.locator("[data-list-table]");
      await tableViewport.evaluate((table) => table.scrollTo(0, 400));
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
      assert.ok(await tableViewport.evaluate((table) => table.scrollTop > 0));
      const viewportTop = await tableViewport.evaluate((table) =>
        Math.round(table.getBoundingClientRect().top)
      );
      const headerTop = await page.locator("[data-list-table] thead th").first()
        .evaluate((header) => Math.round(header.getBoundingClientRect().top));
      assert.equal(headerTop, viewportTop);
      await page.locator('#admin-sidebar-shell a[href="/admin/users"]').click();
      await ready(page, "Users");
      await page.goBack();
      await ready(page, "Products");
      await page.goForward();
      await ready(page, "Users");
      await page.goBack();
      await ready(page, "Products");
      assert.equal(await page.locator("[data-query-filters] button").count(), 2);

      // Programmatic search goes through the same navigation lifecycle.
      const search = page.locator('#list-query input[name="q"]');
      await search.fill("Product 1");
      await page.waitForURL(/q=Product\+1/);
      await page.waitForFunction(() => document.querySelector('#list-query input[name="q"]') === document.activeElement);
      assert.equal(await search.inputValue(), "Product 1");
      assert.equal(await search.evaluate((input) => input.selectionStart), 9);
      await page.locator('[data-query-panel="filter"] > summary').click();
      await page.locator('#list-query a').filter({hasText:"Reset"}).click();
      await ready(page, "Products");
      await page.waitForFunction(() => !location.search);

      await page.getByRole("link", {name: "New product", exact: true}).click();
      await ready(page, "New product");
      await page.locator('[name="name"]').evaluate((input) => { input.form.noValidate = true; });
      await page.locator('[name="price_cents"]').fill("10");
      // Exercise the existing validation response through a 422 swap as well.
      await page.route(`**${listing}/add`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        const response = await route.fetch();
        await route.fulfill({response, status: 422});
      });
      await page.getByRole("button", {name: "Save", exact: true}).click();
      await page.locator(".invalid-feedback").first().waitFor();
      assert.equal(await page.locator('[name="name"]').inputValue(), "");
      await page.unroute(`**${listing}/add`);
      await page.locator('[name="name"]').fill(`Browser ${engine}`);
      await page.getByRole("button", {name: "Save", exact: true}).click();
      await ready(page, "Products");
      await page.getByRole("alert").filter({hasText: "created"}).waitFor();

      // A cancelled confirmation must send no destructive request.
      const beforeDelete = requests.filter((request) => request.method() === "POST").length;
      await page.locator('form[data-confirm] button').first().click();
      await page.locator("dialog[open]").waitFor();
      assert.equal(requests.filter((request) => request.method() === "POST").length, beforeDelete);
      await page.locator("[data-confirm-cancel]").click();
      await page.locator('form[data-confirm] button').first().click();
      await page.locator("[data-confirm-ok]").click();
      await ready(page, "Review deletion");
      await page.getByRole("button", {name: "Confirm deletion"}).click();
      await page.getByRole("alert").filter({hasText: "deleted"}).waitFor();
      assert.equal(requests.filter((request) => request.method() === "POST").length, beforeDelete + 2);

      // Composers and polling must work after navigation and stop on departure.
      await page.locator('#admin-sidebar-shell a[href="/admin/commentsdemo/ticket"]').click();
      await ready(page, "Tickets");
      await page.locator('#admin-main a[href$="/comments"]').click();
      await ready(page, "Ticket comments");
      await page.locator("#comment-body").fill(`Comment ${engine}`);
      await page.locator("#comment-body").press("Control+Enter");
      await page.locator(".comment-body").filter({hasText: `Comment ${engine}`}).waitFor();
      await page.locator('#admin-sidebar-shell a[href="/admin/jobs/tasks"]').click();
      await ready(page, "Background jobs");
      const jobsBefore = requests.filter((request) => request.url().includes("/admin/jobs/tasks")).length;
      await Promise.all([
        page.waitForResponse((response) => response.url().includes("/admin/jobs/tasks")),
        page.locator("[data-refresh-now]").click()
      ]);
      assert.equal(requests.filter((request) => request.url().includes("/admin/jobs/tasks")).length, jobsBefore + 1);
      await page.locator('#admin-sidebar-shell a[href="/admin/users"]').click();
      await ready(page, "Users");
      const jobsAfter = requests.filter((request) => request.url().includes("/admin/jobs/tasks")).length;
      await page.clock.runFor(6000);
      assert.equal(requests.filter((request) => request.url().includes("/admin/jobs/tasks")).length, jobsAfter);
      await page.clock.resume();
      await page.locator(`#admin-sidebar-shell a[href="${listing}"]`).click();
      await ready(page, "Products");

      await page.route("**/admin/users", (route) => route.abort());
      await page.locator('#admin-sidebar-shell a[href="/admin/users"]').click();
      await page.locator("#admin-network-error:not(.d-none)").waitFor();
      assert.ok(await page.locator("#admin-main").textContent());
      await page.unroute("**/admin/users");
      await page.locator('#admin-sidebar-shell a[href="/admin/users"]').click();
      await ready(page, "Users");

      await page.reload();
      await page.waitForFunction(() => window.htmx);
      assert.equal(await page.locator("#admin-main").count(), 1);
      await page.context().clearCookies();
      await page.locator(`#admin-sidebar-shell a[href="${listing}"]`).click();
      await page.waitForURL(/\/admin\/login\?/);
      assert.equal(await page.locator("#admin-main").count(), 0);
      assert.deepEqual(errors, []);
    } catch (error) {
      console.error(engine, page?.url(), error.message);
      throw error;
    } finally { await browser.close(); }
  });
}

for (const [engine, browserType] of Object.entries({chromium, firefox, webkit})) {
  test(`${engine}: list field visibility and row height persist`, async () => {
    const browser = await browserType.launch();
    try {
      const page = await browser.newPage();
      page.setDefaultTimeout(10000);
      await login(page);
      const listing = `${origin}/admin/shop/product`;
      await page.goto(listing);
      await page.locator("[data-list-table]").waitFor();

      await page.locator("[data-fields-panel] > summary").click();
      const fieldSearch = page.locator("[data-field-search]");
      await fieldSearch.fill("price");
      const priceToggle = page.locator('[data-field-toggle="price_cents"]');
      assert.equal(await priceToggle.isChecked(), true);
      await priceToggle.uncheck();
      assert.equal(await page.locator('th[data-list-column="price_cents"]').isVisible(), false);
      await page.locator("[data-hide-all]").click();
      assert.equal(
        await page.locator(".admin-list-table-scroll thead th[data-list-column]:visible").count(),
        0,
      );
      await page.locator("[data-show-all]").click();
      assert.equal(await page.locator('th[data-list-column="price_cents"]').isVisible(), true);

      await fieldSearch.fill("price");
      await priceToggle.uncheck();
      await page.keyboard.press("Escape");
      await page.locator("[data-row-height-panel] > summary").click();
      await page.locator('[data-row-height-choice="compact"]').click();
      assert.equal(await page.locator("[data-list-table]").getAttribute("data-row-height"), "compact");

      await page.goto(`${origin}/admin/users`);
      await page.goBack();
      await page.locator("[data-list-table]").waitFor();
      assert.equal(await page.locator('th[data-list-column="price_cents"]').isVisible(), false);
      assert.equal(await page.locator("[data-list-table]").getAttribute("data-row-height"), "compact");
      await page.reload();
      assert.equal(await page.locator('th[data-list-column="price_cents"]').isVisible(), false);
      assert.equal(await page.locator("[data-list-table]").getAttribute("data-row-height"), "compact");
    } finally {
      await browser.close();
    }
  });
}

test("navigation, child forms and deletion work with JavaScript disabled", async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({javaScriptEnabled: false});
    await page.goto(`${origin}/admin/login`);
    await page.locator('[name="username"]').fill("browser-user");
    await page.locator('[name="password"]').fill("Browser-test-password-2026!");
    await page.locator('button[type="submit"]').click();
    await page.locator('#admin-sidebar-shell a[href="/admin/shop/product"]').click();
    assert.match(await page.title(), /^Products/);
    assert.equal(await page.locator("#admin-main").count(), 1);
    await page.goto(`${origin}/admin/shop/order/add`);
    await page.locator('[name="reference"]').fill("Browser order without scripts");
    await page.locator('[name="inline:0:0:sku"]').fill("Line without scripts");
    await page.getByRole("button", {name: "Save", exact: true}).click();
    await page.waitForURL(`${origin}/admin/shop/order`);
    await page.getByRole("row").filter({hasText: "Browser order without scripts"})
      .getByRole("link", {name: "Edit", exact: true}).click();
    assert.equal(await page.locator('[name="inline:0:0:sku"]').inputValue(), "Line without scripts");
    const detail = page.url();
    await page.getByRole("button", {name: "Save and continue editing", exact: true}).click();
    await page.getByRole("alert").filter({hasText: "updated"}).waitFor();
    await page.goto(detail + "/delete");
    assert.match(await page.locator("#admin-main").textContent(), /Line without scripts/);
    await page.getByRole("button", {name: "Confirm deletion"}).click();
    await page.waitForURL(`${origin}/admin/shop/order`);
    assert.equal(await page.getByRole("row").filter({hasText: "Browser order without scripts"}).count(), 0);
  } finally { await browser.close(); }
});

for (const [engine, browserType] of Object.entries({chromium, firefox, webkit})) {
  test(`${engine}: relation search, multiple selections, child forms and deletion preview`, {timeout: 90000}, async () => {
    const browser = await browserType.launch();
    let page;
    const errors = [];
    try {
      page = await browser.newPage({reducedMotion: "reduce"});
      page.on("pageerror", (error) => errors.push(error.message));
      page.setDefaultTimeout(10000);
      await login(page);
      await page.goto(`${origin}/admin/shop/order/add`);
      const reference = `Browser order ${engine}`;
      await page.locator('[name="reference"]').fill(reference);
      const labels = page.locator('[data-relation-picker]').filter({has: page.locator('select[name="labels"]')});
      await labels.getByRole("combobox").fill("Browser label");
      await labels.getByRole("option", {name: "Browser label 00", exact: true}).waitFor();
      await labels.getByRole("button", {name: "Load more"}).click();
      await labels.getByRole("option", {name: "Browser label 24", exact: true}).click();
      await labels.getByRole("combobox").fill("Browser label 01");
      await labels.getByRole("option", {name: "Browser label 01", exact: true}).waitFor();
      await labels.getByRole("combobox").press("ArrowDown");
      await labels.getByRole("combobox").press("Enter");
      assert.equal(await labels.locator('select option:checked').count(), 2);
      await page.locator('[name="inline:0:0:sku"]').fill(`Line A ${engine}`);
      const product = page.locator('[data-relation-picker]').filter({has: page.locator('select[name="inline:0:0:product_id"]')});
      await product.getByRole("combobox").fill("Product 0");
      await product.getByRole("option", {name: "Product 0", exact: true}).click();
      await page.locator("[data-inline-add]").click();
      await page.locator('[name="inline:0:1:sku"]').fill(`Line B ${engine}`);
      await page.locator("[data-inline-add]").click();
      await page.locator('[name="inline:0:2:sku"]').fill("Discard this line");
      await page.locator("[data-inline-row]").nth(2).locator("[data-inline-remove]").click();
      await page.getByRole("button", {name: "Save", exact: true}).click();
      await page.waitForURL(`${origin}/admin/shop/order`);
      const row = page.getByRole("row").filter({hasText: reference});
      await row.getByRole("link", {name: "Edit", exact: true}).click();
      assert.equal(await page.locator('[name="inline:0:0:sku"]').inputValue(), `Line A ${engine}`);
      assert.equal(await page.locator('[name="inline:0:1:sku"]').inputValue(), `Line B ${engine}`);
      assert.equal(await page.locator('select[name="labels"] option:checked').count(), 2);
      const detail = page.url();
      await page.locator('#admin-sidebar-shell a[href="/admin/users"]').click();
      await page.waitForFunction(() => document.title.startsWith("Users"));
      await page.goBack();
      await page.waitForURL(detail);
      await page.waitForFunction(() => document.title.startsWith("Edit order"));
      const restored = page.locator('[data-relation-picker]').filter({has: page.locator('select[name="labels"]')});
      await restored.getByRole("combobox").fill("Browser label 02");
      await restored.getByRole("option", {name: "Browser label 02", exact: true}).click();
      assert.equal(await restored.locator('select option:checked').count(), 3);
      await page.getByRole("button", {name: "Save and continue editing", exact: true}).click();
      await page.getByRole("alert").filter({hasText: "updated"}).waitFor();
      await page.goto(detail + "/delete");
      assert.match(await page.locator("#admin-main").textContent(), new RegExp(`Line A ${engine}`));
      assert.match(await page.locator("#admin-main").textContent(), /Remove link/);
      await page.getByRole("button", {name: "Confirm deletion"}).click();
      await page.waitForURL(`${origin}/admin/shop/order`);
      assert.equal(await page.getByRole("row").filter({hasText: reference}).count(), 0);
      assert.deepEqual(errors, []);
    } catch (error) {
      console.error(engine, page?.url(), error.message);
      throw error;
    } finally { await browser.close(); }
  });
}
