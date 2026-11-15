# Configure a list and filters

Add search and filter fields to the builder. Choose columns and a page size for
the admin list:

```python
from obox_builder import ModelBuilder, builder


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "pub_date")
    search_fields = ("question_text",)
    filter_fields = ("pub_date",)
    sort_fields = ("question_text", "pub_date")
    ordering = ("-pub_date", "-id")

    class View(builder.View):
        list_display = ("question_text", "pub_date")
        page_size = 50
```

The filter and sort declarations are allowlists. The policy still controls which
records and fields the current user may see. Set `View.related` to display
configured relationship cards on record pages.

See [Resource builders](../../features/resources.md) for API surface options.
