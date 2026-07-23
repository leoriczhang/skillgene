# SkillGene

[中文](./README.md)

SkillGene is a shared skill-library toolkit for coding agents and LLM agents.
It organizes reusable behavior as standard `SKILL.md` bundles and provides
local management, shared synchronization, a Web console, and optional
candidate validation.

## Features

- **Skill library management**: read, create, edit, delete, and package `SKILL.md` skills.
- **Local and remote sync**: local filesystem storage plus OpenViking-compatible object storage.
- **Version tracking**: stable skill IDs, content hashes, versions, and update history.
- **Web console**: built-in React + TypeScript console for skills, users, candidate review, and health checks.
- **Hermes integration**: `skillgene-feed` can submit Hermes sessions back to a SkillGene service.
- **Optional validation**: replay candidate skills with an OpenAI-compatible model and PRM scorer.
- **True Replay**: run real Hermes agents in isolated sandboxes for candidate-vs-baseline A/B trajectory replay.

## Install

```bash
git clone https://github.com/leoriczhang/skillgene.git
cd skillgene
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[all]"
```

Core package only:

```bash
python -m pip install -e .
```

Installer script:

```bash
bash scripts/install_skillgene.sh
```

## Quick Start

Configure a local skill library:

```bash
skillgene config skills.enabled true
skillgene config skills.dir ./skills
skillgene config sharing.enabled true
skillgene config sharing.backend local
skillgene config sharing.local_root ./skillgene-store
```

Create a local skill:

```bash
mkdir -p skills/example-skill
cat > skills/example-skill/SKILL.md <<'EOF'
---
name: example-skill
description: Use when you need a minimal SkillGene example.
category: general
---

# Example Skill

Follow the project conventions and keep the answer concise.
EOF
```

Sync it:

```bash
skillgene skills push
skillgene skills list
skillgene skills pull
```

Start the SkillGene service:

```bash
skillgene config service.port 30000  # service port
skillgene start --daemon
skillgene status
```

Open the console:

```text
http://127.0.0.1:30000/console
```

## Use Team Skills on Other Hermes Machines

SkillGene is no longer used as a Hermes model proxy. To use team skills on
other Hermes machines, pull or sync the team skill directory locally, then add
that directory to Hermes `skills.external_dirs`.

Example:

```yaml
skills:
  external_dirs:
    - /path/to/team/skills
```

If you only want to submit Hermes sessions after each conversation, use the
`skillgene-feed` hook below. The hook target must expose an `/ingest_session`
endpoint.

## OpenViking / Object Storage

Remote sync is implemented through SkillGene's object-store abstraction.
Example OpenViking configuration:

```bash
skillgene config sharing.enabled true
skillgene config sharing.backend viking
skillgene config sharing.viking_endpoint "https://<your-openviking-endpoint>"
skillgene config sharing.viking_team_api_key "<team-key>"
skillgene config sharing.viking_personal_api_key "<personal-key>"
skillgene config sharing.viking_root_prefix "skillgene"
```

Do not commit real API keys. Use local configuration or environment injection.

## Hermes Session Feed

Install the bundled Hermes skill:

```bash
python skillgene/integrations/hermes_skill/install.py \
  --user "$USER" \
  --url "http://127.0.0.1:8787"
```

If your ingest service requires authentication:

```bash
python skillgene/integrations/hermes_skill/install.py \
  --user "$USER" \
  --url "http://127.0.0.1:8787" \
  --api-key "$SKILLGENE_INGEST_API_KEY"
```

The installer copies `skillgene-feed` into the Hermes home and registers an
`on_session_end` hook. No Hermes source-code modification is required.

## True Replay

Install True Replay dependencies:

```bash
python -m pip install -e ".[truereplay]"
```

Replay a job from the shared validation queue:

```bash
python -m skillgene.true_replay --job-id <validation-job-id> --json
```

Replay a local JSON job file:

```bash
python -m skillgene.true_replay --job-file ./candidate_job.json --dry-run
python -m skillgene.true_replay --job-file ./candidate_job.json --json
```

True Replay creates temporary `HOME` and `HERMES_HOME` directories for both
baseline and candidate branches. Your real `~/.hermes` is not modified. To use
a local Hermes checkout:

```bash
export HERMES_ORIGIN=/path/to/hermes-agent
```

## Layout

```text
skillgene/
├── skillgene/
│   ├── cli/              # skillgene command line
│   ├── config_store/     # local config store
│   ├── proxy/            # Compatibility package for service routes, console, and admin APIs
│   ├── skills/           # SKILL.md management, bundling, sync
│   ├── storage/          # local / OpenViking storage backends
│   ├── integrations/     # Hermes integration
│   ├── validation/       # optional candidate-skill validation
│   ├── true_replay.py    # true A/B replay
│   └── web/              # built console assets
├── web-ui/               # React + TypeScript console source
├── tests/
├── scripts/
└── pyproject.toml
```

## Development

```bash
python -m pip install -e ".[dev,all]"
python -m pytest
```

Build:

```bash
npm --prefix web-ui install
npm --prefix web-ui run build
python -m pip install build
python -m build
```

## License

MIT
