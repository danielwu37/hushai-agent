# hushAi-agent

`hushAi-agent` 是一个开源标准的个人 AI 记忆与协作框架：以本地 Markdown 文件夹（`vault`）作为唯一记忆底座，各端（`door`）平日只向收件箱（`inbox`）追加记录，服务器上的常驻管家（`steward`）定时将收件对账汇总为派生视图，让不同 AI 工具安全读写同一份个人记忆。

## 核心概念

- **vault（记忆库）**：一个使用者的全部数据目录，包含 `hub/`（跨领域核心记忆）与 `domains/`（各领域资料库）。
- **door（端）**：能读写 vault 的入口，具有唯一编号（如 `steward`、`laptop`、`web`）。其中 `steward` 是服务器上的常驻管家。
- **inbox（收件）**：位于 `hub/inbox/` 下，每端每天一个文件，严格采用只追加（append-only）方式写入。
- **instance（实例）**：一个使用者的一整套运行单元（vault + 配置 + 状态），单台主机可承载多个独立实例。
- **记忆三层**：
  1. **约定层**（`AGENTS.md`、`AGENTS.local.md`、`profile.md`）：行为规则与个人画像，仅在使用者批准后修改。
  2. **记录层**（`hub/inbox/`）：日常事实流水，只追加不篡改。
  3. **派生层**（`status.md`、`status-index.md`、`open-items.md`、`today.md`、`insights.md`、`reports/`）：仅由管家汇总任务生成，是数据而非指令，可由记录层随时重算。

## 5 分钟上手

确保运行环境为 Python 3.10 或更高版本（零第三方依赖）。

### 1. 初始化实例（init）

```bash
hushai init --instance demo --vault /path/to/my-vault
```

该命令会从内置模板生成完整 vault 目录结构、写入实例配置、创建状态目录，并在系统安装了 `git` 时自动初始化版本库。重复执行具备幂等性，只补齐缺失文件而不覆盖已有文件（若在已有提交的 Git 仓库中补回了缺失文件，会自动提交本次补齐的文件）。指定 `--lang en` 时，生成的实例配置默认领域为英文（`work,family,life,study,health`），但内置 vault 模板文件正文目前只有中文。

### 2. 实例体检（doctor）

```bash
hushai doctor --instance demo
# 或输出 JSON 格式：
hushai doctor --instance demo --json
```

逐项检查配置文件、目录结构、文件行数上限、收件格式、同步冲突副本、状态目录权限、Git 状态与隐私目录。

### 3. 追加收件（inbox add）

```bash
hushai inbox add \
  --instance demo \
  --door laptop \
  --domain 工作 \
  --did "完成第三章译稿初校" \
  --artifact "domains/work/ch3.md" \
  --todo "周五前复核专有名词表"
```

向实例当天对应端的收件文件安全追加一条标准格式记录。

### 4. 校验任意记忆库（validate）

```bash
hushai validate /path/to/my-vault
# 或校验仓库自带的虚构示例库：
hushai validate examples/vault-demo
```

无需实例配置即可按规范校验任意 vault 的结构、收件文件名、收件行格式与同步冲突文件。

## 仓库目录说明

```text
pyproject.toml            包配置与命令行入口定义
LICENSE                   MIT 开源许可证
README.md                 项目简介与快速上手
docs/spec.md              hushAi-agent 核心规范文档
src/hushai_agent/         核心源码包（标准库实现）
  cli.py                  命令行参数解析与子命令分发
  config.py               实例配置读写与校验
  paths.py                配置根、状态根与模板路径解析
  guard.py                写入路径护栏（防目录穿越与外部符号链接）
  fsio.py                 原子覆写与 O_APPEND 整行追加
  inboxfmt.py             收件文件名与收件行解析、格式化与校验
  vault.py                记忆库初始化与收件追加逻辑
  validate.py             无配置记忆库规范校验
  doctor.py               实例全量体检
  templates/vault/        初始化默认模板（内置模板正文目前只有中文）
examples/vault-demo/      虚构示例记忆库（自由译者「林晓」）
tools/privacy_scan.py     个人隐私信息正则扫描工具
tests/                    单元测试套件
```
