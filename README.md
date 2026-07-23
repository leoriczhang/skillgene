# SkillGene

[English](./README.en.md)

SkillGene 是一个面向 Agent 的共享技能库工具。它围绕标准 `SKILL.md`
组织技能，提供本地管理、团队同步、Web 控制台和可选验证能力，适合把真实
Agent 使用经验沉淀成可复用的团队技能资产。

## 能力

- **技能库管理**：读取、创建、编辑、删除和打包 `SKILL.md` 技能。
- **本地/远端同步**：支持本地目录和 OpenViking 兼容对象存储。
- **技能版本记录**：为技能生成稳定 ID，并记录版本、内容 hash 和历史。
- **Web 控制台**：内置 React + TypeScript 控制台，可管理技能、用户、候选评审和系统健康。
- **Hermes 接入**：提供 `skillgene-feed`，把 Hermes 会话回流到 SkillGene 服务。
- **可选验证**：通过 OpenAI 兼容模型和 PRM 对候选技能进行回放评估。
- **True Replay**：在隔离沙盒中启动真实 Hermes Agent，对候选技能做 A/B 工具轨迹回放。

## 安装

```bash
git clone https://github.com/leoriczhang/skillgene.git
cd skillgene
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[all]"
```

只使用核心功能：

```bash
python -m pip install -e .
```

使用安装脚本：

```bash
bash scripts/install_skillgene.sh
```

## 快速开始

配置一个本地技能目录：

```bash
skillgene config skills.enabled true
skillgene config skills.dir ./skills
skillgene config sharing.enabled true
skillgene config sharing.backend local
skillgene config sharing.local_root ./skillgene-store
```

创建本地技能：

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

同步到共享存储：

```bash
skillgene skills push
skillgene skills list
skillgene skills pull
```

启动 SkillGene 服务：

```bash
skillgene config service.port 30000  # 服务端口
skillgene start --daemon
skillgene status
```

打开控制台：

```text
http://127.0.0.1:30000/console
```

## 在其他机器上使用团队 Skills

SkillGene 不再作为 Hermes 的模型代理使用。其他机器上的 Hermes 若要使用团队 skills，
应通过 OpenViking 或文件同步把团队技能拉到本机，再把该目录加入 Hermes 的
`skills.external_dirs`。

示例：

```yaml
skills:
  external_dirs:
    - /path/to/team/skills
```

如果只想在会话结束后把 Hermes 会话回流给 SkillGene / evolve ingest 服务，可使用下面的
`skillgene-feed` hook。注意：该 hook 需要目标服务提供 `/ingest_session` 接口。

## OpenViking / 对象存储

SkillGene 的远端同步通过对象存储抽象完成。OpenViking 配置示例：

```bash
skillgene config sharing.enabled true
skillgene config sharing.backend viking
skillgene config sharing.viking_endpoint "https://<your-openviking-endpoint>"
skillgene config sharing.viking_team_api_key "<team-key>"
skillgene config sharing.viking_personal_api_key "<personal-key>"
skillgene config sharing.viking_root_prefix "skillgene"
```

不要把真实 API Key 写进仓库。建议通过本机配置或环境变量注入。

## Hermes 会话回流

安装内置 Hermes skill：

```bash
python skillgene/integrations/hermes_skill/install.py \
  --user "$USER" \
  --url "http://127.0.0.1:8787"
```

如果服务端要求鉴权：

```bash
python skillgene/integrations/hermes_skill/install.py \
  --user "$USER" \
  --url "http://127.0.0.1:8787" \
  --api-key "$SKILLGENE_INGEST_API_KEY"
```

该脚本会把 `skillgene-feed` 安装到 Hermes home，并注册 `on_session_end`
hook。Hermes 的源码不需要修改。

## True Replay

安装 True Replay 依赖：

```bash
python -m pip install -e ".[truereplay]"
```

用共享验证队列中的 job 回放：

```bash
python -m skillgene.true_replay --job-id <validation-job-id> --json
```

也可以用本地 JSON 文件独立回放：

```bash
python -m skillgene.true_replay --job-file ./candidate_job.json --dry-run
python -m skillgene.true_replay --job-file ./candidate_job.json --json
```

True Replay 会为 baseline 和 candidate 两个分支分别创建临时 `HOME` 与
`HERMES_HOME`，真实 `~/.hermes` 不会被修改。若使用本地 Hermes checkout，可设置：

```bash
export HERMES_ORIGIN=/path/to/hermes-agent
```

## 项目结构

```text
skillgene/
├── skillgene/
│   ├── cli/              # skillgene 命令行
│   ├── config_store/     # 本地配置读写
│   ├── proxy/            # Web 服务兼容包：路由、控制台和管理接口
│   ├── skills/           # SKILL.md 管理、打包、同步
│   ├── storage/          # local / OpenViking 存储后端
│   ├── integrations/     # Hermes 集成
│   ├── validation/       # 可选候选技能验证
│   ├── true_replay.py    # 真实 A/B 回放
│   └── web/              # 控制台构建产物
├── web-ui/               # React + TypeScript 控制台源码
├── tests/
├── scripts/
└── pyproject.toml
```

## 开发

```bash
python -m pip install -e ".[dev,all]"
python -m pytest
```

构建：

```bash
npm --prefix web-ui install
npm --prefix web-ui run build
python -m pip install build
python -m build
```

## License

MIT
