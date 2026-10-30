# Lakeview `.lvdash.json` format

Reverse-engineered from a real AI/BI dashboard exported from a Databricks workspace (28 datasets,
6 pages). Ids, dataset names, workspace names and queries below are placeholders; the colours are
Databricks' default theme. Field/column names that are
generic system-table columns (`usage_date`, `billing_origin_product`, ...) are Databricks' own
public schema, not anything workspace-specific.

Use this as ground truth for writing a dashboard generator: every shape here was read out of a
real export with `json.load`, not guessed from docs.

## Top level

```json
{
  "datasets": [ /* array, see below */ ],
  "pages": [ /* array, see below */ ],
  "uiSettings": { /* theme + feature flags, see below */ }
}
```

No other top-level keys were present. `datasets` and `pages` are required; every real export also
carries `uiSettings`, but nothing here confirms it's optional, so keep it.

```json
"uiSettings": {
  "theme": {
    "canvasBackgroundColor": { "light": "#FFFFFF", "dark": "#11171C" },
    "widgetBackgroundColor": { "light": "#FFFFFF", "dark": "#11171C" },
    "widgetBorderColor": { "light": "#FFFFFF", "dark": "#11171C" },
    "fontColor": { "light": "#11171C", "dark": "#E8ECF0" },
    "selectionColor": { "light": "#2272B4", "dark": "#8ACAFF" },
    "visualizationColors": ["#077A9D", "#FFAB00", "#00A972", "#FF3621", "#8BCAE7",
                             "#AB4057", "#99DDB4", "#FCA4A1", "#919191", "#BF7080"],
    "widgetHeaderAlignment": "LEFT"
  },
  "genieSpace": { "isEnabled": false, "enablementMode": "ENABLEMENT_MODE_UNSPECIFIED" },
  "applyModeEnabled": false
}
```

`visualizationColors` is a 10-slot palette. A widget's `mark.colors[]` or a color scale's
`mappings[].color` can reference a slot instead of a literal hex: `{"position": 2,
"themeColorType": "visualizationColors"}` (1-indexed).

## Datasets

```json
{
  "name": "ds_main",
  "displayName": "main_usage",
  "queryLines": [
    "SET ansi_mode = true;\n",
    "\n",
    "with source as (\n",
    "  select\n",
    "    entity_id,\n",
    "    entity_name,\n",
    "    usage_date,\n",
    "    usage_amount\n",
    "  from catalog.schema.usage_table\n",
    "  -- apply filters\n",
    "  where usage_date between :time_range.min and :time_range.max\n",
    "    and (array_contains(:category, entity_category) or array_contains(:category, 'all'))\n",
    ")\n",
    "select * from source"
  ],
  "parameters": [ /* optional, see Dataset parameters */ ],
  "columns": [ /* optional, see below */ ]
}
```

`queryLines` is simply the full SQL text split on `\n`: each array element is exactly one line of
the original query, **with its trailing `\n` kept**, including blank lines (an element that is just
`"\n"`) and comment-only lines. Concatenating the array reproduces the SQL byte-for-byte. A
generator does not need to match any particular wrapping — one element per logical clause is fine,
and a single-element array holding the whole query also imports — but splitting on real newlines
(not an arbitrary character count) is what the real exporter does.

`name` is the dataset's internal id. It only needs to be unique within the file; nothing requires
it to look like a hex id, and the workspace does not need to recognize it — widgets reference it
via `datasetName`.

`columns` (seen on 4 of 28 datasets) declares extra calculated fields on top of the query, each
`{"displayName", "description", "expression"}` with a plain (non-backtick) SQL expression over the
dataset's own output columns, e.g. `"expression": "AVG(usage_amount)"`. These don't require their
own backticks the way widget query `fields[].expression` does.

### Dataset parameters

```json
"parameters": [
  {
    "displayName": "time_range",
    "keyword": "time_range",
    "dataType": "DATE",
    "complexType": "RANGE",
    "defaultSelection": {
      "range": { "dataType": "DATE", "min": {"value": "now-30d/d"}, "max": {"value": "now/d"} }
    }
  },
  {
    "displayName": "category",
    "keyword": "category",
    "dataType": "STRING",
    "complexType": "MULTI",
    "defaultSelection": {
      "values": { "dataType": "STRING", "values": [ {"value": "all"} ] }
    }
  }
]
```

Observed `dataType` values: `STRING`, `DATE`, `INTEGER`, `DECIMAL`. `complexType` is omitted for a
single scalar value, `"MULTI"` for a multi-select array, `"RANGE"` for a min/max pair. Relative
dates use Databricks' `now`, `now-<n><unit>/<unit>` syntax (e.g. `now-12M/M`, `now-30d/d`).

In the SQL, a scalar/multi parameter is referenced as `:keyword` (a multi-value one is bound as an
array, used with `array_contains(:category, col)`); a RANGE parameter is referenced as
`:keyword.min` / `:keyword.max`. `IDENTIFIER(:keyword)` works too, for parameterizing a table name.

## Pages

Two `pageType` values were present: `PAGE_TYPE_CANVAS` (the normal pages, 5 of 6) and
`PAGE_TYPE_GLOBAL_FILTERS` (exactly one, holding only filter widgets that apply across every page).

```json
{
  "name": "page_overview",
  "displayName": "Overview",
  "pageType": "PAGE_TYPE_CANVAS",
  "layout": [
    { "widget": { "...": "text widget, see below" }, "position": {"x": 0, "y": 0, "width": 6, "height": 2} },
    { "widget": { "...": "bar widget, see below" }, "position": {"x": 0, "y": 2, "width": 6, "height": 8} }
  ]
}
```

```json
{
  "name": "page_filters",
  "displayName": "Global Filters",
  "pageType": "PAGE_TYPE_GLOBAL_FILTERS",
  "layoutVersion": "GRID_V1",
  "layout": [
    { "widget": { "...": "filter-date-range-picker" }, "position": {"x": 0, "y": 0, "width": 1, "height": 2} },
    { "widget": { "...": "filter-multi-select" }, "position": {"x": 1, "y": 0, "width": 1, "height": 2} }
  ]
}
```

### Layout grid

`position` is always `{x, y, width, height}` in grid cells, `y` growing downward, no gaps needed
between adjacent widgets. There are **two grid widths** in this file:

- pages with no `layoutVersion` key: legacy grid, **6 columns** wide (observed max `x + width` = 6
  on all 4 plain canvas pages).
- pages with `"layoutVersion": "GRID_V1"`: **12 columns** wide (observed max `x + width` = 12 on
  the README page).

Pick one grid per page and keep every widget's `x + width` within it; mixing widths on the same
page is what would overlap or leave dead space.

## Widgets

A `layout[].widget` is one of two shapes: a **text** widget (just `name` +
`multilineTextboxSpec`, no `spec`, no `queries` at all) or a **data** widget (`name` + `queries[]`
+ `spec`). Nothing else marks the difference, so a parser must check for `multilineTextboxSpec`
before assuming `spec.widgetType` exists.

### Text

```json
{
  "widget": {
    "name": "w_text_header",
    "multilineTextboxSpec": {
      "lines": ["# **Overview**\n", "---\n", "\n", "One line of context for this page."]
    }
  },
  "position": {"x": 0, "y": 0, "width": 6, "height": 2}
}
```

### Field naming convention (every data widget)

```json
"query": {
  "datasetName": "ds_main",
  "fields": [
    { "name": "entity_name", "expression": "`entity_name`" },
    { "name": "sum(usage_amount)", "expression": "SUM(`usage_amount`)" }
  ],
  "disaggregated": false
}
```

- A bare column: `name` is the plain column name, `expression` is the same name back-tick-quoted.
- An aggregate: `name` is the literal string `"<agg>(<column>)"` lower-cased
  (`"sum(usage_amount)"`), `expression` is the SQL (`"SUM(\`usage_amount\`)"`). `spec.encodings.*
  .fieldName` must match the `name` string **exactly**, parens and case included — this is the one
  key a widget silently fails to render correctly without, since it just won't find the field.
- `disaggregated: false` — the widget's own channel aggregates the query's rows (typical chart).
  `disaggregated: true` — rows pass through one-for-one (used when the dataset SQL already
  produced the final per-row values, e.g. a pre-pivoted table).

### Counter (v2)

```json
{
  "widget": {
    "name": "w_counter_total",
    "queries": [
      {
        "name": "main_query",
        "query": {
          "datasetName": "ds_main",
          "fields": [
            { "name": "time_window_string", "expression": "`time_window_string`" },
            { "name": "sum(usage_amount)", "expression": "SUM(`usage_amount`)" }
          ],
          "disaggregated": false
        }
      }
    ],
    "spec": {
      "version": 2,
      "widgetType": "counter",
      "encodings": {
        "value": {
          "fieldName": "sum(usage_amount)",
          "displayName": "Sum of usage_amount",
          "format": {
            "type": "number-currency", "currencyCode": "USD", "abbreviation": "none",
            "decimalPlaces": {"type": "max", "places": 0}
          },
          "style": {"bold": true, "fontSize": 32, "color": "#00A972"}
        },
        "target": {
          "fieldName": "time_window_string",
          "displayName": "time_window_string",
          "style": {"fontSize": 14, "italic": true, "color": {"themeColorType": "fontColor"}}
        }
      },
      "frame": {
        "title": "Total Usage", "showTitle": true,
        "description": "(# of usage periods)", "showDescription": false
      }
    }
  },
  "position": {"x": 2, "y": 2, "width": 2, "height": 2}
}
```

`encodings.target` is optional (a second, smaller line under the big number); `value` is required.

### Bar (v3) — categorical × quantitative, colored

```json
{
  "widget": {
    "name": "w_bar_by_category",
    "queries": [
      {
        "name": "main_query",
        "query": {
          "datasetName": "ds_tags",
          "fields": [
            { "name": "tag_key", "expression": "`tag_key`" },
            { "name": "sum(usage_amount)", "expression": "SUM(`usage_amount`)" },
            { "name": "group_label", "expression": "`group_label`" }
          ],
          "disaggregated": false
        }
      }
    ],
    "spec": {
      "version": 3,
      "widgetType": "bar",
      "encodings": {
        "x": {"fieldName": "sum(usage_amount)", "displayName": "Usage", "scale": {"type": "quantitative"}},
        "y": {"fieldName": "tag_key", "displayName": "Tag", "scale": {"type": "categorical", "sort": {"by": "x-reversed"}}},
        "color": {
          "fieldName": "group_label", "displayName": "Group",
          "legend": {"position": "bottom"}, "scale": {"type": "categorical"}
        },
        "label": {"show": false}
      },
      "frame": {"title": "Top Tags By Group", "showTitle": true}
    }
  },
  "position": {"x": 3, "y": 56, "width": 3, "height": 9}
}
```

**Stacked variant**: add a sibling `"mark": {"layout": "stack"}` (or `"percent-stack"` for a 100%
stacked bar) to `spec`, next to `encodings`/`frame`/`version`/`widgetType`. Nothing else changes —
stacking is driven by the `color` channel already present, `mark.layout` just picks how the
segments are laid out.

`x`/`y` are interchangeable for horizontal vs. vertical bars — whichever channel holds the
categorical field becomes that axis; a bar over time (`x` = `temporal`, `y` = `quantitative`) is
the same widget type, just with `x.scale.type: "temporal"`.

### Area / line over a date

```json
{
  "widget": {
    "name": "w_area_over_time",
    "queries": [
      {
        "name": "main_query",
        "query": {
          "datasetName": "ds_main",
          "fields": [
            { "name": "usage_type", "expression": "`usage_type`" },
            { "name": "time_key", "expression": "`time_key`" },
            { "name": "sum(usage_amount)", "expression": "SUM(`usage_amount`)" }
          ],
          "disaggregated": false
        }
      }
    ],
    "spec": {
      "version": 3,
      "widgetType": "area",
      "encodings": {
        "x": {"fieldName": "time_key", "displayName": "Time", "scale": {"type": "temporal"}},
        "y": {
          "fieldName": "sum(usage_amount)", "displayName": "Usage",
          "format": {"type": "number-currency", "currencyCode": "USD", "abbreviation": "compact",
                     "decimalPlaces": {"type": "max", "places": 2}},
          "scale": {"type": "quantitative"}
        },
        "color": {
          "fieldName": "usage_type", "displayName": "usage_type", "legend": {"hideTitle": true},
          "scale": {
            "type": "categorical",
            "mappings": [ {"value": "<series_a>", "color": "#077A9D"}, {"value": "<series_b>", "color": "#919191"} ]
          }
        }
      },
      "mark": {"colors": ["#077A9D", {"position": 2, "themeColorType": "visualizationColors"}]},
      "frame": {
        "title": "Total Usage Over Time", "showTitle": true,
        "description": "Does not cross-filter; use the global filters.", "showDescription": true
      }
    }
  },
  "position": {"x": 2, "y": 6, "width": 4, "height": 8}
}
```

A `line` widget is the same shape with `"widgetType": "line"`; a single-series line doesn't need a
`color` channel or `mark` at all — just `x` (temporal), `y` (quantitative), and optionally
`"label": {"show": false}`.

### Pie

Not present anywhere in the 6-page source file (no `"widgetType": "pie"` widget exists in this
export), so there is nothing here to copy from ground truth. Treat any pie-widget JSON as
unverified against a real export.

### Table (v1) — full columns encoding

```json
{
  "widget": {
    "name": "w_table_period",
    "queries": [
      {
        "name": "main_query",
        "query": {
          "datasetName": "ds_period_table",
          "fields": [
            { "name": "category", "expression": "`category`" },
            { "name": "trend_html", "expression": "`trend_html`" },
            { "name": "current_period", "expression": "`current_period`" },
            { "name": "prior_period", "expression": "`prior_period`" }
          ],
          "disaggregated": true
        }
      }
    ],
    "spec": {
      "version": 1,
      "widgetType": "table",
      "allowHTMLByDefault": false,
      "condensed": false,
      "withRowNumber": false,
      "itemsPerPage": 20,
      "paginationSize": "default",
      "encodings": {
        "columns": [
          {
            "fieldName": "category", "title": "Key", "type": "string", "displayAs": "string",
            "order": 0, "visible": true, "alignContent": "left",
            "allowHTML": true, "allowSearch": true, "useMonospaceFont": false,
            "preserveWhitespace": false, "highlightLinks": false,
            "booleanValues": ["false", "true"], "dateTimeFormat": "YYYY-MM-DD",
            "linkUrlTemplate": "{{ @ }}", "linkTextTemplate": "{{ @ }}", "linkTitleTemplate": "{{ @ }}",
            "linkOpenInNewTab": true, "imageUrlTemplate": "{{ @ }}", "imageTitleTemplate": "{{ @ }}",
            "imageWidth": "", "imageHeight": ""
          },
          {
            "fieldName": "trend_html", "title": "Trend", "type": "string", "displayAs": "string",
            "order": 1, "visible": true, "alignContent": "left",
            "allowHTML": true, "allowSearch": false, "useMonospaceFont": false,
            "preserveWhitespace": false, "highlightLinks": false,
            "booleanValues": ["false", "true"],
            "linkUrlTemplate": "{{ @ }}", "linkTextTemplate": "{{ @ }}", "linkTitleTemplate": "{{ @ }}",
            "linkOpenInNewTab": true, "imageUrlTemplate": "{{ @ }}", "imageTitleTemplate": "{{ @ }}",
            "imageWidth": "", "imageHeight": ""
          },
          {
            "fieldName": "current_period", "title": "Current period", "type": "string", "displayAs": "string",
            "order": 2, "visible": true, "alignContent": "center",
            "allowHTML": true, "allowSearch": false, "useMonospaceFont": false,
            "preserveWhitespace": false, "highlightLinks": false,
            "booleanValues": ["false", "true"], "dateTimeFormat": "YYYY-MM-DD",
            "linkUrlTemplate": "{{ @ }}", "linkTextTemplate": "{{ @ }}", "linkTitleTemplate": "{{ @ }}",
            "linkOpenInNewTab": true, "imageUrlTemplate": "{{ @ }}", "imageTitleTemplate": "{{ @ }}",
            "imageWidth": "", "imageHeight": ""
          },
          {
            "fieldName": "prior_period", "title": "Prior period", "type": "string", "displayAs": "string",
            "order": 3, "visible": true, "alignContent": "center",
            "allowHTML": true, "allowSearch": false, "useMonospaceFont": false,
            "preserveWhitespace": false, "highlightLinks": false,
            "booleanValues": ["false", "true"], "dateTimeFormat": "YYYY-MM-DD",
            "linkUrlTemplate": "{{ @ }}", "linkTextTemplate": "{{ @ }}", "linkTitleTemplate": "{{ @ }}",
            "linkOpenInNewTab": true, "imageUrlTemplate": "{{ @ }}", "imageTitleTemplate": "{{ @ }}",
            "imageWidth": "", "imageHeight": ""
          }
        ]
      },
      "invisibleColumns": [
        {
          "name": "_sort_key", "title": "_sort_key", "type": "integer", "displayAs": "number",
          "order": 4, "numberFormat": "0", "alignContent": "right",
          "allowHTML": false, "preserveWhitespace": false, "highlightLinks": false,
          "booleanValues": ["false", "true"], "useMonospaceFont": false,
          "linkUrlTemplate": "{{ @ }}", "linkTextTemplate": "{{ @ }}", "linkTitleTemplate": "{{ @ }}",
          "linkOpenInNewTab": true, "imageUrlTemplate": "{{ @ }}", "imageTitleTemplate": "{{ @ }}",
          "imageWidth": "", "imageHeight": ""
        }
      ],
      "frame": {"title": "Usage By Period", "showTitle": true, "showDescription": false}
    }
  },
  "position": {"x": 0, "y": 31, "width": 6, "height": 13}
}
```

Every column object — visible or invisible — carries the **full** attribute set above, even
columns that never use `link*`/`image*`/`boolean*`. The real exporter never omits any of them; a
hand-written generator should fill them all with the defaults shown rather than relying on which
ones Lakeview will default on import (unverified). Note the one asymmetry: a visible column is
keyed by `fieldName` + `visible`, an `invisibleColumns` entry is keyed by `name` instead of
`fieldName` and has no `visible` key at all.

## Filters

Two selection widgets had **no** `spec.selection` key at all in this file:
`filter-multi-select`. The three that did: `filter-single-select`, `filter-date-range-picker`,
`range-slider` — each via `spec.selection.defaultSelection` using the same `values`/`range` shapes
as a dataset parameter's `defaultSelection` (see above).

### Multi-select targeting one dataset (local filter)

When a multi-select only needs to filter the one dataset the chart on its page already queries, it
has a single query and filters that dataset's column directly — no dataset parameter involved:

```json
{
  "widget": {
    "name": "w_filter_local",
    "queries": [
      {
        "name": "dashboards/DASHBOARD_ID/datasets/QUERY_ID_entity_id",
        "query": {
          "datasetName": "ds_detail",
          "fields": [
            { "name": "entity_id", "expression": "`entity_id`" },
            { "name": "entity_id_associativity", "expression": "COUNT_IF(`associative_filter_predicate_group`)" }
          ],
          "disaggregated": false
        }
      }
    ],
    "spec": {
      "version": 2,
      "widgetType": "filter-multi-select",
      "encodings": {
        "fields": [
          { "fieldName": "entity_id", "displayName": "entity_id", "queryName": "dashboards/DASHBOARD_ID/datasets/QUERY_ID_entity_id" }
        ]
      },
      "frame": {"title": "Entity", "showTitle": true}
    }
  },
  "position": {"x": 2, "y": 6, "width": 2, "height": 2}
}
```

`COUNT_IF(\`associative_filter_predicate_group\`)` is a Lakeview cross-filter helper: request it
alongside the raw field so the picker can show live counts per option.

### Multi-select targeting several datasets (shared/global filter)

To make one filter reach N different datasets, give every target dataset a parameter with the
**same `keyword`**, then build the widget with one lookup query (for the picker's option list, can
point at any one convenient dataset) plus one parameter-set query per target dataset:

```json
{
  "widget": {
    "name": "w_filter_category_global",
    "queries": [
      {
        "name": "dashboards/DASHBOARD_ID/datasets/Q1_category",
        "query": {
          "datasetName": "ds_lookup",
          "fields": [
            { "name": "category", "expression": "`category`" },
            { "name": "category_associativity", "expression": "COUNT_IF(`associative_filter_predicate_group`)" }
          ],
          "disaggregated": false
        }
      },
      {
        "name": "parameter_dashboards/DASHBOARD_ID/datasets/Q2_category_param",
        "query": { "datasetName": "ds_main", "parameters": [ {"name": "category", "keyword": "category"} ], "disaggregated": false }
      },
      {
        "name": "parameter_dashboards/DASHBOARD_ID/datasets/Q3_category_param",
        "query": { "datasetName": "ds_tags", "parameters": [ {"name": "category", "keyword": "category"} ], "disaggregated": false }
      }
    ],
    "spec": {
      "version": 2,
      "widgetType": "filter-multi-select",
      "encodings": {
        "fields": [
          { "fieldName": "category", "displayName": "category", "queryName": "dashboards/DASHBOARD_ID/datasets/Q1_category" },
          { "parameterName": "category", "queryName": "parameter_dashboards/DASHBOARD_ID/datasets/Q2_category_param" },
          { "parameterName": "category", "queryName": "parameter_dashboards/DASHBOARD_ID/datasets/Q3_category_param" }
        ]
      },
      "frame": {"title": "Category", "showTitle": true}
    }
  },
  "position": {"x": 0, "y": 2, "width": 1, "height": 2}
}
```

The real export (11-dataset version of this pattern) repeats the parameter-query block once per
target dataset — there is no batching, it's one query + one `encodings.fields[]` entry per
dataset, every time. `spec.encodings.fields[0]` (the lookup field) has `fieldName`+`displayName`;
every entry after it has only `parameterName` (no `fieldName`/`displayName`) — that's the signal
that it's a parameter target rather than the option-list source.

### Date range picker / range slider

```json
{
  "widget": {
    "name": "w_filter_date_range",
    "queries": [ /* one parameter-set query per target dataset, same shape as above */ ],
    "spec": {
      "version": 2,
      "widgetType": "filter-date-range-picker",
      "encodings": { "fields": [ /* one {parameterName, queryName} per dataset */ ] },
      "frame": {"title": "Time Window", "showTitle": true},
      "selection": {
        "defaultSelection": { "range": {"dataType": "DATE", "min": {"value": "now-30d/d"}, "max": {"value": "now/d"}} }
      }
    }
  },
  "position": {"x": 0, "y": 0, "width": 1, "height": 2}
}
```

```json
{
  "widget": {
    "name": "w_filter_range_slider",
    "queries": [
      {
        "name": "dashboards/DASHBOARD_ID/datasets/QUERY_ID_days_since",
        "query": {
          "datasetName": "ds_detail",
          "fields": [
            { "name": "min(days_since_last_use)", "expression": "MIN(`days_since_last_use`)" },
            { "name": "max(days_since_last_use)", "expression": "MAX(`days_since_last_use`)" }
          ],
          "disaggregated": false
        }
      }
    ],
    "spec": {
      "version": 2,
      "widgetType": "range-slider",
      "encodings": {
        "fields": [
          { "fieldName": "days_since_last_use", "displayName": "days_since_last_use", "queryName": "dashboards/DASHBOARD_ID/datasets/QUERY_ID_days_since" }
        ]
      },
      "frame": {"title": "Days Since Last Use", "showTitle": true, "description": "Filter out stale rows", "showDescription": true},
      "selection": {
        "defaultSelection": { "range": {"dataType": "INTEGER", "min": {"value": ""}, "max": {"value": "30"}} }
      }
    }
  },
  "position": {"x": 0, "y": 6, "width": 2, "height": 2}
}
```

A `range-slider`'s lookup query uses `MIN(...)`/`MAX(...)` to size the slider's bounds from live
data, rather than a distinct-values list.

## Minimum keys an import needs

- Top level: `datasets`, `pages` (both arrays).
- Each dataset: `name` (unique within the file), `displayName`, `queryLines` (array of strings).
- Each page: `name`, `displayName`, `layout` (array); `pageType` was always set explicitly on every
  page in the source file. `layoutVersion: "GRID_V1"` only if the page needs the 12-column grid.
- Each `layout[]` entry: `widget` + `position` (`x`,`y`,`width`,`height`, all integers).
- A **text** widget: `widget.name` + `widget.multilineTextboxSpec.lines[]` — nothing else.
- A **data** widget: `widget.name`, `widget.queries[]` (each `name` + `query.datasetName` + either
  `fields[]` or `parameters[]` + `disaggregated`), `widget.spec.version`, `widget.spec.widgetType`,
  `widget.spec.encodings`. `spec.frame` carries the title but every real widget has one, so treat
  it as required too rather than relying on an unverified default.
- Every `spec.encodings.<channel>.fieldName` must exactly match some query's `fields[].name` (or,
  for a filter, a `parameterName` must match a `parameters[].name`/`keyword` the target dataset(s)
  declare) — this is the one mismatch that fails silently (empty widget) rather than erroring.
- Internal ids (`dataset.name`, `widget.name`, query `name` strings) only need to be unique and
  self-consistent within the file; they do not need to look like the hex ids the real UI
  generates, and nothing about them needs to match anything already in the target workspace.

## Importing a `.lvdash.json`

**UI** (verified — docs.databricks.com/aws/en/dashboards/automate/import-export): from the
dashboards list, the caret menu → "Import dashboard from file" → choose the `.lvdash.json` → it's
saved into the user's own folder, auto-renamed on a name collision. To replace an existing draft
instead: open it → kebab menu → File actions → Replace → choose file → Overwrite.

**REST API** (verified — docs.databricks.com/api/lakeview and the dashboard-crud-api tutorial):
`POST /api/2.0/lakeview/dashboards` with body `{"display_name", "warehouse_id", "parent_path",
"serialized_dashboard": "<the whole .lvdash.json file, as one JSON string>"}` creates a new draft
and returns a `dashboard_id`. `PATCH /api/2.0/lakeview/dashboards/{dashboard_id}` updates one
in place (can pass `etag` to guard against a stale overwrite). `POST
/api/2.0/lakeview/dashboards/{dashboard_id}/published` with `{"embed_credentials", "warehouse_id"}`
publishes it. The generic `POST /api/2.0/workspace/import` (base64 content, path ending in
`.lvdash.json`) is also mentioned on the import/export doc page as a second, more generic route,
distinct from the Lakeview-specific endpoint above.

**Databricks CLI** (verified — docs.databricks.com/aws/en/dev-tools/cli/reference/dashboards-commands):
`databricks lakeview create --display-name <name> --warehouse-id <id> --serialized-dashboard
@path/to/file.lvdash.json` (or `--json @...`); `databricks lakeview update DASHBOARD_ID ...` to
replace; `databricks lakeview publish DASHBOARD_ID --embed-credentials --warehouse-id <id>` to
publish; `databricks lakeview get DASHBOARD_ID` / `get-published` / `list` / `unpublish` / `trash`
round it out. No dedicated "import a file" subcommand — `create`/`update` take the serialized JSON
directly via `--json`/`--serialized-dashboard`.

**Databricks Asset Bundles** (verified — learn.microsoft.com mirror of
dev-tools/bundles/resources, which rendered where docs.databricks.com did not):

```yaml
resources:
  dashboards:
    overview:
      display_name: "Lakehouse Health 01 - Overview"
      file_path: "../databricks_dashboards/Lakehouse Health 01 - Overview.lvdash.json"
      warehouse_id: ${var.warehouse_id}
      embed_credentials: false
```

Documented fields: `display_name`, `file_path` (local path, must end `.lvdash.json`),
`warehouse_id`, `parent_path`, `path`, `serialized_dashboard`, `etag`, `permissions`, `lifecycle`,
and (CLI ≥ 0.283.0) `dataset_catalog`/`dataset_schema` to default every dataset's catalog/schema at
deploy time without editing the file. **`embed_credentials`** (boolean, default `false`): when
`true`, the bundle's deployment identity's credentials run every viewer's queries; when `false`
(the default), each viewer's own credentials and permissions apply. A community example
(sunnydata.ai) additionally showed `dataset_catalog`/`dataset_schema` set from bundle variables
directly on the dashboard resource — consistent with the official field table but not itself an
official source, so treat that one example as plausible, not verified.

## Sources

- https://docs.databricks.com/api/lakeview/v1/dashboard
- https://docs.databricks.com/aws/en/dashboards/tutorials/dashboard-crud-api
- https://docs.databricks.com/aws/en/dashboards/automate/import-export
- https://docs.databricks.com/aws/en/dev-tools/cli/reference/dashboards-commands
- https://learn.microsoft.com/en-us/azure/databricks/dev-tools/bundles/resources
- https://www.sunnydata.ai/blog/databricks-dashboard-cicd-deployment-guide (community example, not official)
