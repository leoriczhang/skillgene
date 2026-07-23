"""Hermes integration: auto-configures the Hermes CLI to use the proxy."""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..config import SkillGeneConfig

logger = logging.getLogger(__name__)
# D3: legacy skills dir is probed read-only for one-time migration; not moved.
_LEGACY_SKILLS_DIR = Path.home() / ".skillgene" / "skills"
_HERMES_HOME = Path.home() / ".hermes"
_HERMES_SKILLS_DIR = _HERMES_HOME / "skills"
_HERMES_BACKUP_DIR = Path.home() / ".skillgene" / "backups" / "hermes"


# ------------------------------------------------------------------ #
# Shared file helpers                                                 #
# ------------------------------------------------------------------ #


def _load_yaml_mapping(path: Path, label: str) -> dict:
    """Load a YAML mapping, falling back to an empty mapping."""
    if not path.exists():
        return {}

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("[Hermes] Failed to read %s config %s: %s", label, path, e)
        return {}

    if isinstance(loaded, dict):
        return loaded

    logger.warning(
        "[Hermes] %s config %s is not a mapping; replacing it",
        label,
        path,
    )
    return {}


def _write_yaml_mapping_atomic(path: Path, data: dict, label: str) -> None:
    """Atomically write a YAML mapping to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(_yaml_mapping_to_text(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        logger.info("[Hermes] %s config updated: %s", label, path)
    except Exception as e:
        logger.error("[Hermes] Failed to write %s config %s: %s", label, path, e)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _yaml_mapping_to_text(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _write_text_atomic(path: Path, text: str, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        logger.info("[Hermes] %s updated: %s", label, path)
    except Exception as e:
        logger.error("[Hermes] Failed to write %s %s: %s", label, path, e)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _backup_text_file_if_changed(
    path: Path,
    new_text: str,
    *,
    backup_dir: Path,
    backup_stem: str,
    backup_suffix: str,
    label: str,
) -> Path | None:
    """Save a timestamped backup before overwriting a text file."""
    if not path.exists():
        return None

    try:
        current_text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("[Hermes] Failed to read %s for backup: %s", path, e)
        return None

    if current_text == new_text:
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{backup_stem}.{timestamp}.{backup_suffix}"
    latest_path = backup_dir / f"{backup_stem}.latest.{backup_suffix}"
    try:
        backup_path.write_text(current_text, encoding="utf-8")
        latest_path.write_text(current_text, encoding="utf-8")
        logger.info("[Hermes] %s backup saved: %s", label, backup_path)
        return backup_path
    except Exception as e:
        logger.warning("[Hermes] Failed to save %s backup: %s", label, e)
        return None


def _latest_backup_path(backup_dir: Path, backup_stem: str, backup_suffix: str) -> Path | None:
    latest_path = backup_dir / f"{backup_stem}.latest.{backup_suffix}"
    if latest_path.exists():
        return latest_path
    if not backup_dir.is_dir():
        return None
    backups = sorted(backup_dir.glob(f"{backup_stem}.*.{backup_suffix}"))
    return backups[-1] if backups else None


# ------------------------------------------------------------------ #
# Hermes adapter                                                      #
# ------------------------------------------------------------------ #


def _backup_hermes_config_if_changed(config_path: Path, new_text: str) -> Path | None:
    """Save the current Hermes config before overwriting it, if it changed."""
    return _backup_text_file_if_changed(
        config_path,
        new_text,
        backup_dir=_HERMES_BACKUP_DIR,
        backup_stem="config",
        backup_suffix="yaml",
        label="Hermes config",
    )


def _latest_hermes_backup_path() -> Path | None:
    return _latest_backup_path(_HERMES_BACKUP_DIR, "config", "yaml")


def configure_hermes(cfg: "SkillGeneConfig") -> None:
    """Auto-configure Hermes to route model traffic through the proxy."""
    config_path = _HERMES_HOME / "config.yaml"
    model_id = cfg.served_model_name or cfg.llm_model_id or "skillgene-model"
    api_key = cfg.proxy_api_key or "skillgene"
    base_url = f"http://127.0.0.1:{cfg.proxy_port}/v1"
    _prepare_hermes_skills_dir(cfg)

    data = _load_yaml_mapping(config_path, "Hermes")
    model = data.get("model")
    if not isinstance(model, dict):
        model = {"default": model} if isinstance(model, str) and model.strip() else {}

    model["provider"] = "custom"
    model["base_url"] = base_url
    model["default"] = model_id
    model["api_key"] = api_key
    model["api_mode"] = ""

    data["model"] = model
    _backup_hermes_config_if_changed(config_path, _yaml_mapping_to_text(data))
    _write_yaml_mapping_atomic(config_path, data, "Hermes")


def inspect_hermes_config(cfg: "SkillGeneConfig") -> dict[str, object]:
    """Return a diagnostic snapshot of the local Hermes integration state."""
    config_path = _HERMES_HOME / "config.yaml"
    expected_model = cfg.served_model_name or cfg.llm_model_id or "skillgene-model"
    expected_base_url = f"http://127.0.0.1:{cfg.proxy_port}/v1"
    expected_api_key = cfg.proxy_api_key or "skillgene"
    expected_skills_dir = Path(str(getattr(cfg, "skills_dir", "") or _HERMES_SKILLS_DIR)).expanduser()

    data = _load_yaml_mapping(config_path, "Hermes")
    model = data.get("model") if isinstance(data, dict) else {}
    if not isinstance(model, dict):
        model = {"default": model} if isinstance(model, str) and model else {}

    configured_provider = str(model.get("provider", "") or "")
    configured_base_url = str(model.get("base_url", "") or "")
    configured_default = str(model.get("default", "") or "")
    configured_api_key = str(model.get("api_key", "") or "")

    backup_path = _latest_hermes_backup_path()
    proxy_match = (
        configured_provider == "custom"
        and configured_base_url == expected_base_url
        and configured_default == expected_model
        and configured_api_key == expected_api_key
    )
    legacy_present = _LEGACY_SKILLS_DIR.is_dir()
    uses_default_skills_dir = expected_skills_dir == _HERMES_SKILLS_DIR
    issues: list[str] = []
    notes: list[str] = [
        "This integration rewrites Hermes-local config.",
        "Hermes session capture relies on explicit session headers.",
    ]
    next_steps: list[str] = []

    if not config_path.exists():
        issues.append("Hermes config is missing: ~/.hermes/config.yaml")
    if not proxy_match:
        issues.append("Hermes model routing is not pointing at the local proxy.")
        next_steps.append("Start the proxy once so it can rewrite ~/.hermes/config.yaml.")
    if not expected_skills_dir.is_dir():
        issues.append(f"Hermes skills directory is missing: {expected_skills_dir}")
        next_steps.append(f"Create or prepare the Hermes skills directory: {expected_skills_dir}")
    if legacy_present:
        notes.append(
            f"Legacy skills were found at {_LEGACY_SKILLS_DIR};"
            " missing skills are copied into the Hermes library on startup."
        )
    if not backup_path:
        next_steps.append(
            "Run the proxy once before relying on `skillgene restore hermes`, so a backup can be created."
        )

    return {
        "status": "ok" if not issues else "warning",
        "config_path": str(config_path),
        "config_exists": config_path.exists(),
        "integration_scope": "hermes-only",
        "expected_model": expected_model,
        "expected_base_url": expected_base_url,
        "configured_provider": configured_provider or "(unset)",
        "configured_base_url": configured_base_url or "(unset)",
        "configured_model": configured_default or "(unset)",
        "proxy_match": proxy_match,
        "expected_skills_dir": str(expected_skills_dir),
        "skills_dir_exists": expected_skills_dir.is_dir(),
        "skills_dir_mode": "hermes-default" if uses_default_skills_dir else "custom",
        "legacy_skills_dir": str(_LEGACY_SKILLS_DIR),
        "legacy_skills_present": legacy_present,
        "latest_backup": str(backup_path) if backup_path else "(none)",
        "session_boundary_mode": "explicit headers",
        "issues": issues,
        "notes": notes,
        "next_steps": next_steps,
    }


def restore_hermes_config(backup_path: Path | None = None) -> dict[str, str]:
    """Restore ~/.hermes/config.yaml from the latest or a specified backup."""
    source = Path(backup_path).expanduser() if backup_path is not None else _latest_hermes_backup_path()
    if source is None or not source.exists():
        raise FileNotFoundError("No Hermes backup found")

    text = source.read_text(encoding="utf-8")
    target = _HERMES_HOME / "config.yaml"
    _write_text_atomic(target, text, "Hermes config restore")
    return {"source": str(source), "target": str(target)}


def _prepare_hermes_skills_dir(cfg: "SkillGeneConfig") -> None:
    """Prepare the Hermes-local skill directory."""
    target_dir = Path(str(getattr(cfg, "skills_dir", "") or _HERMES_SKILLS_DIR)).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    if target_dir != _HERMES_SKILLS_DIR:
        logger.info(
            "[Hermes] Hermes uses custom skills dir: %s",
            target_dir,
        )
        return

    if not _LEGACY_SKILLS_DIR.is_dir():
        return

    migrated = _copy_missing_skill_dirs(_LEGACY_SKILLS_DIR, target_dir)
    if migrated > 0:
        logger.info(
            "[Hermes] migrated %d legacy skill(s) into Hermes skills dir",
            migrated,
        )


def _copy_missing_skill_dirs(src_root: Path, dst_root: Path) -> int:
    """Copy only skill folders that do not already exist in the destination."""
    copied = 0
    for entry in sorted(src_root.iterdir()):
        if not entry.is_dir():
            continue
        src_skill_md = entry / "SKILL.md"
        if not src_skill_md.is_file():
            continue
        dst_dir = dst_root / entry.name
        dst_skill_md = dst_dir / "SKILL.md"
        if dst_skill_md.exists():
            continue
        shutil.copytree(entry, dst_dir)
        copied += 1
    return copied
