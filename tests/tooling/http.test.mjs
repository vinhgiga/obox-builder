import assert from "node:assert/strict";
import { test } from "node:test";
import { request, uploadRequest } from "../../obox_builder/builder/static/http.js";

test("request preserves options, JSON errors, and cancellation", async (t) => {
  const signal = new AbortController().signal;
  const response = new Response('{"detail":"Sign in"}', { status: 401 });
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, "/api/items");
    assert.equal(options.method, "POST");
    assert.equal(options.body, "{}");
    assert.equal(options.signal, signal);
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers.get("Accept"), "application/json");
    assert.equal(options.headers.get("X-Obox-Request"), "true");
    return response;
  });
  const result = await request("/api/items", {
    method: "POST",
    body: "{}",
    signal,
    headers: { Accept: "application/json" },
  });
  assert.equal(result.status, 401);
  assert.deepEqual(await result.json(), { detail: "Sign in" });
});

test("request propagates network errors", async (t) => {
  const error = new TypeError("Network unavailable");
  t.mock.method(globalThis, "fetch", async () => {
    throw error;
  });
  await assert.rejects(request("/items"), (failure) => failure === error);
});

function xhrMock(t, { status = 200, destination = null, failure = null } = {}) {
  const instances = [];
  globalThis.XMLHttpRequest = class {
    upload = {};
    headers = new Headers();
    status = status;
    responseText = '{"offset":4}';
    constructor() {
      instances.push(this);
    }
    open(method, url) {
      this.method = method;
      this.url = url;
    }
    setRequestHeader(name, value) {
      this.headers.set(name, value);
    }
    getResponseHeader() {
      return destination;
    }
    send(body) {
      this.body = body;
      this.upload.onprogress({ loaded: 4 });
      if (failure) this[failure]();
      else this.onload();
    }
  };
  t.after(() => delete globalThis.XMLHttpRequest);
  return instances;
}

test("upload preserves bytes, offset, CSRF, progress, and response", async (t) => {
  const instances = xhrMock(t);
  const body = new Blob(["data"]);
  const progress = [];
  const response = await uploadRequest("/media/upload", body, {
    headers: { "Upload-Offset": "8", "X-CSRF-Token": "csrf" },
    onProgress: (event) => progress.push(event.loaded),
  });
  assert.equal(instances[0].method, "PATCH");
  assert.equal(instances[0].url, "/media/upload");
  assert.equal(instances[0].body, body);
  assert.equal(instances[0].headers.get("Upload-Offset"), "8");
  assert.equal(instances[0].headers.get("X-CSRF-Token"), "csrf");
  assert.equal(instances[0].headers.get("X-Obox-Request"), "true");
  assert.deepEqual(progress, [4]);
  assert.deepEqual(await response.json(), { offset: 4 });
});

for (const failure of ["onerror", "ontimeout"]) {
  test(`upload rejects ${failure}`, async (t) => {
    xhrMock(t, { failure });
    await assert.rejects(
      uploadRequest("/media/upload", new Blob(), { headers: {}, onProgress() {} }),
      /Upload interrupted/,
    );
  });
}

test("upload uses the same server-directed navigation contract", async (t) => {
  const destination = "/prefix/sign-in";
  const navigation = Promise.withResolvers();
  xhrMock(t, { status: 401, destination });
  globalThis.window = {
    location: {
      assign(url) {
        assert.equal(url, destination);
        navigation.resolve();
      },
    },
  };
  t.after(() => delete globalThis.window);
  let consumed = false;
  uploadRequest("/media/upload", new Blob(), { headers: {}, onProgress() {} }).then(() => {
    consumed = true;
  });
  await navigation.promise;
  assert.equal(consumed, false);
});
