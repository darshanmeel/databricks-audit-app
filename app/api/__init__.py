"""app/api

FastAPI JSON backend over app/core (app/core/data.py, app/core/registry.py, app/core/pricing.py,
app/core/config.py). Read-only: it returns JSON, never HTML, and never writes SQL directly --
every query goes through app/core/data.py's existing functions.

    app/api/service.py     the four-outcome honesty rule, built on app/core alone.
    app/api/app.py         the FastAPI application and its routes.
    app/api/__main__.py    `python -m app.api` -> uvicorn app.api.app:app on 127.0.0.1:8000.
"""
