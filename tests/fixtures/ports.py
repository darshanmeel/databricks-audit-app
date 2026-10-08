"""tests/fixtures/ports.py -- batch G (T-11), fixture builders for the two non-billing ports
(compute_serving_dormant_endpoints and storage_target_table_discovery).

Per DEC-15, every id this file writes carries the `pt_` prefix (served_entity_id, endpoint_id,
table_catalog/table_schema/table_name) so assertions filter on ports.py's own ids rather than on
workspace alone. These do not collide with T-22's later serving_storage.py.

Fills:
  system.serving.served_entities (for compute_serving_dormant_endpoints)
  system.serving.endpoint_usage (for compute_serving_dormant_endpoints)
  system.information_schema.tables (for storage_target_table_discovery)

Per PLAN.md 7.3, T-11 / compute_serving_dormant_endpoints needs:
  - At least one entity with total_requests >= :warn_low_requests (10) -> OK
  - One with 1-9 total_requests -> WARN
  - One with 0 requests / no endpoint_usage rows -> CRITICAL
  - One whose latest_change_time falls inside the :period_days (30 day) window -> NOT_ASSESSED
  Four distinct served_entity_id rows minimum.

Per PLAN.md 7.3, T-11 / storage_target_table_discovery needs:
  - Several rows spanning table_type values
  - At least one 'VIEW' row (excluded by the query's WHERE table_type <> 'VIEW')
  - At least two non-VIEW rows across different table_catalog/table_schema
"""
from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dtime

import duckdb

from base import AS_OF, write_parquet  # noqa: F401

D0 = AS_OF.date()  # 2026-09-21


def D(n: int):
    """D(n) = D0 - n days."""
    return D0 - timedelta(days=n)


def DT(n: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """A full datetime at D(n) (n days before D0) with the given time-of-day -- every timestamp
    column this file writes stays AS_OF-relative (never an absolute literal)."""
    return datetime.combine(D(n), dtime(hour, minute, second))


# Shared fixture workspaces (per DEC-15, billing.py defines these).
WS_PROD = "1111"
WS_DEV = "2222"

# Account ID for serving entities.
ACCOUNT_ID = "pt_acct"


def build(con: duckdb.DuckDBPyConnection) -> None:
    """Build fixture tables for compute_serving_dormant_endpoints and storage_target_table_discovery.

    The connection passed in already has all 47 source tables created from ddl.py's DDL (see
    tests/fixtures/build_fixtures.py); this builder just fills serving__served_entities,
    serving__endpoint_usage, and information_schema__tables with test data."""

    # =========================================================================
    # Build system.serving.served_entities (for compute_serving_dormant_endpoints)
    # =========================================================================
    # Four rows minimum, one per status band: OK, WARN, CRITICAL, NOT_ASSESSED.
    # latest_change_time controls NOT_ASSESSED (>= D0 - period_days, at any of 7/30/90).

    con.execute(
        """
        INSERT INTO serving__served_entities
        (
            account_id, workspace_id, served_entity_id, endpoint_id, endpoint_name, endpoint_config_version,
            served_entity_name, entity_type, entity_name, entity_version, task, external_model_config,
            foundation_model_config, custom_model_config, feature_spec_config, created_by, change_time, endpoint_delete_time
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # OK: entity with total_requests >= 10. change_time is well outside every window
            # (7/30/90 days) so this entity is never NOT_ASSESSED -- only its request volume
            # (see endpoint_usage below) decides its status.
            ACCOUNT_ID, WS_PROD, "pt_se_ok", "pt_ep_ok", "endpoint-ok", 1,
            "entity-ok", "model", "model-ok", "1.0.0", None, None, None, None, None,
            "pt_creator_ok", DT(100), None,
        ],
    )

    con.execute(
        """
        INSERT INTO serving__served_entities
        (
            account_id, workspace_id, served_entity_id, endpoint_id, endpoint_name, endpoint_config_version,
            served_entity_name, entity_type, entity_name, entity_version, task, external_model_config,
            foundation_model_config, custom_model_config, feature_spec_config, created_by, change_time, endpoint_delete_time
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # WARN: entity with 1-9 total_requests. change_time is well outside every window
            # (7/30/90 days), same reasoning as pt_se_ok above.
            ACCOUNT_ID, WS_DEV, "pt_se_warn", "pt_ep_warn", "endpoint-warn", 1,
            "entity-warn", "model", "model-warn", "1.0.0", None, None, None, None, None,
            "pt_creator_warn", DT(100), None,
        ],
    )

    con.execute(
        """
        INSERT INTO serving__served_entities
        (
            account_id, workspace_id, served_entity_id, endpoint_id, endpoint_name, endpoint_config_version,
            served_entity_name, entity_type, entity_name, entity_version, task, external_model_config,
            foundation_model_config, custom_model_config, feature_spec_config, created_by, change_time, endpoint_delete_time
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # CRITICAL: entity with zero requests (no endpoint_usage rows). change_time is well
            # outside every window (7/30/90 days), same reasoning as pt_se_ok above.
            ACCOUNT_ID, WS_PROD, "pt_se_crit", "pt_ep_crit", "endpoint-crit", 1,
            "entity-crit", "model", "model-crit", "1.0.0", None, None, None, None, None,
            "pt_creator_crit", DT(100), None,
        ],
    )

    con.execute(
        """
        INSERT INTO serving__served_entities
        (
            account_id, workspace_id, served_entity_id, endpoint_id, endpoint_name, endpoint_config_version,
            served_entity_name, entity_type, entity_name, entity_version, task, external_model_config,
            foundation_model_config, custom_model_config, feature_spec_config, created_by, change_time, endpoint_delete_time
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            # NOT_ASSESSED: entity created/reconfigured inside every window this suite exercises
            # (7/30/90 days) -- change_time = D(1) is inside all three.
            ACCOUNT_ID, WS_DEV, "pt_se_new", "pt_ep_new", "endpoint-new", 1,
            "entity-new", "model", "model-new", "1.0.0", None, None, None, None, None,
            "pt_creator_new", DT(1), None,
        ],
    )

    # Left out, though idle: an endpoint deleted at D(50), and a Databricks pay-per-token
    # foundation model endpoint (databricks-*, FOUNDATION_MODEL).
    for se_id, ep_id, ep_name, entity_type, delete_time in (
        ("pt_se_deleted", "pt_ep_deleted", "endpoint-deleted", "model", DT(50)),
        ("pt_se_ppt", "pt_ep_ppt", "databricks-meta-llama-3-3-70b-instruct", "FOUNDATION_MODEL", None),
    ):
        con.execute(
            """
            INSERT INTO serving__served_entities
            (
                account_id, workspace_id, served_entity_id, endpoint_id, endpoint_name, endpoint_config_version,
                served_entity_name, entity_type, entity_name, entity_version, task, external_model_config,
                foundation_model_config, custom_model_config, feature_spec_config, created_by, change_time, endpoint_delete_time
            )
            VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ACCOUNT_ID, WS_PROD, se_id, ep_id, ep_name, 1,
                f"{ep_name}-entity", entity_type, ep_name, "1", None, None, None, None, None,
                "pt_creator_left_out", DT(100), delete_time,
            ],
        )

    # =========================================================================
    # Build system.serving.endpoint_usage (for compute_serving_dormant_endpoints)
    # =========================================================================
    # Need enough requests to hit the status bands: OK >= 10, WARN 1-9, CRITICAL 0.
    # All request_time values are D()-relative (never an absolute literal).

    # OK entity: 15 requests at D(11) -- inside the 30- and 90-day windows, outside the 7-day
    # window (so w=7 flips this entity's status: 0 requests there, since change_time is also
    # outside all three windows -- see served_entities above).
    for i in range(15):
        con.execute(
            """
            INSERT INTO serving__endpoint_usage
            (
                account_id, workspace_id, served_entity_id, client_request_id, databricks_request_id,
                requester, status_code, request_time, input_token_count, output_token_count,
                input_character_count, output_character_count, usage_context, request_streaming
            )
            VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ACCOUNT_ID, WS_PROD, "pt_se_ok", f"pt_req_ok_{i}", f"pt_dbreq_ok_{i}",
                "pt_requester", 200, DT(11, hour=i), 100, 50,
                500, 250, {}, False,
            ],
        )

    # OK entity, extra row at D0 -- must be excluded by every window (request_time <
    # current_date()), proving the current-day exclusion holds for this query too.
    con.execute(
        """
        INSERT INTO serving__endpoint_usage
        (
            account_id, workspace_id, served_entity_id, client_request_id, databricks_request_id,
            requester, status_code, request_time, input_token_count, output_token_count,
            input_character_count, output_character_count, usage_context, request_streaming
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ACCOUNT_ID, WS_PROD, "pt_se_ok", "pt_req_ok_d0", "pt_dbreq_ok_d0",
            "pt_requester", 200, DT(0, hour=12), 100, 50,
            500, 250, {}, False,
        ],
    )

    # OK entity, extra row at D(45) -- inside the 90-day window only, outside the 30-day window,
    # so this entity's total_requests differs across all three of 7/30/90 (0 / 15 / 16).
    con.execute(
        """
        INSERT INTO serving__endpoint_usage
        (
            account_id, workspace_id, served_entity_id, client_request_id, databricks_request_id,
            requester, status_code, request_time, input_token_count, output_token_count,
            input_character_count, output_character_count, usage_context, request_streaming
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ACCOUNT_ID, WS_PROD, "pt_se_ok", "pt_req_ok_d45", "pt_dbreq_ok_d45",
            "pt_requester", 200, DT(45), 100, 50,
            500, 250, {}, False,
        ],
    )

    # WARN entity: 5 requests at D(6) -- inside the 7-, 30-, and 90-day windows, so this entity's
    # status is stable (WARN) at every window this suite exercises.
    for i in range(5):
        con.execute(
            """
            INSERT INTO serving__endpoint_usage
            (
                account_id, workspace_id, served_entity_id, client_request_id, databricks_request_id,
                requester, status_code, request_time, input_token_count, output_token_count,
                input_character_count, output_character_count, usage_context, request_streaming
            )
            VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ACCOUNT_ID, WS_DEV, "pt_se_warn", f"pt_req_warn_{i}", f"pt_dbreq_warn_{i}",
                "pt_requester", 200, DT(6, hour=i), 50, 25,
                250, 125, {}, False,
            ],
        )

    # CRITICAL entity: no requests (no rows inserted)
    # NOT_ASSESSED entity: no requests (no rows inserted; the status is determined by latest_change_time)

    # =========================================================================
    # Build system.information_schema.tables (for storage_target_table_discovery)
    # =========================================================================
    # Need multiple rows with different table_type values, including at least one VIEW.
    # Use pt_ prefixed table names and different catalogs/schemas.

    tables = [
        # Non-VIEW tables (2+ across different catalogs/schemas)
        ("pt_cat_a", "pt_schema_a", "pt_table_1", "MANAGED"),
        ("pt_cat_a", "pt_schema_b", "pt_table_2", "EXTERNAL"),
        ("pt_cat_b", "pt_schema_a", "pt_table_3", "MANAGED"),
        # VIEW (excluded by query's WHERE table_type <> 'VIEW')
        ("pt_cat_a", "pt_schema_a", "pt_view_1", "VIEW"),
    ]

    for catalog, schema, table_name, table_type in tables:
        con.execute(
            """
            INSERT INTO information_schema__tables
            (
                table_catalog, table_schema, table_name, table_type, is_insertable_into, commit_action,
                table_owner, comment, created, created_by, last_altered, last_altered_by, data_source_format,
                storage_sub_directory, storage_path
            )
            VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                catalog, schema, table_name, table_type, "YES" if table_type != "VIEW" else "NO", None,
                "pt_owner", None, AS_OF, "pt_creator", AS_OF, "pt_creator",
                "DELTA" if table_type != "VIEW" else None,
                None, f"s3://pt-bucket/{catalog}/{schema}/{table_name}/" if table_type == "EXTERNAL" else None,
            ],
        )

    # Parquet is written by build_fixtures.py's own driver loop (one write_parquet call per
    # touched source, after this build(con) call returns) -- a builder never calls write_parquet
    # itself (see base.py's write_parquet docstring and billing.py's build(), which follows the
    # same contract).
