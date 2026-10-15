"""masking_enabled() reads settings.yml's privacy.mask_user_identities (off by default), and
app/core/data.py's dim readers follow it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import data as app_core_data  # noqa: E402
from app.core import identity as app_identity  # noqa: E402


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_masking_enabled_defaults_false(config_dir):
    assert app_identity.masking_enabled() is False


def test_masking_enabled_true_when_set(config_dir):
    (config_dir / "settings.yml").write_text("privacy:\n  mask_user_identities: true\n", encoding="utf-8")
    assert app_identity.masking_enabled() is True


def test_mask_identity_columns_off_leaves_values_raw(config_dir):
    df = pd.DataFrame({"owned_by": ["person@example.com"]})
    out = app_core_data._mask_identity_columns(df, ("owned_by",))
    assert out["owned_by"].iloc[0] == "person@example.com"


def test_mask_identity_columns_on_masks_values(config_dir):
    (config_dir / "settings.yml").write_text("privacy:\n  mask_user_identities: true\n", encoding="utf-8")
    df = pd.DataFrame({"owned_by": ["person@example.com"]})
    out = app_core_data._mask_identity_columns(df, ("owned_by",))
    assert out["owned_by"].iloc[0] != "person@example.com"
    assert out["owned_by"].iloc[0].endswith("***")


def test_mask_dim_job_off_leaves_values_raw(config_dir):
    df = pd.DataFrame({
        "run_as": [None],
        "run_as_user_name": ["person@example.com"],
        "creator_user_name": ["creator@example.com"],
    })
    out = app_core_data._mask_dim_job(df)
    assert out["run_as_user_name"].iloc[0] == "person@example.com"
    assert out["creator_user_name"].iloc[0] == "creator@example.com"


def test_mask_dim_job_on_masks_values(config_dir):
    (config_dir / "settings.yml").write_text("privacy:\n  mask_user_identities: true\n", encoding="utf-8")
    df = pd.DataFrame({
        "run_as": [None],
        "run_as_user_name": ["person@example.com"],
        "creator_user_name": ["creator@example.com"],
    })
    out = app_core_data._mask_dim_job(df)
    assert out["run_as_user_name"].iloc[0].endswith("***")
    assert out["creator_user_name"].iloc[0].endswith("***")
