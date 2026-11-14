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

### 5. 闭环汇总、自检与派活（M2 / M3a）

- `hushai digest --instance demo`：每日收集收件、机械对账、模型判断、更新派生视图与提交 Git。
- `hushai reconcile --instance demo`：立即执行一次纯机械对账（不调用模型）。
- `hushai weekly --instance demo`：每周近况归档、洞察重写与周报审计。
- `hushai today --instance demo` / `hushai todo done|defer|drop TARGET --instance demo`：查看与回写今日待办。
- `hushai adapter test NAME --instance demo`：用极小提示词与结构自检问答式适配项（`claude` / `agy` / `fake`）。
- `hushai dispatch --instance demo --name JOB (--task FILE | --goal "目标")`：在隔离工作目录派发独立代理任务并落盘记账。

### 6. 各端接入、即时对账与跨端留言（M3b）

- `hushai gate append-inbox|put-exchange|check-inbox|ssh --instance demo --door laptop`：各端写入 vault 的唯一受控入口，输出 JSON；接法见 `docs/doors.md`。
- `hushai on-event --instance demo`：收件有变化时跑一遍，先即时机械对账（不调模型），再做跨端路由。
- 跨端路由：收件行「待办」以 `@steward`、`@agy` 等开头时，由一个**只出文字、没有工具**的模型作答，回复放 `hub/exchange/out/to-<来源端>/`。默认关闭，只认 `[routing] trusted_doors` 里的端，每天有次数上限。
- `hushai install units --instance demo --out DIR`：生成 systemd 单元模板（目录监听、每日、每周、待办页面）。只生成文件，不安装、不启用。

### 7. 晨间简报与待办页面（M3c）

- `hushai brief --instance demo [--md|--html|--json]`：把「要我办、要我定、候选记忆、疑似已办」收在一屏。每次汇总和即时对账后自动重写 `hub/reports/morning-brief.md` 与 `.html`（只读快照）。
- `hushai serve --instance demo [--port 8765]`：可点选的简报页面，只监听 `127.0.0.1`，凭令牌进入；每次点选写一条标准收件行并立即对账。
- `hushai candidate list|approve|discard`：候选记忆有编号（`C-YYMMDD-N`），批准后原文追加到 `profile.md` 或 `AGENTS.local.md`；只有 `[approval] doors` 里的端能批。
- `hushai todo accept TARGET`：等我拍板的事带推荐时，一步按推荐确认。

### 8. 安装向导、端接入包、同步与生命周期管理（M3d）

- `hushai install wizard --instance demo --vault /path/to/vault`：一站式初始化、写入双适配项与回退链配置、生成 systemd 单元与端接入包并执行体检。
- `hushai install doorkit --instance demo --out DIR`：生成 `claude`（采用 `--setting-sources project,local --settings` 同账号零冲突隔离）与 `agy` 的端接入配置和启动脚本。
- `hushai sync --instance demo [--direction push|pull|both] [--dry-run]`：按文件归属调 `rclone copy` 单向同步（不碰挂载点），拉取后立即触发事件对账。
- `hushai export` / `hushai upgrade` / `hushai uninstall`：完整导出备份（排除令牌凭据与 `.git`）、更新框架托管模板、安全卸载实例。
- 两本手册：`docs/user-guide.md`（《使用者手册》）与 `docs/admin-guide.md`（《安装与维护手册》）。

## 仓库目录说明

```text
pyproject.toml            包配置与命令行入口定义
LICENSE                   MIT 开源许可证
README.md                 项目简介与快速上手
docs/spec.md              hushAi-agent 核心规范文档
docs/design.md            hushAi-agent 架构设计文档
docs/doors.md             各端接入说明（闸门、强制命令、回写写法）
docs/user-guide.md        日常使用者手册
docs/admin-guide.md       安装与维护手册
src/hushai_agent/         核心源码包（标准库实现）
  cli.py                  命令行参数解析与主分发
  cli_loop.py             M2 闭环子命令（today/todo/ledger/digest/weekly/reconcile）
  cli_m3.py               M3 子命令（adapter test / dispatch）
  cli_m3b.py              M3b 子命令（gate / on-event / install units|doorkit|wizard）
  cli_m3c.py              M3c 子命令（candidate / brief / serve）
  cli_m3d.py              M3d 子命令（sync / export / upgrade / uninstall）
  config_ext.py           M3 可选配置节（gate / routing / approval / sync / candidates_pending）
  config.py               实例配置读写与校验
  config_loop.py          回退链、适配项、预算与调度配置解析
  paths.py                配置根、状态根与模板路径解析
  guard.py                读写路径护栏（防目录穿越、外部符号链接与 local_only）
  fsio.py                 原子覆写、O_APPEND 整行追加与提示词安全读取
  inboxfmt.py             收件文件名与收件行解析、格式化与校验
  inboxscan.py            收件行确定性 line_id 与目录有序遍历
  vault.py                记忆库初始化与收件追加逻辑
  validate.py             无配置记忆库规范校验
  doctor.py               实例全量体检（含运行态 ledger-recent / inbox-backlog）
  daily_state.py          同日累积日报状态读写与合并
  reconcile.py            即时纯机械对账（run_mechanical_only）
  digest.py               每日汇总主流程
  weekly.py               每周洞察重写与审计周报
  adapter_cmd.py          适配项自检命令逻辑
  dispatch.py             代理式派活与中断续跑逻辑
  gate.py                 闸门：各端写收件、放交换区、查待收
  on_event.py             事件处理：即时机械对账加跨端路由
  routing.py              跨端路由（只用问答式）
  install.py              服务单元模板渲染
  doorkit.py              端接入包生成与一站式安装向导
  sync.py                 按文件归属单向同步（rclone copy）
  lifecycle.py            导出备份、模板升级与实例卸载
  ppc.py                  更正驱动规则微调（Propose-Probe-Compose）
  candidates.py           候选记忆：编号、上限、批准、丢弃
  brief.py / brief_html.py  晨间简报数据、Markdown 与页面
  serve.py                待办页面（127.0.0.1、令牌、点选回写）
  adapters/               模型适配层（base / fake / proc / claude / agy）
  prompts/                内置中文提示词模板（digest / weekly / dispatch-task / route）
  templates/systemd/      服务单元模板
  templates/vault/        初始化默认模板（内置模板正文目前只有中文）
examples/vault-demo/      虚构示例记忆库（自由译者「林晓」）
tools/privacy_scan.py     个人隐私信息正则扫描工具
tests/                    单元测试套件
```
