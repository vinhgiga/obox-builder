import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = """
from typing import Any, assert_type
from obox.forms import Form, ModelForm, formset_factory, modelform_factory
from obox import ModelResource
from obox.views.generic import GridView, DetailView, UpdateView
from obox.views.generic.inlines import Inline
from pydantic import BaseModel, Field
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class ReviewInput(BaseModel):
    name: str
    rate: int = Field(ge=1, le=5)
    note: str | None = None

class Review(Form[ReviewInput]):
    schema = ReviewInput

class Base(DeclarativeBase):
    pass

class Record(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    rate: Mapped[int]
    note: Mapped[str | None]

class ReviewModel(ModelForm[ReviewInput, Record]):
    schema = ReviewInput

    class Meta:
        model = Record
        fields = ("name", "rate", "note")

class ReviewInline(Inline):
    model = Record
    form = ReviewModel

class ReviewResource(ModelResource[Record]):
    model = Record

class ReviewGrid(GridView[Record]):
    resource = ReviewResource

formset_factory(Review)
modelform_factory(Record, form=ReviewModel, fields=("name", "rate", "note"))

async def validate(raw: dict[str, Any]) -> None:
    form = Review(raw)
    if await form.is_valid() and form.cleaned_data is not None:
        assert_type(form.cleaned_data, ReviewInput)
        assert_type(form.cleaned_data.name, str)
        assert_type(form.cleaned_data.rate, int)
        assert_type(form.cleaned_data.note, str | None)

async def model_types(
    form: ReviewModel, detail: DetailView[Record], edit: UpdateView[Record]
) -> None:
    assert_type(form.instance, Record)
    assert_type(await form.save(), Record)
    assert_type(await detail.get_object(), Record)
    assert_type(await edit.get_object(), Record)
    assert_type(ReviewGrid().object_list[0], Record)
"""


@pytest.mark.parametrize("checker", ["mypy", "pyright"])
def test_schema_backed_form_type_inference(tmp_path: Path, checker: str) -> None:
    source = tmp_path / "form_types.py"
    command = [sys.executable, "-m", checker]
    if checker == "mypy":
        command += ["--strict", "--no-incremental"]
    source.write_text(SOURCE)
    result = subprocess.run([*command, str(source)], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(SOURCE + '\nReviewInput(name="test", rate="bad")\n')
    result = subprocess.run([*command, str(source)], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode != 0
