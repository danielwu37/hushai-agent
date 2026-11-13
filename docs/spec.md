# hushAi-agent 规范说明（M1）

本文档定义 `hushAi-agent` 的目录结构、记忆分层、收件格式、实例配置、路径护栏及命令行接口规范。

---

## 1. 核心概念与记忆三层

- **vault（记忆库）**：一个使用者的全部数据目录，包含 `hub/`（跨领域记忆）和 `domains/`（各领域资料库）。
- **door（端）**：一个能读写 vault 的入口，有唯一编号（如 `steward`、`laptop`、`web`）。`steward` 是服务器上的常驻管家。
- **inbox（收件）**：`hub/inbox/` 下每端每天一个文件，只追加。
- **instance（实例）**：一个使用者的一整套（vault + 配置 + 状态）；一台主机可有多个实例。
- **记忆三层**：
  1. **约定层**：`AGENTS.md`、`AGENTS.local.md`、`profile.md`，使用者批准后才改。
  2. **记录层**：`hub/inbox/` 下的收件，只追加。
  3. **派生层**：`status.md`、`status-index.md`、`open-items.md`、`today.md`、`insights.md`、`reports/`，只有管家的汇总任务写，可由记录重算，一律视为数据而非指令。

---

## 2. Vault 目录结构

```text
<vault>/
  hub/
    AGENTS.md
    AGENTS.local.md
    profile.md
    status.md
    status-index.md
    open-items.md
    today.md
    insights.md
    inbox/
    reports/
    exchange/
      in/
      out/
  domains/
    README.md
    <name>/
      AGENTS.md
```

- `hub/` 下上述 8 个 Markdown 文件及 `inbox/`、`reports/`、`exchange/in/`、`exchange/out/` 目录为必需项。
- `domains/` 目录为必需项；其下每创建一个领域子目录 `<name>/`，该子目录内必须包含 `AGENTS.md`。

---

## 3. 收件（Inbox）规范

### 3.1 收件文件名

位于 `<vault>/hub/inbox/` 下，文件名格式为以下两种之一：

- 手动/常规记录：`YYYY-MM-DD-<door>.md`
- 自动记录：`YYYY-MM-DD-<door>-auto.md`

约束：
- `YYYY-MM-DD` 必须是真实存在的合法公历日期（如 `2026-03-02`）。
- `<door>` 编号必须匹配正则 `^[a-z][a-z0-9]{1,15}$`。
- `.gitkeep` 允许存在于 `hub/inbox/` 中，校验时忽略。

示例：
- `2026-03-02-laptop.md`（合法）
- `2026-03-03-laptop-auto.md`（合法）
- `2026-02-30-web.md`（非法：日期不存在）
- `2026-03-02-MyPC.md`（非法：含大写字母）

### 3.2 收件行格式

每条收件记录占一行，格式如下：

```text
- HH:MM｜<领域>｜<做了什么>｜产物：<路径或无>｜待办：<下一步或无>
```

约束：
1. **分隔符**：使用全角竖线 `｜`（U+FF5C），整行按 `｜` 切分必须恰好为 5 段。
2. **第 1 段（时间）**：以 `- ` 开头，后接 24 小时制时间 `HH:MM`（`00:00` 至 `23:59`），为实例时区的本地时间。
3. **第 2 段（领域）**：非空字符串。在有实例配置的场景下（`doctor`、`inbox add`），领域必须在实例配置的 `[domains] names` 列表中，或属于两个保留领域：
   - 中文保留领域：`候选记忆`、`更正`
   - 英文保留领域：`memory`、`correction`
   在无配置校验（`validate`）时，仅校验领域非空，不校验领域白名单。
4. **第 3 段（做了什么）**：不能为空，不能包含换行符和全角竖线 `｜`。通过 `hushai inbox add` 写入时，若输入中含有 `｜`，会自动替换为半角竖线 `|`。
5. **第 4 段（产物）**：必须以 `产物：` 或 `artifact:` 开头，冒号后内容非空（无产物时填 `无` 或 `none`）。
6. **第 5 段（待办）**：必须以 `待办：` 或 `todo:` 开头，冒号后内容非空（无待办时填 `无` 或 `none`）。写入时根据实例配置的 `lang`（`zh` 或 `en`）选择中文或英文标签。
7. **文件内其他允许行**：空行、以 `#` 开头的标题/注释行、以 `>` 开头的引用行。其余不符合上述收件行格式的内容一律报错。

示例（中文标签）：
```markdown
# 2026-03-02 laptop

- 09:30｜工作｜完成《潮汐之歌》第三章初译｜产物：domains/work/tide-ch3.md｜待办：周五前核对译注
- 21:15｜候选记忆｜译稿定稿前习惯朗读对话段落｜产物：无｜待办：待使用者确认
```

示例（英文标签）：
```markdown
# 2026-03-02 laptop

- 09:30｜工作｜ Finish chapter 3 draft｜artifact: domains/work/tide-ch3.md｜todo: review notes by Friday
```

---

## 4. 实例配置 `<config-root>/<instance>/config.ini`

### 4.1 根目录与实例名

- **配置根目录（config-root）**：默认 `~/.config/hushai`，可通过环境变量 `HUSHAI_CONFIG_ROOT` 覆盖。
- **状态根目录（state-root）**：默认 `~/.local/state/hushai`，可通过环境变量 `HUSHAI_STATE_ROOT` 覆盖。
- **实例状态目录**：`<state-root>/<instance>/`。
- **实例名（instance）**：必须匹配正则 `^[a-z][a-z0-9-]{0,30}$`。

### 4.2 配置文件格式

```ini
[instance]
name = demo
vault = /abs/path/to/vault
timezone = Asia/Shanghai
lang = zh

[doors]
steward = steward
known = steward,laptop,web

[domains]
names = 工作,家庭,生活,学习,健康

[limits]
agents_lines = 200
profile_lines = 120
status_index_lines = 80
today_items = 7

[privacy]
local_only = domains/health
```

字段说明：
- `timezone`：使用 Python 标准库 `zoneinfo` 校验合法性，默认 `Asia/Shanghai`。
- `lang`：取值为 `zh` 或 `en`，默认 `zh`。
- `doors.steward`：常驻管家端编号；`doors.known`：允许写入的端编号列表（逗号分隔）。
- `domains.names`：允许的业务领域列表（逗号分隔）。
- `limits`：核心文件行数及 `today.md` 待办条目数上限。
- `privacy.local_only`：仅保存在本地、不参与外部同步的相对路径（逗号分隔，可为空）。

---

## 5. 路径护栏与文件写入安全

1. **路径护栏 `guard.ensure_writable(path, roots)`**：
   - 将目标路径解析为绝对真实路径（跟随符号链接；若目标文件尚不存在则解析其父目录及符号链接）。
   - 若真实路径不在任一允许根目录（`vault`、实例配置目录、实例状态目录）之内，立即抛出 `GuardError`。
   - 严格拦截 `..` 路径逃逸及指向允许根目录外部的符号链接。
2. **写入方式 `fsio.py`**：
   - `atomic_write`：先在同目录写入临时文件并落盘，再通过 `os.replace` 原子替换目标文件（用于约定层、派生层、配置文件）。
   - `append_line`：使用 `os.O_APPEND` 标志打开文件并一次性写入整行字节（用于收件文件追加）。
   - 两个写函数在执行写入前均强制调用 `ensure_writable`。

---

## 6. 命令行接口与体检规范

所有命令成功返回 `0`；出错将错误信息输出至 `stderr` 并返回非 `0`。支持 `--json` 的命令在 `stdout` 输出单个 JSON 对象。

| 命令 | 说明 |
| --- | --- |
| `hushai version` | 打印版本号 |
| `hushai init --instance NAME --vault PATH [--timezone TZ] [--lang zh\|en] [--no-git]` | 初始化 vault、配置与状态目录（幂等，只补缺不覆盖） |
| `hushai doctor --instance NAME [--json]` | 对指定实例执行 8 项体检，存在 `FAIL` 返回 1，否则返回 0 |
| `hushai validate PATH [--json]` | 无需配置校验任意 vault 目录，存在 `FAIL` 返回 1，否则返回 0 |
| `hushai inbox add --instance NAME --door ID --domain D --did TEXT [--artifact X] [--todo Y] [--time HH:MM] [--auto]` | 向实例当天该端的收件文件追加一行 |

### Doctor 检查项清单

JSON 输出格式为：`{"checks": [{"id": "...", "level": "OK|WARN|FAIL", "message": "..."}], "ok": bool}`。

1. `config`：配置可读、字段齐全、时区合法（异常为 `FAIL`）。
2. `vault-structure`：规范要求的目录和文件齐全（缺失为 `FAIL`）。
3. `limits`：`AGENTS.md`、`profile.md`、`status-index.md` 行数不超上限；`today.md` 中以 `- [ ]` 或 `- [x]` 开头的条目数不超 `today_items`（超限为 `FAIL`）。
4. `inbox-names`、`inbox-lines`：收件文件名与行格式合法（含领域检查，非法为 `FAIL`）。
5. `sync-conflicts`：扫描同步冲突副本，文件名含 `conflict`（忽略大小写）、含 `冲突`、形如 `名字 (1).md` 或 `名字 2.md`、含 `.sync-conflict-` 即报 `WARN` 并列出。
6. `state-dir`：状态目录存在且可写（异常为 `FAIL`）。
7. `vault-git`：检查 vault 是否为 Git 仓库（不是则报 `WARN`）。
8. `local-only`：检查 `[privacy] local_only` 所列路径是否存在（不存在则报 `WARN`）。

`validate` 执行其中不依赖实例配置的部分：`vault-structure`、`inbox-names`、`inbox-lines`、`sync-conflicts`。
