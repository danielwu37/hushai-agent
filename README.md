# hushAi-agent

> **跨设备、跨厂商的个人 AI 记忆中枢与集中调度管家。**
>
> 把你在**办公室电脑、家庭笔记本、手机端**与**不同 AI 应用**（Claude Code、Antigravity / `agy`、Muse、Duet 等）的对话、决策、待办与阅读思考，沉淀进你自己的**纯文本（Markdown）记忆库**。由部署在你自己服务器上的中枢统一对账、互链、调度多模型容错执行；换掉任何一家 AI，你的记忆与工作流始终完整在手。

[![GitHub release](https://img.shields.io/github/v/release/danielwu37/hushai-agent?style=flat-square)](https://github.com/danielwu37/hushai-agent/releases)
[![License](https://img.shields.io/github/license/danielwu37/hushai-agent?style=flat-square)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](./pyproject.toml)

---

## 为什么需要 hushAi-agent

今天绝大多数人每天都在多台设备、多个 AI 工具之间来回切换——办公室电脑用终端 AI 写方案、家里笔记本用知识库整理研究、手机上用对话 App 随手记灵感。没有统一中枢时，痛点非常明显：

1. **多端割裂**：在公司电脑和 Claude 探讨过的架构取舍，回家换笔记本或出门用手机时，另一端的 AI 完全不知情，只能重新交代上下文。
2. **厂商锁定**：记忆散落在各个封闭云端账号里。某家模型额度耗尽、服务波动或你想换更强的新模型时，多年积累的偏好、项目背景与历史经验无法迁走。
3. **只聊不沉**：每天大量的调研、阅读、会议约定和决策要点留在聊天记录里，既没有转化成可追踪的行动推进，也没有沉淀为彼此关联的长期知识网络。

**hushAi-agent 的解法**：以你服务器上的一个标准 Markdown 文件夹为**唯一事实源（Single Source of Truth）**，建立**「多端受控写入 → 中枢集中整理与调度 → 多模型互为备份 → 长期知识关联沉淀」**的闭环。

---

## 核心能力全景

| 能力维度 | 具体做什么 |
| --- | --- |
| **多端统一记忆底座** | 所有设备与 AI 端通过受限闸门（`gate`）向 `hub/inbox/` 只追加写收件，支持 `line_id` 确定性去重与会话结束自动采录（`capture`）；`local_only`（如健康、私密目录）永不发送给外部模型、永不同步出本机。 |
| **集中调度与多模型容错** | `digest` / `weekly` 等定时任务支持按档位配置多模型回退链（如 `claude:light,agy:light`），主引擎失败或超预算自动降级切换；常驻管家（`steward`）也可在 `claude` 与 `agy` 之间一键热切换。 |
| **行动与决策闭环** | 收件变化触发秒级机械对账（`reconcile`），每晚 AI 汇总开放项、状态索引与晨间简报（`brief`），支持命令行与本机 Web 界面（`serve`）直接销账，长期沉淀 `decisions.md` 与 `lessons.md`。 |
| **个人第二大脑（Second-Brain）** | 自动将收件与长文章加工为 `hub/index.md` 主题索引 + 原子笔记（`notes/`）、带原句/出处的来源档案（`sources/`）、人物承诺档案（`people/`）与项目脉络（`projects/`）；强制每篇新笔记至少关联 2 篇旧笔记；提供**零模型编造、纯笔记引证**的 `hushai brain ask` 问答、每日旧笔记唤醒（`resurface`）与月度维护。 |
| **零记忆成本交互菜单** | 提供 **`hushai config`** 交互式数字菜单，从时区、AI 适配器、多端接入到第二大脑开关、日常查看待办、手工触发整理，全部按 `1..9` 菜单完成，不用背命令行。 |
| **后台自愈与零维护** | 每小时静默体检（`heal`）：检查定时器、写权限、磁盘、挂载点与 `tmux` 管家存活，白名单问题自动拉起恢复并 24 小时去重上报；记忆库自身是 Git 仓库，每次整理自动提交可随时回滚。 |

---

## 跨端使用的一天是怎么运转的

```text
早上 · 办公室电脑
  打开终端 AI（Claude Code / agy），SessionStart 自动拉取昨夜中枢整理的
  「晨间简报」与待办交接，直接接续昨天的上下文推进工作；结束对话自动采集摘要入库。

白天 · 手机端（外出/会议间隙）
  打开手机 Claude App、Muse 或待办简报页：语音转述一条决策或人员承诺、
  将一篇长参考资料丢进 hub/exchange/in/、顺手勾选已完成的待办，秒级触发对账。

晚上 · 服务器中枢自动调度
  21:00/21:30 集中读取全部设备当日收件与交换区：
  - 更新开放待办与项目近况；
  - 将文章/笔记拆分为原子笔记并自动建立 ≥2 条跨笔记关联、更新 index.md；
  - 输出当日 3 行总结与次日简报（无新增内容则保持静默）。

周末与家庭 · 家庭笔记本
  通过 rclone 单向同步直接在 Obsidian 或本地工具里翻看全部沉淀的笔记与索引；
  每周日自动完成近况重写、本周反复出现的思路追踪、观点冲突排查与未兑现承诺提醒。
```

---

## 快速安装（纯 Python 3.10+ 标准库，零第三方依赖）

### 准备条件
- **一台常开 Linux 主机 / VPS**（Ubuntu 22.04+ 或 Debian 12+，2 GB 内存即可）
- **基础工具**：Python 3.10+、`git`、`tmux`（`sudo apt install -y git tmux`），可选 `rclone`（用于网盘同步）
- **至少一个 AI CLI**（推荐同时准备两个以便互为备份）：[Claude Code](https://docs.anthropic.com/en/docs/claude-code)（`claude`）与/或 Antigravity CLI（`agy`）

### 安装步骤

**方式 A：让服务器上的 AI 帮你装（推荐）**
在服务器上打开 `claude` 或 `agy`，发给它：

```text
请帮我部署 https://github.com/danielwu37/hushai-agent.git：
1. 克隆到 /opt/hushai-agent（非 root 用户先创建目录并赋权：sudo mkdir -p /opt/hushai-agent && sudo chown "$USER:$USER" /opt/hushai-agent），将 /opt/hushai-agent/bin/hushai 软链到 ~/.local/bin/hushai 并确保 PATH 包含 ~/.local/bin。
2. 先询问我想使用的实例名称（默认为 my）和记忆库路径（默认 ~/hushai-vault），再运行初始化向导：hushai install wizard --instance <实例名> --vault <记忆库路径> --timezone Asia/Shanghai --lang zh --steward claude
3. 把向导输出的服务端命令（systemd）和体检结果列给我确认后再启用。
```

**方式 B：自己动手**

```bash
# 若当前不是 root，先建目录并赋权：sudo mkdir -p /opt/hushai-agent && sudo chown "$USER:$USER" /opt/hushai-agent
git clone https://github.com/danielwu37/hushai-agent.git /opt/hushai-agent
mkdir -p ~/.local/bin && ln -sf /opt/hushai-agent/bin/hushai ~/.local/bin/hushai
export PATH="$HOME/.local/bin:$PATH"

# 方式 B-1：交互向导（按提示输入实例名[默认 my] 与记忆库路径[默认 ~/hushai-vault]）
hushai install wizard

# 方式 B-2：或一行参数直通初始化（可将 my 换成任意小写实例名）
hushai install wizard --instance my --vault ~/hushai-vault --timezone Asia/Shanghai --lang zh --steward claude
```

---

## 日常首选入口：`hushai config` 菜单控制台

普通用户无需记住各种子命令和参数。安装完成后，只需要记住这一条命令：

```bash
hushai config
```

进入后即可看到清晰的 `1..9` 分类菜单，所有可配置项与常用操作都在里面交互完成（所有修改先校验、失败自动回滚，绝不写坏配置）：

```text
==================================================
  hushAi-agent 设置与控制中心  [实例: my]
  记忆库: ~/hushai-vault
  时区: Asia/Shanghai | 语言: zh | 适配器: claude,agy | 第二大脑总开关: 已禁用
==================================================
  1. 基础与记忆库设置（时区、语言、路径、仅本机目录、篇幅与窗口限制）
  2. 领域与接入端管理（领域列表、管家端、已知端、审批权、交换区）
  3. AI 适配器与模型调度（claude/agy/fake、回退链、派活、每日预算、自检）
  4. 常驻管家管理与跨端路由（查看/切换 claude|agy/重启、@路由）
  5. 多端接入与同步（rclone 同步、doorkit、systemd 单元、电脑/手机接入速查）
  6. 第二大脑 (Second-Brain) 设置与操作（开关、引导、Ask、加工、Routine）
  7. 日常查看与待办操作（今日待办、晨间简报、记收件、销待办、批记忆）
  8. 手动触发整理与系统维护（reconcile/digest/weekly/heal/doctor/export/upgrade/uninstall）
  9. 切换或新建实例
  0. 退出
```

> 首次启用第二大脑时，进入 **菜单 6 → 第 3 项** 运行「首次运行 5 问引导」，回答你正在做的项目、想记住的事与阅读偏好后，系统即可自动梳理首批条目并生成 `"HERE'S YOUR BRAIN"` 知识地图。第二大脑的所有定时 Routine（`daily`、`weekly`、`resurface`、`monthly`）**默认全部禁用**，可在菜单 6 随时按需单独打开或关闭。

---

## 不同设备、不同 AI 应用怎么接入这个中枢

以下接入所需的关键配置和文件生成，都可以直接在 **`hushai config` 菜单 2 / 4 / 5** 中完成：

### 1. 办公室电脑 & 家庭笔记本（桌面多端协同）

| 使用工具 | 接入方法 | 实现效果 |
| --- | --- | --- |
| **Claude Code / Antigravity (`agy`)** | 1. 在菜单 `2` 将 `office`、`laptop` 加入已知接入端；<br>2. 在菜单 `5` 生成对应 `doorkit`（附 `SessionStart`/`Stop` 钩子模板）；<br>3. 远端电脑通过 SSH 强制命令连接服务器 `hushai gate ssh`。 | 每次在任意一台电脑打开终端 AI，自动同步最新简报和待收消息；退出会话自动记录产出与待办，公司和家里无缝接力。 |
| **Obsidian / 本地文本编辑器** | 在菜单 `5` 配置 `[sync]` 远端（如 `onedrive:hushai-vault`），通过 `hushai sync` 或菜单第 `5 → 2` 项执行按归属单向同步（不自带后台定时任务，可按需加入 cron）。 | 服务器把整理好的 `today.md`、`index.md`、`notes/`、`sources/`、`people/`、`projects/`、`daily/`、`weekly.md`、`reports/` 推向网盘，本地电脑只往自己所属端写文件，永不冲突覆盖；`local_only`（如 `domains/health`）与 `domains/` 绝不上云。 |

### 2. 手机端（Claude、Muse、Duet 与浏览器）

| 手机端场景 | 接入方式 | 能做什么 |
| --- | --- | --- |
| **Claude App（iOS / Android）** | 将常驻管家切换为 `claude`（菜单 `4`），服务器后台通过 `tmux` 保活远程管家会话；打开手机 Claude App → **Code** 连接对应远程会话。 | 随时随地像聊天一样调度整个中枢：语音记事、查历史决策、问第二大脑、让中枢 `dispatch` 后台任务给重型模型处理。 |
| **Muse / 支持受限 SSH 的手机客户端** | 在服务器 `~/.ssh/authorized_keys` 为手机端公钥写入强制命令：<br>`restrict,command="/usr/bin/python3 -I /opt/hushai-agent/bin/hushai-gate-ssh --instance my --door phone" ...` | 手机只能在受限闸门下安全读写收件、交换区与报告（`ping/ls/read/append-inbox/put/verify`），兼容旧 `00-我/...` 映射，绝不会误删或越权读取。 |
| **手机浏览器（任意系统通用）** | 运行 `hushai serve --instance my`（仅监听 `127.0.0.1`），通过 SSH 端口转发（如 Termius 本地转发 `8765` 端口）打开带令牌页面。 | 一屏查看「晨间简报」，点一下完成待办、推迟任务、批准或丢弃候选记忆，0.1 秒内即时更新状态。 |
| **Duet 等网盘文件型应用** | 手机端接入同一套 `rclone`/网盘同步目录。 | 只读浏览 `hub/reports/morning-brief.md`、`hub/index.md` 与 `hub/notes/`；若需写入，可将草稿存入交换区或对应端收件文件，待同步回服务器后集中入库。 |
| **ChatGPT 等暂未开放受限本地闸门的 App** | **当前版本边界**：暂未提供原生直连插件。 | 现阶段建议将有价值的回复、摘要通过手机/电脑管家或交换区 `hub/exchange/in/` 投入中枢，即刻拆解沉淀为 `sources/` 与互链 `notes/`。 |

详细协议见 [多端接入规范](./docs/doors.md)。

---

## 记忆库目录结构

```text
~/hushai-vault/                    ← 纯文本记忆库（Git 仓库，可整体迁走）
├── hub/
│   ├── index.md                   ← 第二大脑主题索引（每主题一行，硬上限 200 行）
│   ├── weekly.md                  ← 第二大脑周回顾（反复想法、观点冲突、未兑现承诺）
│   ├── inbox/                     ← 多端统一收件：按日、按端分文件，只追加不覆盖
│   ├── exchange/                  ← 跨端文章/大文件交换区（in/ 与 out/）
│   ├── today.md                   ← 今日待办（最多 7 条）
│   ├── open-items.md              ← 全量开放追踪项
│   ├── status.md / status-index.md← 项目近况及索引
│   ├── profile.md                 ← 个人画像（必须经你批准才会写入）
│   ├── decisions.md / lessons.md  ← 长期决策与教训档案
│   ├── notes/                     ← 加工后的原子笔记（至少 2 条互链）
│   ├── sources/                   ← 文章/视频/书籍来源档案与带出处原句
│   ├── people/                    ← 人物关系、聊过什么与承诺兑现记录
│   ├── projects/                  ← 项目目标、开放问题、决定原因与下一步
│   ├── daily/                     ← 按日归档的每日笔记（YYYY-MM-DD.md）
│   └── reports/                   ← 日报、周报、晨间简报与月度维护清单
└── domains/                       ← 按领域划分的材料目录（health 默认仅本机，永不外发）
```

---

## 常用命令行速查（脚本/自动化用户参考）

日常推荐直接使用 `hushai config`。以下列出对应核心 CLI 命令便于脚本集成与排错：

```bash
# 查看与操作日常闭环
hushai today --instance my
hushai brief --instance my
hushai todo done 1 --instance my
hushai inbox add --instance my --door office --domain 工作 --did "..." --todo "..."
hushai digest --instance my
hushai weekly --instance my

# 常驻管家、同步与自愈体检
hushai steward status --instance my
hushai steward use agy --instance my     # 在 claude 与 agy 之间热切换
hushai sync --instance my --direction both
hushai heal --instance my
hushai doctor --instance my

# 个人第二大脑（Second-Brain）
hushai brain onboard --instance my --interactive
hushai brain ask "为什么当初决定采用纯标准库方案？" --instance my
hushai brain process --instance my
hushai brain enable all --instance my    # 手动启用第二大脑定时 routine

# 卸载与环境重置（默认永久保留 vault 记忆库；也可在 hushai config -> 8 -> 8 交互完成）
hushai uninstall --instance my --with-systemd            # 先演练预览，加 --yes 只删配置/状态/systemd 并保留 vault；真要连数据销毁需加 --delete-vault
/opt/hushai-agent/bin/hushai-uninstall                   # 独立一键交互卸载脚本（默认永久保留 vault）
```

查看完整参数帮助：`hushai --help` 或 `hushai brain --help`。

---

## 当前版本的诚实边界（v0.2.1）

- **建议使用独立系统用户或 root 部署服务端定时单元**：使用受限普通用户安装时，业务功能可正常运行，但 `heal` 自愈模块重启 `systemd` 服务可能因权限不足转为在 `today.md` 顶部告警提醒。
- **Claude 远程会话继承服务器全局配置**：`claude` 远程会话模式会读取同账号全局 `~/.claude/` 配置；推荐为 `hushai` 分配专用用户或专用服务目录以隔离规则。
- **「Claude 额度耗尽自动切 `agy`」在测试桩验证通过，真实生产账单报错样式仍在持续跟踪**：手动与菜单切换（`hushai steward use agy`）经实际验证可靠。
- **ChatGPT 等封闭云端应用**尚未开放本地闸门直连，现阶段需通过复制投递至 `hub/exchange/in/` 或由管家转存。
- **Second-Brain 所有周期 Routine 默认处于禁用状态**：安装与升级不会擅自开启后台自动写入，也不会未经 `--yes` 同意擅自删除或合并任何已有笔记。启用 `monthly` 或修改第二大脑时间参数后，需在菜单 `5` 或 `hushai install units --with-brain` 重新生成并启用对应 systemd 定时器；人物与项目档案按显式 `人物：`/`项目：`/`@姓名` 前缀触发提取。

---

## 更多文档与测试

- [使用者手册](./docs/user-guide.md) | [安装与运维手册](./docs/admin-guide.md)
- [多端接入规范（doors / gate）](./docs/doors.md)
- [Second-Brain 集成设计](./docs/second-brain.md) | [核心设计](./docs/design.md) | [文件格式规范](./docs/spec.md)
- [更新日志](./CHANGELOG.md)

运行全量测试（不依赖外部网络或真实 API 密钥）：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

[MIT License](./LICENSE)
