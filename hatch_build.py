"""Package content-addressed static assets in standard wheels."""

import json
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class StaticAssetsHook(BuildHookInterface[Any]):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        self.assets_directory = TemporaryDirectory(prefix="obox-assets-")
        output = Path(self.assets_directory.name)
        package = Path(self.root) / "obox_builder"
        collect = run_path(str(package / "_assets.py"))["collect_assets"]
        manifest = collect(package, output)
        (output / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True), encoding="utf-8"
        )
        build_data["force_include"][str(output)] = "obox_builder/_static"

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        if version != "editable":
            self.assets_directory.cleanup()
