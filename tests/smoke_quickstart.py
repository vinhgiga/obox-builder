"""Exercise the quickstart over HTTP from an installed wheel."""

import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx


def run(project: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=project, env=env, check=True)  # noqa: S603


def check_http(url: str, password: str) -> None:
    with httpx.Client(base_url=url) as client:
        home = client.get("/")
        collection = client.get("/products")
        for response in (home, collection):
            assert response.status_code == 303, response.text
            assert response.headers["location"].startswith("/auth/login?")

        login_path = collection.headers["location"]
        page = client.get(login_path)
        assert page.status_code == 200, page.text
        assert '<html lang="vi">' in client.get("/vi/auth/login").text
        field = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        assert field is not None
        session = client.post(
            login_path,
            data={"username": "admin", "password": password, "csrf_token": field[1]},
        )
        assert session.status_code == 303, session.text

        assert client.get("/products").status_code == 200
        form = client.get("/products/add")
        assert form.status_code == 200
        masked = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', form.text)
        assert masked is not None
        invalid = client.post("/products/add", data={"csrfmiddlewaretoken": masked[1]})
        assert invalid.status_code == 422, invalid.text
        stylesheet = re.search(r'href="([^"]+tabler\.min\.css)"', page.text)
        assert stylesheet is not None
        asset = client.get(stylesheet[1])
        assert asset.status_code == 200
        assert "immutable" in asset.headers["cache-control"]

        csrf = client.get("/v0/auth/csrf")
        csrf.raise_for_status()
        client.headers["X-CSRF-Token"] = csrf.json()["csrfToken"]
        product = client.post("/v0/product", json={"name": "Desk lamp", "price": "12.00"})
        assert product.status_code == 201, product.text
        pk = product.json()["id"]
        for action in ("view", "change", "delete"):
            assert client.get(f"/products/{pk}/{action}").status_code == 200
        assert client.get(f"/v0/product/{pk}").json()["name"] == "Desk lamp"
        assert client.get("/v0/products").status_code == 404
        assert client.get("/api/products").status_code == 404
        assert client.delete(f"/v0/product/{pk}").status_code == 204


def main() -> None:
    obox = shutil.which("obox", path=sysconfig.get_path("scripts"))
    assert obox is not None
    password = "quickstart-test-password-2026"  # noqa: S105
    docs = Path(__file__).resolve().parents[1] / "docs/examples/quickstart.md"
    model, registration = re.findall(r"```python\n(.*?)```", docs.read_text("utf-8"), re.S)
    with TemporaryDirectory(prefix="obox-quickstart-") as directory:
        root = Path(directory)
        run(root, obox, "startproject", "example")
        project = root / "example"
        run(project, sys.executable, "-m", "alembic", "upgrade", "head")
        products = project / "app" / "products"
        products.mkdir(parents=True)
        (products / "models.py").write_text(model, encoding="utf-8")
        for path, code in (
            (project / "settings.py", '\nINSTALLED_APPS.append("app.products")\n'),
            (project / "main.py", "\n" + registration),
        ):
            with path.open("a", encoding="utf-8") as file:
                file.write(code)
        run(project, sys.executable, "-m", "alembic", "revision", "--autogenerate", "-m", "product")
        run(project, sys.executable, "-m", "alembic", "upgrade", "head")
        run(
            project,
            obox,
            "createsuperuser",
            "--username",
            "admin",
            "--noinput",
            env={**os.environ, "OBOX_SUPERUSER_PASSWORD": password},
        )
        run(project, obox, "collectstatic", str(root / "static"))

        log = root / "uvicorn.log"
        with log.open("w", encoding="utf-8") as output:
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "main:app", "--port", "0", "--no-use-colors"],
                cwd=project,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            for _ in range(100):
                logs = log.read_text(encoding="utf-8")
                match = re.search(r"Uvicorn running on (http://127\.0\.0\.1:\d+)", logs)
                if match:
                    check_http(match[1], password)
                    return
                if server.poll() is not None:
                    break
                time.sleep(0.1)
            raise RuntimeError(f"The quickstart server failed to start.\n{logs}")
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
