from __future__ import annotations

import json
import io
from pathlib import Path

import yaml

from skillgene.integrations.hermes_skill_sync import install, sync_skills


def test_install_wires_pre_llm_hook_external_dir_and_allowlist(tmp_path: Path) -> None:
    hermes_home = tmp_path / ".hermes"
    rc = install.main(
        [
            "--hermes-home",
            str(hermes_home),
            "--python",
            "python3",
            "--viking-endpoint",
            "https://openviking.example",
            "--viking-team-api-key",
            "team-secret",
            "--viking-root-prefix",
            "skillgene",
        ]
    )
    assert rc == 0

    sync_dir = hermes_home / "skills" / "skillgene-sync"
    target_dir = hermes_home / "team_skills" / "skillgene"
    assert (sync_dir / "SKILL.md").is_file()
    assert (sync_dir / "sync_skills.py").is_file()
    assert target_dir.is_dir()

    config = yaml.safe_load((hermes_home / "config.yaml").read_text("utf-8"))
    assert str(target_dir) in config["skills"]["external_dirs"]
    hook = config["hooks"]["pre_llm_call"][0]
    assert hook["command"] == f"python3 {sync_dir / 'sync_skills.py'}"
    assert hook["timeout"] == 60

    sync_cfg = json.loads((sync_dir / "sync.json").read_text("utf-8"))
    assert sync_cfg["target_dir"] == str(target_dir)
    assert sync_cfg["viking_endpoint"] == "https://openviking.example"
    assert sync_cfg["viking_team_api_key"] == "team-secret"

    allowlist = json.loads((hermes_home / "shell-hooks-allowlist.json").read_text("utf-8"))
    assert allowlist["approvals"] == [
        {
            "event": "pre_llm_call",
            "command": f"python3 {sync_dir / 'sync_skills.py'}",
            "approved_at": allowlist["approvals"][0]["approved_at"],
            "script_mtime_at_approval": allowlist["approvals"][0]["script_mtime_at_approval"],
        }
    ]


def test_sync_hook_skips_without_sharing_config(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "sync.json"
    cfg.write_text(json.dumps({"target_dir": str(tmp_path / "team")}), "utf-8")
    monkeypatch.setenv("SKILLGENE_SYNC_CONFIG", str(cfg))
    monkeypatch.delenv("SKILLGENE_SYNC_TARGET_DIR", raising=False)

    rc = sync_skills.main()
    assert rc == 0


def test_sync_hook_pulls_local_backend_from_config(tmp_path: Path, monkeypatch) -> None:
    bucket = tmp_path / "bucket"
    source = tmp_path / "source"
    skill_dir = source / "team-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: team-skill\ndescription: demo\n---\n\n# Demo\n", "utf-8")

    from skillgene.skills.hub import SkillHub

    SkillHub(
        backend="local",
        endpoint="",
        local_root=str(bucket),
        customer_id="",
        user_alias="tester",
    ).push_skills(str(source))

    target = tmp_path / "hermes" / "team_skills" / "skillgene"
    cfg = tmp_path / "sync.json"
    cfg.write_text(
        json.dumps(
            {
                "target_dir": str(target),
                "backend": "local",
                "local_root": str(bucket),
                "min_interval_seconds": 0,
            }
        ),
        "utf-8",
    )
    monkeypatch.setenv("SKILLGENE_SYNC_CONFIG", str(cfg))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    rc = sync_skills.main()
    assert rc == 0
    assert (target / "team-skill" / "SKILL.md").is_file()
