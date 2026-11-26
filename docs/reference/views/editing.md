# Editing views

`FormView`, `CreateView`, `UpdateView`, and `DeleteView` handle HTML input.
Their validation and model persistence use the asynchronous form APIs described
in [Forms](../forms.md).

## `FormMixin`

| Attribute or method | Default | Description |
| --- | --- | --- |
| `form_class` | `None` | Form class to instantiate. |
| `schema` | `None` | Pydantic schema used when a form class is not supplied. |
| `initial` | `{}` | Initial field values, copied for each request. |
| `prefix` | `None` | Prefix for submitted field names. |
| `success_url` | `None` | Path, absolute URL, or named route used after a valid form. |
| `invalid_status_code` | `200` | Status returned when rendering invalid input. |
| `get_initial()` | — | Return initial values for the request. |
| `get_prefix()` | — | Return the form prefix. |
| `get_form_class()` | — | Return the configured class. |
| `get_form_kwargs()` | — | Build constructor arguments, including submitted form data. |
| `get_form()` | — | Construct and prepare the form. |
| `get_success_url()` | — | Resolve the configured success target. |
| `form_valid(form)` | — | Handle valid input; the base implementation redirects with status `303`. |
| `form_invalid(form)` | — | Render the form and errors. |

## `FormView`

`FormView` renders a form on `GET`. On `POST` or `PUT`, it prepares the form,
awaits `form.is_valid()`, then calls `form_valid()` or `form_invalid()`. The base
`form_valid()` redirects; override it to run application behavior or persist
data. A `success_url` is required unless the view supplies its own success
handling.

```python
class ContactView(FormView):
    form_class = ContactForm
    template_name = "contact.html"
    success_url = "contact-thanks"

    async def form_valid(self, form):
        await send_contact_message(form.cleaned_data)
        return await super().form_valid(form)
```

Invalid forms render with status `200` by default. Set `invalid_status_code` to
change that status.

## `ModelFormMixin`

`ModelFormMixin` connects a form to a SQLAlchemy model or shared `ModelResource`.

| Attribute or method | Default | Description |
| --- | --- | --- |
| `fields` | `None` | Model fields used to build a form when `form_class` is unset. |
| `fieldsets` | `()` | `(title, options)` groups used by the generic form template. `options` accepts `fields` and optional `description`. |
| `inlines` | `()` | Inline form declarations used on create and update views. |
| `get_form_class()` | — | Build a `ModelForm` from the model/resource schema, or return `form_class`. `fields` and `form_class` cannot both be set. |
| `get_form_kwargs()` | — | Adds `instance` and `resource` to the form arguments. |
| `get_form()` | — | Checks resource write permissions and marks restricted fields disabled. |
| `save_model(request, obj, form, change)` | — | Async hook called after saving the primary record. |
| `save_related(request, form, formsets, change)` | — | Async hook called to save inline formsets. |
| `save_formset(request, form, formset, change)` | — | Async hook for saving one inline formset. |
| `get_success_url()` | — | Uses `success_url`, the registered list route, or `get_absolute_url()` when available. |

With a `ModelResource`, the generated model form uses the resource's create/update
schema and writable fields. The form and related formsets save within a
transactional savepoint. ModelResource validation errors are returned to the form.

## `CreateView` and `UpdateView`

`CreateView` supplies a new model instance and the `create` resource action.
`UpdateView` loads the existing object using `SingleObjectMixin` and uses the
`update` action. Both accept `model` or `resource`, plus either `form_class` or
`fields` and an optional `schema`.

If `template_name` is unset, both try `<app>/<model>_form.html`, then
`obox/generic/form.html`. Define `success_url` if no list route or
`get_absolute_url()` is available.

## `DeleteView`

`DeleteView` displays a confirmation on `GET`. A valid `POST` or `DELETE`
removes the record through its `ModelResource` and redirects with status `303`.
ModelResource deletion rules are checked before the record is removed. It uses the
configured `success_url` or the registered list route; a missing redirect
target is a configuration error.

Without an explicit `template_name`, it tries
`<app>/<model>_confirm_delete.html`, then
`obox/generic/confirm_delete.html`.

## Inline declarations

Use `StackedInline` or `TabularInline` in a model form view's `inlines` sequence.
An inline model must have one foreign key to its parent; set `fk_name` when the
relationship is ambiguous.

| Attribute or hook | Default | Description |
| --- | --- | --- |
| `model` | required | Related SQLAlchemy model. |
| `fk_name` | `None` | Foreign-key field or relationship joining the inline to its parent. |
| `form`, `formset` | `ModelForm`, `BaseInlineFormSet` | Classes used to build and validate the inline forms. |
| `fields`, `exclude` | `None` | Fields to show or omit. |
| `readonly_fields` | `()` | Fields displayed without editable controls. |
| `ordering` | `()` | Related records ordering; `-field` sorts descending. |
| `extra`, `min_num`, `max_num` | `3`, `0`, `None` | Form count limits. |
| `can_delete` | `True` | Include deletion controls for existing rows. |
| `get_queryset(request)` | — | Return the related-record `Select`. |
| `get_fields(request, obj=None)` | — | Choose the fields for this request and parent object. |
| `has_add_permission`, `has_change_permission`, `has_view_permission`, `has_delete_permission` | model permissions | Override per-request inline authorization. |

`TabularInline` uses `obox/generic/tabular.html`; `StackedInline` uses
`obox/generic/stacked.html`.
