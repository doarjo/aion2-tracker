"""Config discovery must work from a source checkout AND an installed package.

Regression test for a shipped bug: the config directory was derived from the
package file's grandparent, which is the repo root in a checkout but
site-packages' parent once installed. In the container that silently loaded an
empty config, which zeroed every stat weight and demoted every recommendation
from Critical/High down to Medium, with no error anywhere.
"""

from __future__ import annotations

import pytest

from aion2 import paths
from aion2.recommend import ConfigNotFound, Engine, load_config


def test_finds_thresholds_in_source_checkout():
    found = paths.find_config_file("thresholds.yaml")
    assert found is not None, "should find the repo's own config"
    assert found.name == "thresholds.yaml"
    assert found.exists()


def test_explicit_env_var_wins(tmp_path, monkeypatch):
    custom = tmp_path / "custom.yaml"
    custom.write_text("stat_weights:\n  Accuracy: 9.0\n", encoding="utf-8")
    monkeypatch.setenv("AION2_THRESHOLDS", str(custom))

    assert paths.find_config_file("thresholds.yaml") == custom
    assert load_config()["stat_weights"]["Accuracy"] == 9.0


def test_config_dir_env_var_is_searched(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "thresholds.yaml").write_text("stat_weights:\n  Critical: 4.0\n", encoding="utf-8")
    monkeypatch.delenv("AION2_THRESHOLDS", raising=False)
    monkeypatch.setenv("AION2_CONFIG_DIR", str(cfg_dir))

    found = paths.find_config_file("thresholds.yaml")
    assert found is not None
    assert found.parent == cfg_dir.resolve()


def test_missing_config_raises_rather_than_returning_empty(tmp_path, monkeypatch):
    """The whole point: fail loudly instead of silently degrading."""
    monkeypatch.setenv("AION2_THRESHOLDS", str(tmp_path / "does-not-exist.yaml"))
    with pytest.raises(ConfigNotFound):
        load_config()


def test_non_strict_mode_still_available(tmp_path, monkeypatch):
    monkeypatch.setenv("AION2_THRESHOLDS", str(tmp_path / "nope.yaml"))
    assert load_config(strict=False) == {}


def test_engine_reports_missing_weights():
    """An engine with no weights must admit it rather than look healthy."""
    blind = Engine(config={})
    assert blind.has_config is False

    real = Engine(load_config())
    assert real.has_config is True


def test_shipped_config_has_the_keys_the_engine_reads():
    cfg = load_config()
    assert cfg["profiles"], "profiles drive all ranking"
    assert cfg["default_profile"] in cfg["profiles"]
    assert cfg["wasted_stats"], "wasted_stats drives swap detection"
    assert cfg["skill_targets"]
    assert cfg["accuracy"]["target_min"] > 0


def test_every_profile_has_weights():
    cfg = load_config()
    for name, body in cfg["profiles"].items():
        assert body.get("stat_weights"), f"profile {name} has no stat_weights"
        assert body.get("label"), f"profile {name} has no label"


def test_project_root_is_sane():
    root = paths.project_root()
    assert root.exists()
