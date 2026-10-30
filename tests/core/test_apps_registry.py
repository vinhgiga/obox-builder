from __future__ import annotations

# Registry lifecycle cases are adapted from Django's tests/apps/tests.py.
from pathlib import Path

import pytest
from obox_builder.apps import apps
from obox_builder.core.exceptions import ImproperlyConfigured

from tests.support import events

BLOG = "tests.support.apps.blog"
PAYMENTS = "tests.support.apps.payments"
EMPTY = "tests.support.apps.emptyapp"
BROKEN = "tests.support.apps.broken"
BAD_APPS = "tests.support.apps.bad_apps"
BAD_MODELS = "tests.support.apps.bad_models"
DUP_A = "tests.support.apps.dup_a"
DUP_B = "tests.support.apps.dup_b"


def test_populate_runs_three_phases_in_order() -> None:
    apps.populate([BLOG, PAYMENTS])
    assert events.RECORDS == ["blog.models", "payments.models", "blog.ready", "payments.ready"]
    assert apps.apps_ready
    assert apps.models_ready
    assert apps.ready
    assert [config.label for config in apps.get_app_configs()] == ["blog", "payments"]


def test_app_config_attributes() -> None:
    apps.populate([BLOG, EMPTY])
    blog = apps.get_app_config("blog")
    assert blog.name == BLOG
    assert blog.label == "blog"
    assert blog.verbose_name == "Blog"
    assert blog.path == Path(__file__).parent.parent / "support" / "apps" / "blog"
    assert not hasattr(blog, "permissions")

    empty = apps.get_app_config("emptyapp")
    assert empty.label == "emptyapp"
    assert empty.verbose_name == "Emptyapp"
    assert empty.path.name == "emptyapp"


def test_class_path_entry() -> None:
    apps.populate(["tests.support.apps.blog.apps.BlogConfig", PAYMENTS])
    blog = apps.get_app_config("blog")
    assert blog.name == BLOG
    assert blog.verbose_name == "Blog"


def test_duplicate_labels() -> None:
    with pytest.raises(ImproperlyConfigured, match="dup"):
        apps.populate([DUP_A, DUP_B])


def test_duplicate_app_names_are_rejected_even_with_distinct_labels() -> None:
    alias = f"{BLOG}.apps.BlogAliasConfig"
    with pytest.raises(ImproperlyConfigured, match="name.*used more than once"):
        apps.populate([BLOG, alias])


def test_unimportable_app() -> None:
    with pytest.raises(ImproperlyConfigured, match="Cannot import app"):
        apps.populate([BROKEN])


def test_broken_apps_module() -> None:
    with pytest.raises(ImproperlyConfigured, match="Error importing"):
        apps.populate([BAD_APPS])


def test_broken_models_module() -> None:
    with pytest.raises(ImproperlyConfigured, match="models for app"):
        apps.populate([BAD_MODELS])


def test_get_models_and_get_model() -> None:
    apps.populate([BLOG, PAYMENTS])
    assert sorted(model.__name__ for model in apps.get_models()) == ["Payment", "Post"]
    assert apps.get_model("blog", "Post").__name__ == "Post"
    assert apps.get_model("blog", "post").__name__ == "Post"
    with pytest.raises(LookupError):
        apps.get_model("blog", "Nope")
    with pytest.raises(LookupError):
        apps.get_app_config("nope")


def test_get_containing_app_config() -> None:
    apps.populate([BLOG, PAYMENTS])
    containing = apps.get_containing_app_config("tests.support.apps.blog.views.home")
    assert containing is not None
    assert containing.label == "blog"
    assert apps.get_containing_app_config("tests.support.apps.blogx") is None
    assert apps.get_containing_app_config("elsewhere.module") is None


def test_is_installed() -> None:
    apps.populate([BLOG])
    assert apps.is_installed(BLOG)
    assert not apps.is_installed(PAYMENTS)


def test_populate_twice_raises() -> None:
    apps.populate([BLOG])
    with pytest.raises(ImproperlyConfigured, match="once"):
        apps.populate([BLOG])


def test_access_before_populate_raises() -> None:
    with pytest.raises(ImproperlyConfigured, match="populate"):
        apps.get_models()
    with pytest.raises(ImproperlyConfigured, match="populate"):
        apps.get_app_config("blog")


def test_register_model_after_models_phase_raises() -> None:
    apps.populate([BLOG])

    class Late:
        pass

    with pytest.raises(ImproperlyConfigured, match="after the models phase"):
        apps.register_model("blog", Late)
