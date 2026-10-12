"""Tests for config.maybe_reload — picking up config.yaml edits live."""

import copy
import os
import time

import src.config as cfg


def test_maybe_reload_picks_up_disk_change(tmp_path, monkeypatch):
    saved_config = copy.deepcopy(cfg.CONFIG)
    saved_mtime = cfg._LOADED_MTIME
    try:
        p = tmp_path / "config.yaml"
        p.write_text("user:\n  name: First\n  aliases: ''\n")
        monkeypatch.setattr(cfg, "_CONFIG_PATH", str(p))

        # Prime in-memory state as if this file had been loaded at startup.
        cfg.CONFIG.clear()
        cfg.CONFIG.update(cfg.load_config())
        cfg._LOADED_MTIME = cfg._config_mtime()
        assert cfg.CONFIG["user"]["name"] == "First"
        assert cfg.maybe_reload() is False       # unchanged → no reload

        # Edit on disk (force a newer mtime so the change is detectable).
        time.sleep(0.01)
        p.write_text("user:\n  name: Second\n  aliases: 'Deux'\n")
        os.utime(p, (time.time() + 1, time.time() + 1))

        assert cfg.maybe_reload() is True
        assert cfg.CONFIG["user"]["name"] == "Second"
        assert cfg.CONFIG["user"]["aliases"] == "Deux"
        assert cfg.maybe_reload() is False       # no further change
    finally:
        cfg.CONFIG.clear()
        cfg.CONFIG.update(saved_config)
        cfg._LOADED_MTIME = saved_mtime


def test_maybe_reload_survives_half_written_file(tmp_path, monkeypatch):
    saved_config = copy.deepcopy(cfg.CONFIG)
    saved_mtime = cfg._LOADED_MTIME
    try:
        p = tmp_path / "config.yaml"
        p.write_text("user:\n  name: Good\n  aliases: ''\n")
        monkeypatch.setattr(cfg, "_CONFIG_PATH", str(p))
        cfg.CONFIG.clear()
        cfg.CONFIG.update(cfg.load_config())
        cfg._LOADED_MTIME = cfg._config_mtime()

        # Writer caught mid-save: invalid YAML. Keep the last good config.
        time.sleep(0.01)
        p.write_text("user:\n  name: [unterminated\n")
        os.utime(p, (time.time() + 1, time.time() + 1))

        assert cfg.maybe_reload() is False
        assert cfg.CONFIG["user"]["name"] == "Good"
    finally:
        cfg.CONFIG.clear()
        cfg.CONFIG.update(saved_config)
        cfg._LOADED_MTIME = saved_mtime
