async function receive(response) {
  const destination = response.headers.get("X-Obox-Redirect");
  if (destination) {
    window.location.assign(destination);
    // Navigation replaces this document; stop processing its response.
    await new Promise(() => {});
  }
  return response;
}

export async function request(url, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("X-Obox-Request", "true");
  return receive(await fetch(url, { credentials: "same-origin", ...options, headers }));
}

export function uploadRequest(url, body, { headers, onProgress }) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PATCH", url);
    xhr.timeout = 120000;
    xhr.setRequestHeader("X-Obox-Request", "true");
    for (const [name, value] of new Headers(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = onProgress;
    xhr.onload = async () => {
      try {
        const response = new Response(xhr.responseText, {
          status: xhr.status,
          headers: { "X-Obox-Redirect": xhr.getResponseHeader("X-Obox-Redirect") || "" },
        });
        resolve(await receive(response));
      } catch (error) {
        reject(error);
      }
    };
    xhr.onerror = xhr.ontimeout = () => reject(new Error("Upload interrupted."));
    xhr.send(body);
  });
}
