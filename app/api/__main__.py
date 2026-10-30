"""python -m app.api -- runs the FastAPI app with uvicorn on 127.0.0.1:8000 (override with
AUDIT_API_HOST / AUDIT_API_PORT; as a Databricks App, 0.0.0.0 and DATABRICKS_APP_PORT).
AUDIT_DB picks a database, e.g. AUDIT_DB=tests/db_audit_test.duckdb for the test fixture."""
from __future__ import annotations

import os
import threading

import uvicorn

from app.core import data as app_core_data


def main() -> None:
    app_port = os.environ.get("DATABRICKS_APP_PORT")
    host = os.environ.get("AUDIT_API_HOST", "0.0.0.0" if app_port else "127.0.0.1")
    port = int(os.environ.get("AUDIT_API_PORT", app_port or "8000"))
    print(f"database: {app_core_data._db_path()}")
    # "localhost" tries IPv6 first on Windows and waits ~0.2 s per connection before falling back.
    print(f"open http://{'127.0.0.1' if host in ('127.0.0.1', 'localhost') else host}:{port}")
    app_core_data.share_connection = True
    from app.api import app as api_app

    threading.Thread(target=api_app.warm_findings, daemon=True).start()
    uvicorn.run("app.api.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
