# Component query filters

Components published through `AppConfig.components` have a typed JSON `POST`
query route at `/v0/{app_label}/components/{name}:query`. Template-rendered
components also use a private GET data route under the configured UI prefix.
The two routes accept different filter formats.

## Typed JSON route

The `:query` route accepts the component's declared request model. The base
`ComponentQuery` body is a list of `[field, expression]` pairs:

```json
{
  "query": [
    ["regionName", "eq.East"],
    ["amount", "gte.10"],
    ["search", "north"]
  ]
}
```

Field names use the component's public camelCase names. An expression is
`<operator>.<value>`, optionally prefixed with `not.`. Supported operators are
`eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `is`, `in`, `isdistinct`, `like` and
`ilike`, subject to the field's type. Range comparisons require numeric or
date/time fields. `is` supports `null` and boolean values. Text `like` and
`ilike` accept `*` as a wildcard. `in` takes a parenthesized list.

```json
{"query": [["status", "not.eq.closed"], ["archived", "is.true"], ["amount", "in.(10,20,30)"]]}
```

Use `and`, `or`, `not.and` or `not.or` keys with parenthesized values for
groups. Groups can be nested. Quote values containing commas, parentheses or
quotes; inside quoted values, escape a quote or backslash with a backslash.

```json
{"query": [["and", "(amount.gte.10,amount.lte.100)"]]}
{"query": [["not.or", "(status.eq.closed,and(amount.lt.10,amount.gt.100))"]]}
```

The complete filter tree is limited to depth 3 and 50 nodes, counting groups
and conditions. Filters are validated against the component's declared,
readable fields before execution. `search` is optional; `select`, `expand`,
`order`, `limit`, `offset`, `cursor` and `count` are rejected because component
shape and ordering are defined on the server.

## Template data route

Template-rendered components expose
`GET {ui_prefix}/{app_label}/_components/{key}/data`. Query keys are prefixed
with the component key. Use `filter.<field>` for the value, `filter_op.<field>`
for the operator, and `search` for the component's declared search fields:

```text
?sales.filter.status=open&sales.filter_op.status=is&sales.filter.amount=10&sales.search=North
```

The supported template operators are `is`, `is_not`, `contains`,
`does_not_contain`, `is_empty` and `is_not_empty`. Attachment fields support
only `is_empty` and `is_not_empty`. An optional `{key}.filter` key accepts a
JSON filter tree with `conjunction` (`and` or `or`) and `children` entries of
`{"field", "op", "value"}` or nested groups. Each query key may appear only
once, and `ordering` is rejected on this route.

Template component data routes require the app's `{app_label}.components.access`
permission and the component's own permission and source-model policy.
