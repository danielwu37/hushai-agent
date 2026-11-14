# hushAi-agent

> 一个装在你自己服务器上的 AI 管家。它把你和各种 AI 聊过、办过的事记在一个普通文件夹里，每晚帮你整理成「明天要办什么」，第二天早上给你一份简报。换哪家 AI 都能接着用，记忆始终是你自己的。

[![GitHub release](https://img.shields.io/github/v/release/danielwu37/hushai-agent?style=flat-square)](https://github.com/danielwu37/hushai-agent/releases)
[![License](https://img.shields.io/github/license/danielwu37/hushai-agent?style=flat-square)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square)](./pyproject.toml)

---

## 它解决什么问题

如果你同时用好几个 AI（手机上一个、电脑上一个、服务器上一个），大概遇到过这些情况：

- 在 A 里说过的事，B 不知道，每次都要重新交代背景；
- 聊完就忘，答应要办的事散在各个聊天记录里，没人提醒；
- 某家 AI 额度用完或停服，之前积累的「它对你的了解」一起没了。

hushAi-agent 的做法很简单：**所有 AI 都把「做了什么」记到同一个文件夹，由它每天整理一次。** 文件夹里全是能直接打开看的文本文件，拷走就能换机器、换 AI。

## 它能做什么

| 功能 | 说明 |
| --- | --- |
| 统一记事本 | 每个 AI、每台设备做完一件事，就往当天的「收件」里记一行。会话结束时也能自动记 |
| 每晚整理 | 每晚自动把当天的收件读一遍，整理出新的待办，关掉办完的，更新各项目近况 |
| 今日待办和晨间简报 | 每天早上给一份清单：要你亲自办的事（最多 7 条）、等你拍板的事、AI 想记住的关于你的新信息（你批准了才记） |
| 常驻管家 | 服务器上一直开着一个 AI 对话，手机上随时能找它。用 Claude 还是 Antigravity（agy）由你选，一条命令切换 |
| 两家 AI 互为备份 | 整理任务先用一家，失败自动换另一家；两家都不行时也会出一份简化版待办，不会整晚空着 |
| 自己检查、自己修 | 每小时自查一次：定时任务有没有跑、文件能不能写、管家在不在线。能修的自己修，只记一笔；修不好才在待办顶上告诉你 |
| 安全边界 | 别的设备只能通过受限的 SSH 入口读写指定目录；整理时交给 AI 的只有文字，AI 不能动你的文件、不能执行命令；标成「仅本机」的目录（比如健康资料）永远不会发给 AI 或同步出去 |

**它不做什么**：不做聊天 App，不建数据库，不需要你学新软件。对话还是在你原来用的 AI 里进行。

## 一天是怎么过的

```text
白天   你在手机上跟管家说「约了周六带孩子去牙医」
       → 管家记一行收件
晚上   每晚整理：发现一件新待办「周六去牙医」
早上   晨间简报：要我办 1. 周六上午 10 点去牙医
办完   你说「1 办完了」（或在待办页面点一下）
       → 记一行收件，待办马上打勾，晚上整理时正式关掉
```

---

## 开始之前要准备什么

| 准备 | 要求 |
| --- | --- |
| 一台 Linux 服务器 | Ubuntu 22.04+ 或 Debian 12+，内存 2 GB 够用 |
| 基础软件 | Python 3.10+、git、tmux（`sudo apt install -y git tmux`） |
| 至少一个 AI 命令行工具 | [Claude Code](https://docs.anthropic.com/en/docs/claude-code)（`claude`）或 Antigravity CLI（`agy`），装好并登录过 |

不需要装任何 Python 包。本项目只用 Python 自带的标准库。

## 安装

### 方式一：让 AI 帮你装（推荐）

在服务器上打开 Claude Code 或 agy，把下面这段话发给它（尖括号里的换成你自己的）：

```text
请把 https://github.com/danielwu37/hushai-agent.git 安装到这台服务器：
1. 克隆到 ~/hushai-agent，把 ~/hushai-agent/bin/hushai 软链到 ~/.local/bin/hushai，确保 PATH 里有 ~/.local/bin。
2. 运行：hushai install wizard --instance my --vault ~/hushai-vault --timezone <我的时区，如 Asia/Shanghai> --lang zh --steward <claude 或 agy>
   如果 claude 或 agy 不在 /usr/local/bin、~/.local/bin，把它们的绝对路径写进 ~/.config/hushai/my/config.ini 的 [adapters] 段（claude.command、agy.command）。
3. 把向导最后打印的 systemd 命令列给我，我确认后再执行。
4. 执行后运行 hushai doctor --instance my 和 hushai steward status --instance my，把真实输出给我看。
```

### 方式二：自己装

```bash
# 1. 下载，并让系统认得 hushai 命令
git clone https://github.com/danielwu37/hushai-agent.git ~/hushai-agent
mkdir -p ~/.local/bin && ln -sf ~/hushai-agent/bin/hushai ~/.local/bin/hushai
export PATH="$HOME/.local/bin:$PATH"    # 建议也写进 ~/.bashrc

# 2. 安装向导：建好记忆文件夹 ~/hushai-vault，并生成后台服务文件
hushai install wizard --instance my --vault ~/hushai-vault --timezone Asia/Shanghai --lang zh --steward agy

# 3. 向导最后会打印三条 sudo 命令（复制服务文件、重新加载、启用），照着执行

# 4. 检查
hushai doctor --instance my
```

`--instance my` 是这一套的名字，可以随便取；后面所有命令都用同一个名字。

---

## 装好以后先试一试

下面 5 条命令手动走一遍完整流程，不用等到晚上：

```bash
# 记一件事（平时由 AI 自动记，这里手动模拟）
hushai inbox add --instance my --door steward --domain 生活 --did "约了周六带孩子去牙医" --todo "周六上午 10 点去牙医"

# 马上整理一次（平时每晚自动跑，会调用一次 AI）
hushai digest --instance my

# 看今日待办
hushai today --instance my

# 看晨间简报
hushai brief --instance my

# 办完了，打勾（1 是待办里的序号）
hushai todo done 1 --instance my
```

## 在手机上用

| 管家用的 AI | 手机上怎么找它 |
| --- | --- |
| Claude | 打开 Claude App → Code，找到名为「hushAi 管家」的远程会话（Remote Control） |
| agy | 用手机上的 SSH App（如 Termius）登录服务器，运行 `tmux attach -t hushai-my` |

对管家说话就像平时聊天。常用说法：

- 「记一下：……」 → 写进收件
- 「今天要办什么」 → 读今日待办
- 「2 办完了」「3 推迟到下周一」「4 不做了」 → 回写待办
- 「批准候选记忆 C-xxxx」 → 让它记住一条关于你的新信息

## 常用命令

| 想做什么 | 命令 |
| --- | --- |
| 看今日待办 | `hushai today --instance my` |
| 打勾、推迟、放弃 | `hushai todo done 1` / `todo defer 1 --to 2026-12-01` / `todo drop 1`（都加 `--instance my`） |
| 看晨间简报 | `hushai brief --instance my` |
| 看管家状态 | `hushai steward status --instance my` |
| 管家换成 Claude 或 agy | `hushai steward use claude --instance my` |
| 手动自检一次 | `hushai heal --instance my` |
| 全面体检 | `hushai doctor --instance my` |
| 打开本机待办网页 | `hushai serve --instance my --port 8765`（只监听本机，见[使用者手册](./docs/user-guide.md)） |
| 导出全部数据 | `hushai export --instance my --out ~/hushai-backup.tar.gz` |

所有命令：`hushai --help`。

## 你的数据放在哪

```text
~/hushai-vault/              ← 记忆文件夹，全是文本文件，可以直接打开看
├── hub/
│   ├── inbox/               ← 收件：每台设备每天一个文件，只追加不改
│   ├── today.md             ← 今日待办
│   ├── open-items.md        ← 所有没办完的事
│   ├── status.md            ← 各项目近况
│   ├── profile.md           ← 关于你的画像（你批准过才会写进来）
│   ├── decisions.md         ← 你拍过板的决定
│   ├── lessons.md           ← 出过的问题和教训
│   └── reports/             ← 日报、周报
└── domains/                 ← 按领域放的资料；health 默认「仅本机」
~/.config/hushai/my/         ← 配置（config.ini）和生成的服务文件
~/.local/state/hushai/my/    ← 运行记录（每次任务调了哪家 AI、花了多久）
```

记忆文件夹本身是一个 git 仓库，每次整理后自动提交，改错了可以回滚。

## 后台会自动做的事

| 什么时候 | 做什么 |
| --- | --- |
| 收件一有变化 | 立刻处理「办完、推迟、放弃」这类不需要 AI 判断的动作 |
| 每晚（默认 21:30） | 用 AI 整理当天收件，生成明天的待办和日报 |
| 每周日晚 | 重写各项目近况，列出过期没动、办了没销、互相矛盾的事 |
| 每小时 | 静默自检，能修的自己修 |
| 一直 | 管家会话在线，掉线自动拉起 |

---

## 目前的限制（v0.1，请先看）

- **建议用 root 安装。** 用普通用户装，服务能正常跑，但「自己修」需要重启服务，没有权限，会改成在待办顶上提醒你。用户级服务的支持在计划中。
- **Claude 管家会读到这台服务器上 Claude 的全局设置。** 远程会话模式不支持隔离参数，`~/.claude/CLAUDE.md` 里的全局规则会和管家规则混在一起。服务器专门给管家用就不受影响。
- **「Claude 额度用完自动换 agy」还没有用真实的额度报错验证过。** 手动切换（`hushai steward use agy`）是可靠的。
- **agy 管家在手机上**目前只验证过 SSH 方式。
- 修复记录见 [CHANGELOG.md](./CHANGELOG.md)。

## 更多文档

- [使用者手册](./docs/user-guide.md)：日常怎么用、待办网页
- [安装与维护手册](./docs/admin-guide.md)：多实例、同步、自愈、升级与卸载
- [多端接入](./docs/doors.md)：让其他设备、其他 AI 读写同一份记忆
- [设计文档](./docs/design.md)、[文件格式规范](./docs/spec.md)
- 只想先看看样子：`hushai validate ~/hushai-agent/examples/vault-demo`，里面是虚构人物的示例数据

## 参与与协议

欢迎提 Issue 和 PR。提交前请跑通测试：`PYTHONPATH=src python3 -m unittest discover -s tests -t .`

[MIT License](./LICENSE)
