import sys
from pathlib import Path
from types import ModuleType

import pytest
from obox.apps import AppConfig, Apps
from obox.conf import LazySettings
from obox.core.exceptions import AppRegistryNotReady, ImproperlyConfigured


def test_settings_are_lazy_uppercase_and_configure_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBOX_SETTINGS_MODULE", raising=False)
    settings = LazySettings()
    assert not settings.configured
    with pytest.raises(ImproperlyConfigured):
        _ = settings.DEBUG
    with pytest.raises(TypeError):
        settings.configure(debug=True)
    settings.configure(DEBUG=True, CUSTOM_VALUE=3)
    assert settings.DEBUG is True
    assert settings.CUSTOM_VALUE == 3
    with pytest.raises(RuntimeError):
        settings.configure()


def test_settings_module_loads_only_uppercase(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("v01_settings")
    module.DEBUG = True
    module.lowercase = "ignored"
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("OBOX_SETTINGS_MODULE", module.__name__)
    settings = LazySettings()
    assert settings.DEBUG is True
    with pytest.raises(AttributeError):
        _ = settings.lowercase


def test_registry_populates_config_models_ready_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "v01_app"
    package.mkdir()
    (package / "__init__.py").write_text("events = []\n")
    (package / "models.py").write_text(
        "from . import events\n"
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\n"
        "events.append('models')\n"
        "class Base(DeclarativeBase): pass\n"
        "class Item(Base):\n"
        "    __tablename__ = 'v01_items'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
    )
    (package / "apps.py").write_text(
        "from obox.apps import AppConfig\n"
        "from . import events\n"
        "events.append('config')\n"
        "class Config(AppConfig):\n"
        "    name = 'v01_app'\n"
        "    def ready(self):\n"
        "        assert self.apps.models_ready\n"
        "        assert self.get_model('Item')\n"
        "        events.append('ready')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = Apps()
    with pytest.raises(AppRegistryNotReady):
        registry.get_app_configs()
    registry.populate(["v01_app"])
    import v01_app

    assert v01_app.events == ["config", "models", "ready"]
    assert registry.get_app_config("v01_app").name == "v01_app"
    assert registry.get_model("v01_app.Item").__name__ == "Item"
    registry.populate(["v01_app"])
    assert v01_app.events == ["config", "models", "ready"]
    assert registry.is_installed("v01_app")
    explicit = AppConfig.create("v01_app.apps.Config")
    assert explicit.label == "v01_app"
    with pytest.raises(ImproperlyConfigured, match="unique"):
        Apps().populate(["v01_app", "v01_app"])
    for name in ("v01_app.apps", "v01_app.models", "v01_app"):
        monkeypatch.delitem(sys.modules, name)


def test_app_config_selects_the_single_explicit_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "v01_defaults"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "apps.py").write_text(
        "from obox.apps import AppConfig\n"
        "class First(AppConfig):\n    name='v01_defaults'\n"
        "class Chosen(AppConfig):\n    name='v01_defaults'\n    default=True\n"
        "class Disabled(AppConfig):\n    default=False\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = AppConfig.create("v01_defaults")
    assert type(config).__name__ == "Chosen"
    assert config.verbose_name == "V01 Defaults"
    assert config.path == str(package)
    from v01_defaults.apps import First

    monkeypatch.setattr(First, "default", True, raising=False)
    with pytest.raises(RuntimeError, match="More than one default"):
        AppConfig.create("v01_defaults")
    for name in ("v01_defaults.apps", "v01_defaults"):
        monkeypatch.delitem(sys.modules, name)
