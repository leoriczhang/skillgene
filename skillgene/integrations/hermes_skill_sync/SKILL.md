---
name: skillgene-sync
description: Pull team SkillGene skills into Hermes before each LLM turn so native skill_view and skills_list can see them.
category: automation
---

# skillgene-sync

This Hermes integration keeps team skills available without routing model
traffic through SkillGene.

It installs a `pre_llm_call` shell hook. Before each LLM turn, the hook pulls
team skills from the configured SkillGene/OpenViking storage into a local
directory and ensures that directory is listed in Hermes `skills.external_dirs`.
Hermes can then discover the team skills through its native `skills_list` and
`skill_view` tools.

The hook is intentionally silent: it does not inject prompt context and it does
not change Hermes model settings.
