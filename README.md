# SkillGene

[English](./README.en.md)

SkillGene 是一个面向 Agent 的共享技能库工具。它围绕标准 `SKILL.md`
组织技能，提供本地管理、团队同步、代理录制和可选验证能力，适合把真实
Agent 使用经验沉淀成可复用的团队技能资产。

## 能力

- **技能库管理**：读取、创建、编辑、删除和打包 `SKILL.md` 技能。
- **本地/远端同步**：支持本地目录和 OpenViking 兼容对象存储。
- **技能版本记录**：为技能生成稳定 ID，并记录版本、内容 hash 和历史。
- **代理录制**：可启动 OpenAI 兼容代理服务，记录会话与技能使用信号。
- **Hermes 接入**：提供 `skillgene-feed`，把 Hermes 会话回流到 SkillGene 服务。
- **可选验证**：通过 OpenAI 兼容模型和 PRM 对候选技能进行回放评估。

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

启动代理服务：

```bash
skillgene config proxy.port 30000
skillgene start --daemon
skillgene status
```

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

## 项目结构

```text
skillgene/
├── skillgene/
│   ├── cli/              # skillgene 命令行
│   ├── config_store/     # 本地配置读写
│   ├── proxy/            # OpenAI 兼容代理与会话录制
│   ├── skills/           # SKILL.md 管理、打包、同步
│   ├── storage/          # local / OpenViking 存储后端
│   ├── integrations/     # Hermes 集成
│   └── validation/       # 可选候选技能验证
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
python -m pip install build
python -m build
```

## 说明

这个仓库只包含 SkillGene 相关代码。完整的团队技能进化控制台、评估服务、
数据集生成器和其他实验性组件不在本仓库内。

## License

MIT
