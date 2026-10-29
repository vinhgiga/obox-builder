"""Exercise an installed wheel outside the source checkout."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent

import obox_builder


def main() -> None:
    package = Path(obox_builder.__file__).resolve().parent
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    cli = Path(sys.executable).with_name("obox.exe" if os.name == "nt" else "obox")
    env = {key: value for key, value in os.environ.items() if not key.startswith("OBOX_")}
    env.pop("PYTHONPATH", None)
    with TemporaryDirectory(prefix="obox-wheel-") as directory:
        project = Path(directory) / "smoke"

        def run(*args: str, cwd: Path = project) -> None:
            subprocess.run(args, cwd=cwd, env=env, check=True, timeout=120)  # noqa: S603

        run(str(cli), "startproject", "smoke", cwd=Path(directory))
        assert (project / "config/asgi.py").is_file()
        assert (project / ".env.example").is_file()
        run(str(sys.executable), "manage.py", "version")
        run(str(sys.executable), "manage.py", "check")
        run(str(cli), "startapp", "blog", "apps/blog")
        assert (project / "apps/blog/tests/test_smoke.py").is_file()
        run(str(sys.executable), "manage.py", "collectstatic")
        for asset in (
            "admin/vendor/tabler/tabler.min.css",
            "admin/vendor/htmx-4.0.0.min.js",
            "admin/js/navigation.js",
        ):
            assert (project / "static" / asset).stat().st_size > 0, asset
        env["OBOX_SETTINGS_MODULE"] = "config.settings"
        run(
            str(sys.executable),
            "-c",
            dedent("""
                from obox_builder.core.application import get_asgi_application
                from obox_builder.templating import get_environment
                from obox_builder.contrib.i18n import gettext, override

                assert get_asgi_application().routes
                get_environment().get_template("admin/errors/403.html")
                get_environment().get_template("admin/model/list.html")
                with override("vi"):
                    assert gettext("Log in") == "Đăng nhập"
            """),
        )
    print("Installed wheel smoke test passed.")


if __name__ == "__main__":
    main()
