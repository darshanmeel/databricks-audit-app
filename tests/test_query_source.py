"""settings query_source: the "Source" tag on this app's queries; the brand name when not set."""
from __future__ import annotations

import pytest

from app.core import config


def _settings(monkeypatch, tmp_path, text):
    (tmp_path / "settings.yml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(tmp_path))


def test_defaults_to_the_brand_name(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path, "brand:\n  name: Acme Corp\n")
    assert config.query_source() == "AcmeCorp"


def test_a_set_value_wins(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path, "brand:\n  name: Acme Corp\nquery_source: Acme_Audit-1\n")
    assert config.query_source() == "Acme_Audit-1"


def test_a_value_unsafe_for_sql_is_rejected(monkeypatch, tmp_path):
    _settings(monkeypatch, tmp_path, "query_source: \"x' OR 1=1\"\n")
    with pytest.raises(config.SettingsError):
        config.load_settings()


def test_query_tags_take_letters_digits_and_underscore_only():
    assert config._validate_settings({"query_tags": {"team": "data_platform"}})["query_tags"] == {"team": "data_platform"}
    for bad in ({"cost-center": "x"}, {"team": "a-b"}, {"dashboard": "x"}, {"tab": "x"}, ["x"]):
        with pytest.raises(config.SettingsError):
            config._validate_settings({"query_tags": bad})


def test_contract_is_optional_and_checked():
    assert config._validate_settings({})["contract"] is None
    ok = config._validate_settings({"contract": {"start": "2026-01-01", "end": "2026-12-31", "commit_usd": 1000}})
    assert ok["contract"] == {"start": "2026-01-01", "end": "2026-12-31", "commit_usd": 1000.0}
    for bad in ({"start": "2026-01-01", "end": "2025-12-31", "commit_usd": 1},
                {"start": "2026-01-01", "end": "2026-12-31", "commit_usd": 0},
                {"start": "2026-01-01", "end": "2026-12-31"}, "2026"):
        with pytest.raises(config.SettingsError):
            config._validate_settings({"contract": bad})
