-- The entitlements table config/app.yml rls.table points at: one row per grant.
-- '*' (or empty) in a column means all. Only the columns in rls.columns are checked,
-- so with columns: [workspace] the table needs just principal and workspace_id.
CREATE TABLE main.governance.audit_entitlements (
  principal     STRING NOT NULL,  -- the person's email, as they sign in to Databricks
  workspace_id  STRING,           -- workspace id, or '*'
  warehouse_id  STRING,           -- SQL warehouse id, or '*'
  environment   STRING            -- env as the app shows it (prod, dev, ...), or '*'
);

INSERT INTO main.governance.audit_entitlements VALUES
  -- sees everything
  ('anna.finops@acme.com', '*',                '*',                '*'),
  -- two workspaces: one row each
  ('ben.de@acme.com',      '1234567890123456', '*',                '*'),
  ('ben.de@acme.com',      '2345678901234567', '*',                '*'),
  -- one warehouse in one workspace (needs columns: [workspace, warehouse])
  ('cara.bi@acme.com',     '1234567890123456', 'a1b2c3d4e5f6a7b8', '*'),
  -- every prod workspace (needs environment in columns)
  ('dev.ops@acme.com',     '*',                '*',                'prod');
