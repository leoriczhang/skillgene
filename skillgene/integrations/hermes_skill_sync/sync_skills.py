#!/usr/bin/env python3
"""pre_llm_call hook: pull team SkillGene skills into Hermes.

The hook does not proxy model traffic and does not inject context. It only keeps
a local team-skill directory fresh so Hermes' native ``skills_list`` and
``skill_view`` tools can discover team skills through ``skills.external_dirs``.

Configuration precedence:

1. explicit env vars (``SKILLGENE_SYNC_*``),
2. ``sync.json`` next to this script,
3. local ``~/.skillgene/config.yaml`` if the ``skillgene`` package is installed.

If sharing credentials are not configured the hook exits successfully and
silently, matching Hermes shell-hook expectations.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_MIN_INTERVAL_SECONDS = 60


def _log(message: str) -> None:
    print(f"[skillgene-sync] {message}", file=sys.stderr)


def _config_path() -> Path:
    override = os.environ.get("SKILLGENE_SYNC_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().with_name("sync.json")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _hermes_home(cfg: dict[str, Any]) -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip() or str(cfg.get("hermes_home") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def _target_dir(cfg: dict[str, Any]) -> Path:
    raw = os.environ.get("SKILLGENE_SYNC_TARGET_DIR", "").strip() or str(cfg.get("target_dir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _hermes_home(cfg) / "team_skills" / "skillgene"


def _lock_path(target_dir: Path) -> Path:
    return target_dir.parent / ".skillgene-sync.lock"


def _stamp_path(target_dir: Path) -> Path:
    return target_dir.parent / ".skillgene-sync.stamp"


def _interval_seconds(cfg: dict[str, Any]) -> int:
    raw = os.environ.get("SKILLGENE_SYNC_MIN_INTERVAL_SECONDS", "") or cfg.get("min_interval_seconds")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MIN_INTERVAL_SECONDS


def _should_skip(target_dir: Path, cfg: dict[str, Any]) -> bool:
    interval = _interval_seconds(cfg)
    if interval <= 0:
        return False
    stamp = _stamp_path(target_dir)
    try:
        age = time.time() - stamp.stat().st_mtime
    except OSError:
        return False
    return age < interval


def _touch_stamp(target_dir: Path) -> None:
    stamp = _stamp_path(target_dir)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()


def _try_lock(target_dir: Path):
    lock = _lock_path(target_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+", encoding="utf-8")
    try:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
    except ImportError:
        pass
    return handle


def _load_skillgene_config():
    from skillgene.config_store import ConfigStore

    return ConfigStore().to_config()


def _apply_overrides(config, cfg: dict[str, Any]):
    # Keep names aligned with SkillGeneConfig fields, but accept a small set of
    # installer-friendly aliases for remote machines that do not have a full
    # ~/.skillgene/config.yaml yet.
    mapping = {
        "sharing_enabled": "sharing_enabled",
        "backend": "sharing_backend",
        "sharing_backend": "sharing_backend",
        "endpoint": "sharing_viking_endpoint",
        "viking_endpoint": "sharing_viking_endpoint",
        "viking_api_key": "sharing_viking_api_key",
        "team_api_key": "sharing_viking_team_api_key",
        "viking_team_api_key": "sharing_viking_team_api_key",
        "viking_account": "sharing_viking_account",
        "viking_user": "sharing_viking_user",
        "viking_agent": "sharing_viking_agent",
        "viking_agent_id": "sharing_viking_agent_id",
        "viking_customer_id": "sharing_viking_customer_id",
        "viking_root_prefix": "sharing_viking_root_prefix",
        "viking_group_id": "sharing_viking_group_id",
        "local_root": "sharing_local_root",
    }
    for source, target in mapping.items():
        if source in cfg and cfg[source] not in (None, ""):
            setattr(config, target, cfg[source])
    if any(
        cfg.get(key)
        for key in ("backend", "sharing_backend", "endpoint", "viking_endpoint", "viking_api_key", "team_api_key")
    ):
        config.sharing_enabled = bool(cfg.get("sharing_enabled", True))
    return config


def _pull(target_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    from skillgene.skills.hub import SkillHub

    config = _apply_overrides(_load_skillgene_config(), cfg)
    if not getattr(config, "sharing_enabled", False):
        return {"status": "skipped", "reason": "sharing_disabled"}
    backend = str(getattr(config, "sharing_backend", "") or "").strip().lower()
    if not backend and getattr(config, "sharing_viking_endpoint", ""):
        backend = "viking"
        config.sharing_backend = "viking"
    if backend == "viking" and not (
        getattr(config, "sharing_viking_team_api_key", "") or getattr(config, "sharing_viking_api_key", "")
    ):
        return {"status": "skipped", "reason": "missing_team_api_key"}
    hub = SkillHub.team_from_config(config)
    result = hub.pull_skills(str(target_dir), mirror=bool(cfg.get("mirror", False)))
    return {"status": "ok", **result}


def main() -> int:
    # Consume stdin so Hermes can pipe the hook payload without blocking. The
    # current implementation does not need fields from the payload.
    try:
        sys.stdin.read()
    except Exception:
        pass

    cfg = _load_json(_config_path())
    target = _target_dir(cfg)
    if _should_skip(target, cfg):
        print(json.dumps({"action": "allow", "status": "skipped", "reason": "interval"}))
        return 0
    lock = _try_lock(target)
    if lock is None:
        print(json.dumps({"action": "allow", "status": "skipped", "reason": "locked"}))
        return 0
    try:
        try:
            result = _pull(target, cfg)
            if result.get("status") == "ok":
                _touch_stamp(target)
            else:
                _log(f"skipped: {result.get('reason', 'unknown')}")
        except Exception as exc:  # noqa: BLE001 - hooks must not fail Hermes turns
            result = {"status": "error", "error": str(exc)}
            _log(f"sync failed: {exc}")
        print(json.dumps({"action": "allow", **result}, ensure_ascii=False))
        return 0
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
