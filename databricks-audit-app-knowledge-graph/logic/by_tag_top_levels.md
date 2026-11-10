# Top tag values per level (by_tag_top)

`/api/rollup/top` ranks the top values of one key PER LEVEL (query, warehouse, cluster, job, pipeline, workspace, billing line) and never merges levels; rows overlap by design, so only `tagged = total - untagged` reconciles.

```mermaid
flowchart LR
  P[same paths as the nested rollup] --> B{for each path}
  B -->|real compute value| C["bucket (warehouse | cluster | billing line, value)"]
  B -->|real work value, kind query/job/pipeline| W["bucket (query | job | pipeline, value)"]
  B -->|real workspace value| S["bucket (workspace, value)"]
  B -->|effective = __untagged__| U[untagged total]
  C & W & S --> R[rank by usd or duration_ms, top N, more_count]
```

## Rule in plain words
- Levels list - `app/core/rollup.py:45-49`; compute mapping warehouse/cluster else "billing line" - `app/core/rollup.py:1130`; work mapping - `app/core/rollup.py:1131`.
- One path adds to EVERY level where it has a real value - `app/core/rollup.py:1210-1224`.
- Rank: cost by `usd`, performance by `duration_ms` - `app/core/rollup.py:1163`, sort `app/core/rollup.py:1226`.
- `tagged = total - untagged`; `rows_overlap_across_levels: True` - `app/core/rollup.py:1228`, `app/core/rollup.py:1259-1263`.
- `more_count = groups - top` - `app/core/rollup.py:1256-1257`.

## Formula
```
row(level, value).usd = SUM(path.usd where path has real value at that level)
tagged = total - SUM(path.usd where effective == '__untagged__')
```

## Example (test, tg_cc, 30d)
- `(cluster, etl)` 20 and `(job, etl)` 20 are two rows - `tests/test_api_rollup.py:200-212`.
- alpha on three levels: workspace 362, warehouse 150, cluster 80, none the sum of the others - `tests/test_api_rollup.py:213-217`.
- Serverless budget policy tag `shared` reads `billing line` 15 - `tests/test_api_rollup.py:219-227`.
- top=5: untagged 50, `group_count` 15, `more_count` 10 - `tests/test_api_rollup.py:230-239`.
- Performance ranks by query time; workspace/alpha first - `tests/test_api_rollup.py:242-251`.

## Edge / rejected cases
- Idle/unsplit/none work kinds never create a work row - `app/core/rollup.py:1131`.
- Summing rows or taking shares of `total` overstates tagged spend - `app/core/rollup.py:1259-1262`.

## Implementing code
- `app/core/rollup.py:1146-1271`

## Config knobs
- `top` request param (default 10) - `app/core/rollup.py:1150`; `discount_pct` - `config/settings.yml:13-15`.
