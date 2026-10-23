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
      page = await browser.newPage({reducedMotion: "reduce"});
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
      await page.evaluate(() => window.scrollTo(0, 400));
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
      await page.locator('#admin-sidebar-shell a[href="/admin/users"]').click();
      await ready(page, "Users");
      await page.goBack();
      await ready(page, "Products");
      assert.ok(await page.evaluate(() => window.scrollY >= 350));
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
      await page.getByRole("alert").filter({hasText: "deleted"}).waitFor();
      assert.equal(requests.filter((request) => request.method() === "POST").length, beforeDelete + 1);

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

test("navigation still works with JavaScript disabled", async () => {
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
  } finally { await browser.close(); }
});
