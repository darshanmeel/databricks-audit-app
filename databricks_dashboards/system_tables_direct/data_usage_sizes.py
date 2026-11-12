# Databricks notebook source
# MAGIC %md
# MAGIC # Collect table and volume sizes
# MAGIC DESCRIBE DETAIL and DESCRIBE HISTORY on every Delta table and a file listing of every volume the
# MAGIC job can read, many at once. Writes `table_sizes`, `table_history` and `volume_sizes`, read by the
# MAGIC "Data usage" dashboard. Empty `catalogs` = every catalog.

# COMMAND ----------

dbutils.widgets.text("sizes_schema", "main.data_usage")
dbutils.widgets.text("catalogs", "")
dbutils.widgets.text("threads", "16")
dbutils.widgets.text("max_tables", "20000")
dbutils.widgets.text("max_entries_per_volume", "50000")

# COMMAND ----------

import datetime
import os
from concurrent.futures import ThreadPoolExecutor

out = dbutils.widgets.get("sizes_schema")
threads = int(dbutils.widgets.get("threads"))
max_tables = int(dbutils.widgets.get("max_tables"))
cap = int(dbutils.widgets.get("max_entries_per_volume"))
now = datetime.datetime.utcnow()
cats = [c.strip() for c in dbutils.widgets.get("catalogs").split(",") if c.strip()] or [
    r.catalog_name for r in spark.sql("SELECT catalog_name FROM system.information_schema.catalogs "
                                      "WHERE catalog_name NOT IN ('system', 'samples', '__databricks_internal') AND NOT startswith(catalog_name, '__databricks_internal')").collect()]
q = lambda s: "`" + s.replace("`", "``") + "`"
print(f"{len(cats)} catalogs")

# COMMAND ----------

tables = spark.sql(
    "SELECT table_catalog, table_schema, table_name FROM system.information_schema.tables "
    "WHERE table_type <> 'VIEW' AND (data_source_format IS NULL OR data_source_format = 'DELTA') "
    "AND table_schema <> 'information_schema' AND array_contains(:cats, table_catalog) "
    "ORDER BY last_altered DESC NULLS LAST LIMIT :n",  # the most recently changed first
    args={"cats": cats, "n": max_tables}).collect()

ROW_KEYS = ("numTargetRowsInserted", "numTargetRowsUpdated", "numTargetRowsDeleted", "numDeletedRows")
history_rows = []

def describe(t):
    name = ".".join(q(x) for x in (t.table_catalog, t.table_schema, t.table_name))
    try:
        d = spark.sql(f"DESCRIBE DETAIL {name}").collect()[0]
    except Exception as e:  # no access: kept with its error
        return (t.table_catalog, t.table_schema, t.table_name, None, None, None, "failed: " + str(e).splitlines()[0][:200], now)
    try:  # the last 100 versions: what changed, when, how many rows
        for h in spark.sql(f"DESCRIBE HISTORY {name} LIMIT 100").collect():
            m = {k: int(v) for k, v in (h.operationMetrics or {}).items() if str(v).isdigit()}
            changed = sum(m.get(k, 0) for k in ROW_KEYS) or m.get("numOutputRows", 0)
            first = lambda *ks: next((m[k] for k in ks if k in m), None)  # noqa: E731
            history_rows.append((t.table_catalog, t.table_schema, t.table_name, h.version, h.timestamp, h.operation,
                                 h.userName, changed, m.get("numOutputBytes", 0),
                                 first("numAddedFiles", "numFilesAdded", "numTargetFilesAdded", "numFiles"),
                                 first("numRemovedFiles", "numFilesRemoved", "numTargetFilesRemoved", "numDeletedFiles", "numFilesToDelete"),
                                 first("numAddedBytes", "numTargetBytesAdded", "numOutputBytes"),
                                 first("numRemovedBytes", "numTargetBytesRemoved"),
                                 m.get("sizeOfDataToDelete") if str(h.operation).startswith("VACUUM") else None))
    except Exception:  # the size is still worth keeping
        pass
    return (t.table_catalog, t.table_schema, t.table_name, d.sizeInBytes, d.numFiles, str(d.lastModified), "described", now)

with ThreadPoolExecutor(threads) as pool:
    rows = list(pool.map(describe, tables))
(spark.createDataFrame(rows, "catalog string, schema string, table_name string, size_in_bytes bigint, num_files bigint, "
                             "last_modified string, detail string, collected_at timestamp")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{out}.table_sizes"))
(spark.createDataFrame(history_rows, "catalog string, schema string, table_name string, version bigint, timestamp timestamp, "
                                     "operation string, user_name string, rows_changed bigint, bytes_written bigint, "
                                     "files_added bigint, files_removed bigint, bytes_added bigint, bytes_removed bigint, "
                                     "freed_bytes bigint")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{out}.table_history"))
print(f"{len(rows)} tables, {sum(1 for r in rows if r[6] == 'described')} described, {len(history_rows)} history rows")

# COMMAND ----------

volumes = spark.sql(
    "SELECT volume_catalog, volume_schema, volume_name FROM system.information_schema.volumes "
    "WHERE array_contains(:cats, volume_catalog)", args={"cats": cats}).collect()

def walk(v):
    root = f"/Volumes/{v.volume_catalog}/{v.volume_schema}/{v.volume_name}/"
    files = size = seen = 0
    todo, cut = [root], False
    try:
        while todo and not cut:
            # os.scandir on the /Volumes mount: dbutils.fs.ls starts a Spark job per folder on serverless
            with os.scandir(todo.pop(0)) as entries:
                for f in entries:
                    seen += 1
                    if f.is_dir(follow_symlinks=False):
                        todo.append(f.path)
                    else:
                        files += 1
                        size += f.stat(follow_symlinks=False).st_size
                    if seen >= cap:
                        cut = True
                        break
        return (v.volume_catalog, v.volume_schema, v.volume_name, files, size, cut, "listed", now)
    except Exception as e:
        return (v.volume_catalog, v.volume_schema, v.volume_name, None, None, False, "failed: " + str(e).splitlines()[0][:200], now)

with ThreadPoolExecutor(threads) as pool:
    vrows = list(pool.map(walk, volumes))
(spark.createDataFrame(vrows, "catalog string, schema string, volume_name string, files bigint, size_in_bytes bigint, "
                              "truncated boolean, detail string, collected_at timestamp")
      .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{out}.volume_sizes"))
print(f"{len(vrows)} volumes")
