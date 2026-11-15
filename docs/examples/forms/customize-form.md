# Customize a form

Use fieldsets to group controls and labels or help text to clarify inputs:

```python
from obox_builder import ModelBuilder, builder


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "pub_date", "notes")

    class Form(builder.Form):
        labels = {"question_text": "Question"}
        help_texts = {"pub_date": "Choose when this question is published."}
        fieldsets = (
            ("Question", {"fields": ("question_text",)}),
            ("Publishing", {"fields": ("pub_date", "notes")}),
        )
```

The form only renders fields writable for the current operation and principal.
Use `Form.validate()` for form-only normalization or validation; shared domain
rules belong in the resource hooks so API writes receive them too.
