# Component query filters

Components published through `AppConfig.components` have a typed JSON query
route at `/api/{app_label}/components/{name}:query`. Template-rendered components
also use a private GET data route under the configured UI prefix. This page
describes the filter syntax on that GET route; it is not the `ModelApi` query
contract.

The GET route accepts query keys prefixed with the component name. Filter field
names use the component's public camelCase names. For example:

```text
?sales.status=eq.open&sales.amount=gte.10&sales.search=North
```

`search` is optional and searches the fields declared by the component. The
server owns component shape and ordering; `select`, `expand`, `order`, `limit`,
`offset`, `cursor` and `count` are not accepted on this route.

## Conditions and groups

A condition has the form `<field>=<operator>.<value>`. Use `not.` before an
operator to negate it. Supported operators are `eq`, `neq`, `gt`, `gte`, `lt`,
`lte`, `is`, `in`, `isdistinct`, `like` and `ilike`, subject to the field's
type. Range comparisons require numeric or date/time fields. `is` supports
`null` and boolean values; JSON fields cannot be filtered. Text `like` and
`ilike` accept `*` as a wildcard. `in` takes a parenthesized list.

```text
?sales.status=not.eq.closed
?sales.archived=is.true
?sales.amount=in.(10,20,30)
?sales.name=ilike.*north*
```

Use `and`, `or`, `not.and` or `not.or` for parenthesized groups. Groups can be
nested. Quote values containing commas, parentheses or quotes; inside quoted
values, escape a quote or backslash with a backslash.

```text
?sales.and=(amount.gte.10,amount.lte.100)
?sales.not.or=(status.eq.closed,and(amount.lt.10,amount.gt.100))
?sales.name=eq."North, West"
```

The complete filter tree is limited to depth 3 and 50 nodes, counting groups
and conditions. Each query key may appear only once. Filters are validated
against the component's declared, readable fields before execution.

Template component data routes require the app's
`{app_label}.components.access` permission and the component's own permission
and source-model policy. The typed JSON `:query` route accepts the component's
declared request model instead of this query-string grammar.
