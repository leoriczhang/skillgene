# SkillGene

[中文](./README.md)

SkillGene is a shared skill-library toolkit for coding agents and LLM agents.
It organizes reusable behavior as standard `SKILL.md` bundles and provides
local management, shared synchronization, proxy-based recording, and optional
candidate validation.

## Features

- **Skill library management**: read, create, edit, delete, and package `SKILL.md` skills.
- **Local and remote sync**: local filesystem storage plus OpenViking-compatible object storage.
- **Version tracking**: stable skill IDs, content hashes, versions, and update history.
- **Proxy recording**: OpenAI-compatible proxy service that records sessions and skill signals.
- **Hermes integration**: `skillgene-feed` can submit Hermes sessions back to a SkillGene service.
- **Optional validation**: replay candidate skills with an OpenAI-compatible model and PRM scorer.

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

Start the proxy:

```bash
skillgene config proxy.port 30000
skillgene start --daemon
skillgene status
```

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

## Layout

```text
skillgene/
├── skillgene/
│   ├── cli/              # skillgene command line
│   ├── config_store/     # local config store
│   ├── proxy/            # OpenAI-compatible proxy and session recording
│   ├── skills/           # SKILL.md management, bundling, sync
│   ├── storage/          # local / OpenViking storage backends
│   ├── integrations/     # Hermes integration
│   └── validation/       # optional candidate-skill validation
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
python -m pip install build
python -m build
```

## Note

This repository contains the SkillGene package only. The broader team skill
evolution console, evaluation server, dataset generator, and experimental
components are intentionally not included here.

## License

MIT
