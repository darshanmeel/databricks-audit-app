{{ config(materialized='table', schema='tags', tags=['tags']) }}
-- The workspace level of the tag rollup today: the current row of tags.tag_workspace_history per
-- (workspace_id, tag_key), for every key the workspace's bill has carried.
--
--   tag_value     the inferred value, '__mixed__' when no value dominates or '__untagged__' when
--                 too little of the workspace carries the key (both since valid_from).
--   is_allocating TRUE only for a real value; '__mixed__' and '__untagged__' never allocate.
--   share/coverage/top_values  measured over the current version's days only.
--
-- Screens that describe a workspace read this; spend reads the history by usage date, so a tag
-- that first showed on a later day never reaches the days before it.
SELECT workspace_id, tag_key, display_key, tag_value, is_allocating, top_value, top_values,
       share, coverage, dbus_tagged, dbus_total, reason, valid_from
FROM {{ ref('tag_workspace_history') }}
WHERE is_current
