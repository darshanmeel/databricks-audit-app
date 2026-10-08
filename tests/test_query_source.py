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
