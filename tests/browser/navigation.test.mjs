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
    const onServerOutput = (chunk) => { output += chunk; };
    server.stdout.on("data", onServerOutput);
    server.stderr.on("data", onServerOutput);
    server.once("exit", () => reject(new Error(output)));
    const deadline = Date.now() + 15000;
    const checkReady = async () => {
      try {
        const response = await fetch(`${origin}/admin/login`, {signal: AbortSignal.timeout(1000)});
        if (response.ok) return resolve();
      } catch {}
      if (Date.now() >= deadline) return reject(new Error(`Browser test server did not start: ${output}`));
      setTimeout(checkReady, 100);
    };
    checkReady();
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
      assert.equal(await page.locator("[data-query-action]").count(), 2);
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
      assert.equal(await page.locator("[data-query-action]").count(), 2);

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
      await page.waitForFunction(() => Boolean(document.querySelector(".admin-fields-panel").style.maxWidth));
      const fieldMenuRect = await page.locator("[data-fields-panel]").evaluate((popover) => {
        const trigger = popover.querySelector(":scope > summary").getBoundingClientRect();
        const panel = popover.querySelector(":scope > .admin-toolbar-panel").getBoundingClientRect();
        const option = popover.querySelector(".admin-field-option").getBoundingClientRect();
        const bounds = popover.closest(".admin-list-card").getBoundingClientRect();
        return {
          triggerRight: trigger.right,
          triggerBottom: trigger.bottom,
          panelLeft: panel.left,
          panelRight: panel.right,
          panelTop: panel.top,
          panelWidth: panel.width,
          optionHeight: option.height,
          boundsLeft: bounds.left,
        };
      });
      assert.ok(Math.abs(fieldMenuRect.panelRight - fieldMenuRect.triggerRight) < 1);
      assert.ok(fieldMenuRect.panelTop >= fieldMenuRect.triggerBottom);
      assert.ok(fieldMenuRect.panelLeft >= fieldMenuRect.boundsLeft);
      assert.ok(fieldMenuRect.panelWidth <= 21 * 16 + 1);
      assert.ok(fieldMenuRect.optionHeight <= 2.25 * 16 + 1);
      const initialOrder = await page.locator("thead th[data-list-column]").evaluateAll((cells) =>
        cells.map((cell) => cell.dataset.listColumn),
      );
      const movedField = initialOrder[1];
      const reorderHandle = page.locator(`[data-field-reorder="${movedField}"]`);
      const movedOption = page.locator(".admin-field-option").filter({
        has: page.locator(`[data-field-toggle="${movedField}"]`),
      });
      assert.equal(await movedOption.getAttribute("draggable"), null);
      assert.equal(await reorderHandle.getAttribute("draggable"), null);
      await reorderHandle.focus();
      await reorderHandle.press("Alt+ArrowUp");
      const reordered = [initialOrder[1], initialOrder[0], ...initialOrder.slice(2)];
      assert.deepEqual(
        await page.locator("thead th[data-list-column]").evaluateAll((cells) =>
          cells.map((cell) => cell.dataset.listColumn),
        ),
        reordered,
      );
      assert.deepEqual(
        await page.locator("tbody tr[data-group-row]").first().locator("[data-list-column]").evaluateAll((cells) =>
          cells.map((cell) => cell.dataset.listColumn),
        ),
        reordered,
      );
      const draggedOption = page.locator(".admin-field-option").filter({
        has: page.locator(`[data-field-toggle="${reordered[0]}"]`),
      });
      const targetOption = page.locator(".admin-field-option").last();
      const sourceBounds = await draggedOption.boundingBox();
      const targetBounds = await targetOption.boundingBox();
      await page.mouse.move(
        sourceBounds.x + sourceBounds.width / 2,
        sourceBounds.y + sourceBounds.height / 2,
      );
      await page.mouse.down();
      await page.mouse.move(
        targetBounds.x + targetBounds.width / 2,
        targetBounds.y + targetBounds.height * 0.9,
        {steps: 8},
      );
      assert.equal(await page.locator(".admin-field-placeholder").count(), 1);
      await page.mouse.up();
      const afterDrag = await page.locator("thead th[data-list-column]").evaluateAll((cells) =>
        cells.map((cell) => cell.dataset.listColumn),
      );
      assert.notDeepEqual(afterDrag, reordered);
      assert.ok(afterDrag.indexOf(reordered[0]) > 1);
      assert.deepEqual([...afterDrag].sort(), [...reordered].sort());
      await page.reload();
      await page.locator("[data-list-table]").waitFor();
      assert.deepEqual(
        await page.locator("thead th[data-list-column]").evaluateAll((cells) =>
          cells.map((cell) => cell.dataset.listColumn),
        ),
        afterDrag,
      );
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

for (const [engine, browserType] of Object.entries({chromium, firefox, webkit})) {
  test(`${engine}: filter field search and condition editing`, async () => {
    const browser = await browserType.launch();
    try {
      const page = await browser.newPage();
      page.setDefaultTimeout(10000);
      await login(page);
      await page.goto(`${origin}/admin/shop/product`);
      await page.locator("[data-list-table]").waitFor();

      await page.locator('[data-query-panel="filter"] > summary').click();
      assert.equal(await page.locator('[data-query-panel="filter"] .admin-query-panel-header h3').textContent(), "Filter");
      await page.waitForFunction(() => Boolean(document.querySelector('[data-query-panel="filter"] .admin-query-panel').style.maxWidth));
      const filterMenuRect = await page.locator('[data-query-panel="filter"]').evaluate((popover) => {
        const trigger = popover.querySelector(":scope > summary").getBoundingClientRect();
        const panel = popover.querySelector(":scope > .admin-toolbar-panel").getBoundingClientRect();
        const fieldControl = popover.querySelector(".admin-filter-field-picker > summary");
        const operatorControl = popover.querySelector('.admin-filter-row > select[aria-label="Filter operator"]');
        const deleteControl = popover.querySelector('.admin-filter-row > button[aria-label="Delete condition"]');
        const bounds = popover.closest(".admin-list-card").getBoundingClientRect();
        return {
          triggerRight: trigger.right,
          panelLeft: panel.left,
          panelRight: panel.right,
          panelWidth: panel.width,
          fieldControlHeight: fieldControl.getBoundingClientRect().height,
          operatorControlHeight: operatorControl.getBoundingClientRect().height,
          operatorLineHeight: Number.parseFloat(getComputedStyle(operatorControl).lineHeight),
          deleteControlHeight: deleteControl.getBoundingClientRect().height,
          boundsLeft: bounds.left,
        };
      });
      assert.ok(Math.abs(filterMenuRect.panelRight - filterMenuRect.triggerRight) < 1);
      assert.ok(filterMenuRect.panelLeft >= filterMenuRect.boundsLeft);
      assert.ok(filterMenuRect.panelWidth <= 42 * 16 + 1);
      assert.ok(filterMenuRect.fieldControlHeight <= 2.125 * 16 + 1);
      assert.equal(filterMenuRect.fieldControlHeight, filterMenuRect.operatorControlHeight);
      assert.equal(filterMenuRect.fieldControlHeight, filterMenuRect.deleteControlHeight);
      assert.ok(filterMenuRect.operatorLineHeight < filterMenuRect.operatorControlHeight - 2);
      assert.equal(await page.getByText("Describe how to filter this view").count(), 0);
      assert.equal(await page.locator(".admin-filter-heading").textContent(), "In this view, show records");
      assert.equal(await page.locator(".admin-filter-row").count(), 1);
      assert.equal(await page.locator(".admin-filter-join-label").textContent(), "Where");
      assert.equal(await page.locator(".admin-filter-field-label").textContent(), "Name");
      assert.equal(await page.getByLabel("Filter operator").inputValue(), "contains");
      assert.equal(await page.locator('[data-query-panel="filter"] [data-query-count]').textContent(), "0");
      assert.deepEqual(
        JSON.parse(await page.locator('#list-query input[name="filter"]').inputValue()).children,
        [],
      );

      await page.locator(".admin-filter-field-picker > summary").click();
      const menu = page.locator(".admin-filter-field-menu");
      await menu.getByRole("searchbox", {name: "Find a field"}).fill("status");
      assert.equal(await menu.getByRole("button", {name: "Status"}).isVisible(), true);
      assert.equal(await menu.getByRole("button", {name: "Name"}).isVisible(), false);
      await menu.getByRole("button", {name: "Status"}).click();
      await page.getByLabel("Filter value").selectOption("draft");

      const filter = JSON.parse(await page.locator('#list-query input[name="filter"]').inputValue());
      assert.deepEqual(filter.children, [{field: "status", op: "eq", value: "draft"}]);
      assert.equal(await page.locator('[data-query-panel="filter"] [data-query-count]').textContent(), "1");

      await page.locator('[data-query-action="add-condition"]').click();
      assert.equal(await page.locator(".admin-filter-row").count(), 2);
      assert.equal(
        await page.locator(".admin-filter-row").last().locator('select[aria-label="Condition join"]').inputValue(),
        "and",
      );
      await page.locator(".admin-filter-row").last().getByRole("button", {name: "Delete condition"}).click();
      assert.equal(await page.locator(".admin-filter-row").count(), 1);

      await page.locator('[data-query-action="add-group"]').click();
      assert.equal(await page.getByText("Group conditions").count(), 1);
      await page.locator(".admin-filter-nested-group").getByRole("button", {name: "Delete group"}).click();
      assert.equal(await page.locator(".admin-filter-nested-group").count(), 0);

      await page.locator('[data-query-panel="filter"] .admin-query-panel-actions button[type="submit"]').click();
      await page.waitForURL(/filter=/);
      assert.equal(await page.locator("[data-group-row]").count() > 0, true);
    } finally {
      await browser.close();
    }
  });
}

for (const [engine, browserType] of Object.entries({chromium, firefox, webkit})) {
  test(`${engine}: group field picker and ordering`, async () => {
    const browser = await browserType.launch();
    try {
      const page = await browser.newPage();
      page.setDefaultTimeout(10000);
      await login(page);
      await page.goto(`${origin}/admin/shop/product`);
      await page.locator("[data-list-table]").waitFor();

      await page.locator('[data-query-panel="group"] > summary').click();
      assert.equal(await page.locator('[data-query-panel="group"] .admin-query-panel-header h3').textContent(), "Group by");
      await page.waitForFunction(() => Boolean(document.querySelector('[data-query-panel="group"] .admin-query-panel').style.maxWidth));
      const groupPanelRect = await page.locator('[data-query-panel="group"]').evaluate((popover) => {
        const trigger = popover.querySelector(":scope > summary").getBoundingClientRect();
        const panel = popover.querySelector(":scope > .admin-toolbar-panel").getBoundingClientRect();
        const bounds = popover.closest(".admin-list-card").getBoundingClientRect();
        return {
          triggerRight: trigger.right,
          triggerBottom: trigger.bottom,
          panelLeft: panel.left,
          panelRight: panel.right,
          panelTop: panel.top,
          panelWidth: panel.width,
          panelHeight: panel.height,
          panelScrollHeight: panel.scrollHeight,
          panelClientHeight: panel.clientHeight,
          helpWidth: popover.querySelector(".admin-query-help > summary").getBoundingClientRect().width,
          boundsLeft: bounds.left,
        };
      });
      assert.ok(Math.abs(groupPanelRect.panelRight - groupPanelRect.triggerRight) < 1);
      assert.ok(groupPanelRect.panelTop >= groupPanelRect.triggerBottom);
      assert.ok(groupPanelRect.panelLeft >= groupPanelRect.boundsLeft);
      assert.ok(groupPanelRect.panelWidth <= 320);
      assert.ok(groupPanelRect.panelHeight < 310);
      assert.equal(groupPanelRect.panelScrollHeight, groupPanelRect.panelClientHeight);
      assert.ok(groupPanelRect.helpWidth <= 24);
      assert.equal(await page.getByText("Pick a field to group by").count(), 1);
      assert.deepEqual(await page.locator(".admin-group-field-option").allTextContents(), [
        "Status", "Category", "Is active", "Created at",
      ]);
      assert.equal(await page.getByRole("button", {name: "Name"}).isVisible(), false);
      const apply = page.locator('[data-query-panel="group"] .admin-query-panel-actions button[type="submit"]');
      assert.equal(await apply.isVisible(), false);

      const seeAll = page.getByRole("button", {name: "See all fields"});
      const groupSearch = page.getByRole("searchbox", {name: "Find a grouping field"});
      assert.equal(await groupSearch.count(), 0);
      assert.equal(await seeAll.getAttribute("aria-expanded"), "false");
      await seeAll.focus();
      await page.keyboard.press("Enter");
      const showFewer = page.getByRole("button", {name: "Show fewer fields"});
      assert.equal(await showFewer.getAttribute("aria-expanded"), "true");
      assert.equal(await page.getByText("Pick a field to group by").count(), 0);
      assert.equal(await groupSearch.evaluate((search) => search === document.activeElement), true);
      assert.deepEqual(await groupSearch.evaluate((search) => {
        const style = getComputedStyle(search);
        return [style.borderTopWidth, style.outlineStyle, style.boxShadow];
      }), ["0px", "none", "none"]);
      assert.equal(await groupSearch.evaluate((search) =>
        search.closest("[data-query-group]").firstElementChild.contains(search)), true);
      assert.equal(await page.getByRole("button", {name: "Name"}).isVisible(), true);
      await groupSearch.fill("  nAMe  ");
      assert.equal(await page.locator(".admin-group-field-option:visible").count(), 1);
      assert.equal(await page.getByRole("button", {name: "Name", exact: true}).isVisible(), true);
      await groupSearch.fill("no matching field");
      assert.equal(await page.getByRole("status").filter({hasText: "No fields found."}).isVisible(), true);
      assert.equal(await page.locator(".admin-group-field-option:visible").count(), 0);
      await groupSearch.press("Enter");
      assert.equal(new URL(page.url()).search, "");
      await groupSearch.fill("");
      assert.equal(await page.locator(".admin-group-field-option:visible").count(), 8);
      await showFewer.focus();
      await page.keyboard.press("Enter");
      assert.equal(await seeAll.evaluate((button) => button === document.activeElement), true);
      assert.equal(await groupSearch.count(), 0);
      assert.equal(await page.getByText("Pick a field to group by").isVisible(), true);
      assert.equal(await page.getByRole("button", {name: "Name"}).isVisible(), false);

      await page.getByRole("button", {name: "Status"}).click();
      assert.equal(await page.locator('#list-query input[name="group"]').inputValue(), "status");
      assert.equal(await apply.isVisible(), true);
      for (const closeWith of ["trigger", "escape", "outside", "other-panel"]) {
        await seeAll.click();
        assert.equal(await groupSearch.inputValue(), "");
        assert.equal(await groupSearch.evaluate((search) => search === document.activeElement), true);
        assert.equal(await page.getByText("Pick a field to group by").count(), 0);
        await groupSearch.fill("name");
        if (closeWith === "trigger") await page.locator('[data-query-panel="group"] > summary').click();
        else if (closeWith === "escape") await page.keyboard.press("Escape");
        else if (closeWith === "outside") await page.locator(".page-title").click();
        else await page.locator('[data-query-panel="sort"] > summary').click();
        await page.waitForFunction(() => !document.querySelector('[data-query-panel="group"]').open);
        await page.locator('[data-query-panel="group"] > summary').click();
        await page.waitForFunction(() => document.querySelector(".admin-group-see-all")?.getAttribute("aria-expanded") === "false");
        assert.equal(await groupSearch.count(), 0);
        assert.equal(await page.getByText("Pick a field to group by").isVisible(), true);
        assert.equal(await page.getByRole("button", {name: "Name", exact: true}).isVisible(), false);
        assert.equal(await page.locator('#list-query input[name="group"]').inputValue(), "status");
      }
      await page.getByRole("button", {name: "See all fields"}).click();
      assert.equal(await page.getByRole("button", {name: "Name"}).isVisible(), true);
      await groupSearch.fill("name");
      await page.getByRole("button", {name: "Name"}).click();
      assert.equal(await groupSearch.inputValue(), "name");
      assert.equal(await page.getByRole("status").filter({hasText: "No fields found."}).isVisible(), true);
      await groupSearch.fill("");
      await page.getByLabel("Group direction for Name").selectOption("desc");
      assert.equal(await page.locator('#list-query input[name="group"]').inputValue(), "status,-name");

      await page.getByRole("button", {name: "Is active", exact: true}).click();
      assert.equal(await page.locator(".admin-group-field-option:enabled").count(), 0);
      assert.equal(await page.getByText("You can group by up to 3 fields.").isVisible(), true);
      await page.setViewportSize({width: 390, height: 844});
      await page.waitForFunction(() => {
        const panel = document.querySelector('[data-query-panel="group"] > .admin-query-panel');
        return panel.getBoundingClientRect().bottom <= innerHeight - 8;
      });
      assert.equal(await page.locator('[data-query-panel="group"] > .admin-query-panel')
        .evaluate((panel) => panel.scrollWidth <= panel.clientWidth), true);
      await page.setViewportSize({width: 1280, height: 720});
      await page.getByRole("button", {name: "Move Is active group up"}).click();
      assert.equal(await page.locator('#list-query input[name="group"]').inputValue(), "status,is_active,-name");
      await page.getByRole("button", {name: "Remove Is active grouping"}).click();

      await page.getByRole("button", {name: "Remove Status grouping"}).click();
      assert.equal(await page.locator('#list-query input[name="group"]').inputValue(), "-name");
      await page.locator('[data-query-panel="group"] .admin-query-panel-actions button[type="submit"]').click();
      await page.waitForURL(/group=/);
      await page.waitForFunction(() => JSON.parse(document.querySelector("#list-query-config").textContent).group.length === 1);
      assert.ok(await page.locator('[data-group-header="1"]').count() > 0);
      assert.match(await page.locator('[data-group-header="1"]').first().textContent(), /Name:/);

      await page.locator('[data-query-panel="group"] > summary').click();
      await page.getByRole("button", {name: "Remove Name grouping"}).click();
      assert.equal(await apply.isVisible(), true);
      await apply.click();
      await page.waitForFunction(() => JSON.parse(document.querySelector("#list-query-config").textContent).group.length === 0);
      assert.equal(await page.locator('[data-group-header]').count(), 0);

      await page.setViewportSize({width: 390, height: 844});
      await page.locator('[data-query-panel="group"] > summary').click();
      await page.getByRole("button", {name: "See all fields"}).click();
      const mobilePanel = await page.locator('[data-query-panel="group"] > .admin-query-panel').boundingBox();
      assert.ok(mobilePanel.x >= 0 && mobilePanel.x + mobilePanel.width <= 390);
      assert.ok(mobilePanel.y + mobilePanel.height <= 844);
      await page.keyboard.press("Escape");
      assert.equal(await page.locator('[data-query-panel="group"]').getAttribute("open"), null);
    } finally {
      await browser.close();
    }
  });
}

test("group picker handles unavailable fields and a fully grouped list", async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    await login(page);
    for (const names of [[], ["meta"], ["name"]]) {
      await page.route("**/admin/shop/product", async (route) => {
        const response = await route.fetch();
        const body = (await response.text()).replace(
          /(<script[^>]*id="list-query-config"[^>]*>)([\s\S]*?)(<\/script>)/,
          (_, start, json, end) => {
            const config = JSON.parse(json);
            config.fields = config.fields.filter((field) => names.includes(field.name));
            config.sort = [];
            return start + JSON.stringify(config) + end;
          },
        );
        await route.fulfill({response, body});
      });
      await page.goto(`${origin}/admin/shop/product`);
      await page.locator('[data-query-panel="group"] > summary').click();
      assert.equal(await page.getByRole("button", {name: "See all fields"}).count(), 0);
      if (names.includes("name")) {
        assert.deepEqual(await page.locator(".admin-group-field-option").allTextContents(), ["Name"]);
        await page.getByRole("button", {name: "Name", exact: true}).click();
        assert.equal(await page.getByText("All available fields are grouped.").isVisible(), true);
        await page.getByRole("button", {name: "Remove Name grouping"}).click();
        assert.equal(await page.getByRole("button", {name: "Name", exact: true}).isVisible(), true);
      } else {
        assert.equal(await page.getByText("No fields can be grouped.").isVisible(), true);
        assert.equal(await page.locator(".admin-group-field-option").count(), 0);
      }
      assert.equal(await page.locator('[data-query-panel="group"] button[type="submit"]').isVisible(), false);
      await page.unroute("**/admin/shop/product");
    }
  } finally {
    await browser.close();
  }
});

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
