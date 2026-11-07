import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { request } from "../../obox_builder/builder/static/http.js";

const source = readFileSync(
  new URL("../../obox_builder/builder/static/record.js", import.meta.url),
  "utf8",
).replace(/^import .*;\n/, "");

for (const mode of ["fragment", "ordinary redirect", "page navigation"]) {
  test(`record handles ${mode} through the shared client`, async (t) => {
    const navigate = mode === "page navigation";
    const navigations = [];
    const replacements = [];
    const requests = [];
    const root = { id: "record-content", querySelectorAll: () => [], querySelector: () => null };
    const destination = "https://example.test/login";
    const navigation = Promise.withResolvers();
    globalThis.window = {
      location: {
        assign(url) {
          navigations.push(url);
          navigation.resolve();
        },
      },
    };
    t.after(() => delete globalThis.window);
    t.mock.method(globalThis, "fetch", async (url, options) => {
      requests.push({ url, options });
      return {
        redirected: mode === "ordinary redirect",
        headers: new Headers(navigate ? { "X-Obox-Redirect": destination } : {}),
        ok: !navigate,
        text: async () => {
          assert.equal(navigate, false, "a navigation response must not update the record");
          return "<div id='record-content'>Updated</div>";
        },
      };
    });
    const work = runInNewContext(`${source}\nreplace('/record/1/fields/name');`, {
      request,
      document: {
        addEventListener() {},
        querySelectorAll: () => [],
        getElementById: () => ({ replaceWith: (content) => replacements.push(content) }),
      },
      DOMParser: class {
        parseFromString() {
          return { body: { firstElementChild: root } };
        }
      },
    });
    await (navigate ? navigation.promise : work);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].options.headers.get("X-Obox-Fragment"), "true");
    assert.equal(requests[0].options.headers.get("X-Obox-Request"), "true");
    assert.deepEqual(navigations, navigate ? [destination] : []);
    assert.deepEqual(replacements, navigate ? [] : [root]);
  });
}
