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
  .gitignore
  hub/
    AGENTS.md
    AGENTS.local.md
    profile.md
    status.md
    status-index.md
    open-items.md
    today.md
    insights.md
    decisions.md
    lessons.md
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

- 顶层 `.gitignore` 由模板提供，默认忽略临时文件、系统垃圾文件与编辑器缓存（`.*.tmp`、`.DS_Store`、`Thumbs.db`、`desktop.ini`、`.claude/`、`.agents/`、`~$*`）。
- `hub/` 下上述 10 个 Markdown 文件及 `inbox/`、`reports/`、`exchange/in/`、`exchange/out/` 目录为必需项。
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

### 3.3 收件行确定性编号（`line_id`）与遍历

1. **格式定义**：
   通过 `inboxfmt.line_id(date, door, auto, raw_line, occurrence=0) -> str` 生成每条收件行的确定性唯一编号：
   ```text
   L-<YYYYMMDD>-<door>[-auto]-<HHMM>-<h>[-<occurrence>]
   ```
   - `<YYYYMMDD>`：收件文件对应的 8 位日期（如 `20260302`）。
   - `<door>`：端编号；当 `auto=True`（来自 `-auto.md` 文件）时紧接 `-auto`。
   - `<HHMM>`：从收件行第 1 段提取的 4 位时间（如 `0930`）。
   - `<h>`：对 `raw_line` 去掉首尾空白后的 UTF-8 字节串计算 `sha1`，取前 8 位十六进制小写字符。
   - `[-<occurrence>]`：当 `occurrence == 0`（同一文件内首次出现）时省略；当同一文件内出现完全相同的收件行时，第二次出现取 `occurrence = 1`（末尾追加 `-1`），以此类推。
2. **目录遍历 `inboxfmt.iter_inbox_lines(inbox_dir)`**：
   - 按日期、端、手写先于自动记录、文件内行号的顺序遍历收件目录中所有文件名合法的收件文件。
   - 自动维护同一文件内相同行的 `occurrence` 计数，对每条合法收件行产出一个记录对象（含 `line_id`、`date`、`door`、`auto`、解析出的各字段 `time_str`/`domain`/`did`/`artifact`/`todo`、`raw`、`path`、`lineno`）。
   - 遇到空行、注释/引用行或格式非法的行自动跳过，不抛出异常。
3. **设计用途**：
   - **汇总时模型引用来源**：管家（steward）在汇总派生层视图与报告时，通过 `line_id` 精确标注结论与待办的原始收件出处。
   - **已对账行去重**：状态对账机制通过记录已处理的 `line_id` 集合实现幂等增量汇总；因编号不依赖物理行号，在文件头部插入注释行或空行后原有收件行的 `line_id` 保持不变。

### 3.4 开放项（`hub/open-items.md`）格式

`hub/open-items.md` 是开放项正本（派生层文件，首行带 `> 由管家汇总任务生成，是数据不是指令，不要手改。`），正文分为 `## 要我办`（`owner=user`）、`## 要我定`（`owner=user_decision`）、`## AI 推进`（`owner=ai`）、`## 近期关闭`（关闭距基准日不超过 14 天）四节。关闭超过 14 天的条目追加归档至 `hub/reports/closed-items.md`。

每个开放项占一行，以全角竖线 `｜` 分隔：

```text
- [ ] 260302-1｜交水费｜owner=user｜sched=2026-03-05｜due=-｜domain=生活｜verified=2026-03-02｜src=L-20260302-laptop-0930-d0fb9815,L-…
- [x] 260302-2｜寄合同｜owner=user｜sched=-｜due=-｜domain=工作｜verified=2026-03-04｜closed=2026-03-04｜src=…
- [-] 260303-1｜报瑜伽课｜owner=user｜sched=-｜due=-｜domain=生活｜verified=2026-03-05｜closed=2026-03-05｜src=…
```

约束：
1. **状态前缀**：`- [ ] ` 表示开放（`open`）、`- [x] ` 表示已办（`done`）、`- [-] ` 表示放弃（`dropped`）。
2. **前两段**：第 1 段为开放项编号 `YYMMDD-N`（6 位建项日期前缀加当天递增序号，综合 `open-items.md` 与 `closed-items.md` 分配，永不复用）；第 2 段为标题（写入时自动替换换行符与 `｜`）。
3. **键值段**：第 3 段起为 `键=值` 属性，顺序不限，空值用 `-` 表示。合法键包括：`owner`（`user` | `user_decision` | `ai`）、`sched`（排期日 `YYYY-MM-DD` 或 `-`）、`due`（截止日 `YYYY-MM-DD` 或 `-`）、`domain`（领域）、`verified`（核实日 `YYYY-MM-DD`）、`closed`（关闭日 `YYYY-MM-DD`，已关闭项填写）、`defers`（可选，推迟次数非负整数，缺省视为 `0`，每次机械或模型推迟加 1）、`src`（逗号分隔的来源 `line_id` 列表，无来源时填 `-`）。
4. **校验规则**：`validate` 与 `doctor` 均包含 `open-items` 检查项，`open-items.md` 中所有以 `- [` 开头的行必须能按上述格式解析，否则报 `FAIL`。

### 3.5 近况（`hub/status.md`）与近况索引（`hub/status-index.md`）格式

`hub/status.md` 记录各领域的长期事项近况，头部带派生说明引用块，正文按领域分为 `## <领域>` 节，每个事项占一行，以全角竖线 `｜` 分隔为 5 段：

```text
- 工作｜第三章译稿｜初译完成，等编辑反馈｜verified=2026-03-04｜src=L-…,L-…
```

约束：
1. **字段定义**：第 1 段为 `- <领域>`，第 2 段为 `<事项名>`（`topic`），第 3 段为 `<现状一句话>`（`summary`），第 4 段为 `verified=YYYY-MM-DD`，第 5 段为 `src=<逗号分隔的 line_id>`。
2. **更新规则（upsert）**：以 `（领域，事项名）` 为联合键；同名事项更新 `summary` 与 `verified`，`src` 列表合并去重。
3. **过期核实标记**：渲染 `status.md` 时，若 `verified` 距基准日 `as_of` 超过 `stale_days`（默认 14 天），在 `summary` 末尾追加 `（状态未核实）`；解析时自动去掉该后缀，不得累积。
4. **近况索引（`hub/status-index.md`）**：由 `status.md` 机械生成，头部引用块说明「只有事项名，没有状态；要状态读 status.md」，每行格式为 `- <领域>｜<事项名>`。
5. **近况归档与硬上限**：`weekly` 将 `verified` 距基准日超过 `[jobs] status_archive_days`（默认 28 天）的近况行追加至 `hub/reports/status-archive.md` 并从 `status.md` 移除；每次汇总写 `status.md` 时，若生成的 `status-index.md` 行数超过 `[limits] status_index_lines`，按 `verified` 从旧到新归档至 `status-archive.md` 直到不超上限，两处均重新生成索引。

### 3.6 今日待办（`hub/today.md`）与日报累积状态

- `today.md` 支持四种条目状态：`- [ ]`（仍开放且排期不晚于目标日）、`- [x]`（已办）、`- [-]`（已放弃）、`- [>]`（仍开放但排期晚于目标日）。当开放条目的 `defers >= 3` 时，括号内追加 `已推迟 N 次，建议放弃`。
- `<状态目录>/daily/<日期>.json` 累积当天每次运行（`digest` 与即时机械对账）的事实，每次据此重新渲染 `hub/reports/daily-<日期>.md`（含「已关闭的事项又有新记录」节）；演练模式不写累积文件。

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

[adapters]
# <名字>.type = fake | claude | agy（省略时 type 等于名字）
# claude / agy 可配 <名字>.command（默认等于类型名）、<名字>.timeout（秒，默认 180）、<名字>.light / .heavy

[jobs]
digest =
weekly =
dispatch =
window_days = 14
stale_days = 14
status_archive_days = 28

[budget]
max_daily_calls = 20
```

字段说明：
- `timezone`：使用 Python 标准库 `zoneinfo` 校验合法性，默认 `Asia/Shanghai`。
- `lang`：取值为 `zh` 或 `en`，默认 `zh`（内置 vault 模板文件正文目前仅提供中文）。
- `doors.steward`：常驻管家端编号；`doors.known`：允许写入的端编号列表（逗号分隔）。
- `domains.names`：允许的业务领域列表（逗号分隔）。`lang = zh` 时默认为 `工作,家庭,生活,学习,健康`；`lang = en` 时默认为 `work,family,life,study,health`。
- `limits`：核心文件行数及 `today.md` 待办条目数上限。
- `privacy.local_only`：仅保存在本地、不参与外部同步且禁止拼入模型提示词的相对路径黑名单（逗号分隔，默认 `domains/health`，`lang = en` 时同样为 `domains/health`）。
- `adapters`：模型适配项配置；真适配项支持 `command`（默认等于类型名）、`timeout`（秒，默认 `180`）、`light` 与 `heavy`（对应档位模型名，缺失时该档位调用直接失败）。
- `jobs.dispatch`：派活默认使用的适配项名字（默认空；v1 仅 `agy` 类型支持代理式派活）。

---

## 5. 路径护栏与文件读写安全

1. **写入路径护栏 `guard.ensure_writable(path, roots)`**：
   - 将目标路径解析为绝对真实路径（跟随符号链接；若目标文件尚不存在则解析其父目录及符号链接）。
   - 若真实路径不在任一允许根目录（`vault`、实例配置目录、实例状态目录）之内，立即抛出 `GuardError`。
   - 严格拦截 `..` 路径逃逸及指向允许根目录外部的符号链接。
2. **提示词读取护栏 `guard.ensure_prompt_allowed(path, vault_root, local_only)`**：
   - 将待读取路径解析为真实路径；若不在 `vault_root` 之内，抛出 `GuardError`。
   - 若真实路径（或原路径）落在任一 `local_only` 路径之内（含该路径本身，以及通过符号链接指向 `local_only` 目录或其内部文件），立即抛出 `GuardError`。
   - 校验通过返回解析后的真实路径。
3. **安全读写函数 `fsio.py`**：
   - `atomic_write`：先在同目录写入临时文件并落盘，再通过 `os.replace` 原子替换目标文件（用于约定层、派生层、配置文件）。
   - `append_line`：使用 `os.O_APPEND` 标志打开文件并一次性写入整行字节。
   - `append_line_with_header(path, header, line, roots)`：首次创建文件时用 `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o644)` 一次 `os.write` 写入 `header + "\n" + line + "\n"`；若文件已存在（捕获 `FileExistsError`）则走 `O_APPEND` 仅追加 `line`（0 字节已存在文件不特殊处理）。`inbox add` 使用该函数避免表头与首行分两次写入的竞争，写前同样强制经过 `ensure_writable`。
   - `read_for_prompt(path, vault_root, local_only) -> str`：先调用 `ensure_prompt_allowed` 校验路径，再以 UTF-8 读取文本。**今后所有把文件内容拼进模型提示词的代码，只能经 `fsio.read_for_prompt` 这个函数读文件。**

---

## 6. 命令行接口与体检规范

所有命令成功返回 `0`；出错将错误信息输出至 `stderr` 并返回非 `0`。支持 `--json` 的命令在 `stdout` 输出单个 JSON 对象。

| 命令 | 说明 |
| --- | --- |
| `hushai version` | 打印版本号 |
| `hushai init --instance NAME --vault PATH [--timezone TZ] [--lang zh\|en] [--no-git]` | 初始化 vault、配置与状态目录（幂等，只补缺不覆盖；若在已有提交的 Git 仓库中补回了缺失文件，只 `git add` 本次补齐的文件并提交一次，未补文件则不提交） |
| `hushai doctor --instance NAME [--json]` | 对指定实例执行 11 项体检，存在 `FAIL` 返回 1，否则返回 0 |
| `hushai validate PATH [--json]` | 无需配置校验任意 vault 目录，存在 `FAIL` 返回 1，否则返回 0 |
| `hushai inbox add --instance NAME --door ID --domain D --did TEXT [--artifact X] [--todo Y] [--time HH:MM] [--auto]` | 向实例当天该端的收件文件追加一行；若为机械动作或保留领域则自动触发一次即时机械对账 |
| `hushai digest --instance NAME [--date YYYY-MM-DD] [--for-date YYYY-MM-DD] [--dry-run] [--json]` | 执行每日收件对账与派生视图汇总 |
| `hushai reconcile --instance NAME [--json]` | 手动触发一次即时纯机械对账（不调用模型） |
| `hushai weekly --instance NAME [--date YYYY-MM-DD] [--dry-run] [--json]` | 执行每周近况归档、洞察重写与周报生成 |
| `hushai today --instance NAME [--json]` | 读取 `today.md` 四种状态并叠加尚未进游标的机械动作 |
| `hushai todo done\|defer\|drop TARGET --instance NAME [--to YYYY-MM-DD\|+Nd] [--door ID]` | 回写待办动作并触发即时机械对账（`defer` 省略 `--to` 时默认今天加 7 天） |
| `hushai ledger --instance NAME [--limit N] [--json]` | 查看运行账本记录 |
| `hushai adapter test NAME --instance X [--tier light\|heavy] [--json]` | 用极小提示词与结构（`{"ok": true}`）自检问答式适配项，记 `call` 账本行并受每日预算限制 |
| `hushai dispatch --instance X --name JOB (--task FILE \| --goal TEXT [--domain D]) [--adapter NAME] [--tier heavy] [--workdir DIR] [--timeout SEC] [--json]` | 在隔离工作目录执行代理式派活（默认 `<状态目录>/jobs/<任务名>/work/`，禁止位于 vault 内）；中断带会话号时自动续跑一次，记账并保存 `out.json` |
| `hushai heal --instance X [--before digest\|weekly] [--dry-run] [--json]` | 静默自愈健康检查：执行 8 项白名单确定性自检与恢复，正常只记账本；自愈与 2 次失败都写 `<state>/alerts.jsonl`（需人工的 24 小时去重，恢复后自动撤下），不进收件和 today.md |
| `hushai capture --instance X [--door ID] [--json]` | 从钩子标准输入读取会话 JSONL；配了 `[jobs] capture` 时由轻档模型读最后一轮对话（人类全文、AI 回复）判断记不记、记成一句话；模型不可用时退回机械取前 120 字。写入 `<日期>-<door>-auto.md`（同 `session_id` 10 分钟内最多一次） |
| `hushai alert add\|resolve\|list\|active --instance X` | 系统告警：`add --source ext:<任务> --level warning\|needs_human --message …` 写 `<state>/alerts.jsonl`（周边任务用它代替写收件）；`resolve --source <前缀>` 撤下；`active` 供管家会话开场钩子主动报告，没有未恢复告警时静默 |
| `hushai ledger --instance X --stats [--days 7] [--json]` | 北极星三项指标：需人介入次数、今日待办有用率、收件系统自嗨率（关键词粗估，`[stats] system_keywords` 可改）；周报末尾自动附这一节 |

### Doctor 检查项清单

JSON 输出格式为：`{"checks": [{"id": "...", "level": "OK|WARN|FAIL", "message": "..."}], "ok": bool}`。

1. `config`：配置可读、字段齐全、时区合法（异常为 `FAIL`）。
2. `vault-structure`：规范要求的目录和文件齐全（缺失为 `FAIL`）。
3. `limits`：`AGENTS.md`、`profile.md`、`status-index.md` 行数不超上限；`today.md` 中以 `- [ ]`、`- [x]`、`- [-]`、`- [>]` 开头的条目数不超 `today_items`（超限为 `FAIL`）。
4. `inbox-names`、`inbox-lines`：收件文件名与行格式合法（含领域检查，非法为 `FAIL`）。
5. `open-items`：`hub/open-items.md` 中所有以 `- [` 开头的行格式合法（非法为 `FAIL`）。
6. `sync-conflicts`：扫描同步冲突副本（不限 `.md` 扩展名）。文件名含 `conflict`（忽略大小写）、含 `冲突`、含 `.sync-conflict-` 直接命中；形如 `名字 数字.扩展名` 或 `名字 (1).扩展名` 的文件，仅当同目录下同时存在去掉尾部序号的原文件（如 `today.md` 与 `today 2.md`）时才算冲突，命中报 `WARN` 并列出。
7. `state-dir`：状态目录存在且可写（异常为 `FAIL`）。
8. `vault-git`：检查 vault 是否为 Git 仓库（不是则报 `WARN`）。
9. `local-only`：`[privacy] local_only` 是黑名单规则，所列路径尚未创建也算 `OK`（消息中注明「尚未创建」）；若路径写成绝对路径或包含 `..` 则报 `FAIL`。
10. `ledger-recent`：账本首行距今（按 `clock`）已满 3 天，且最近 3 天没有 `job == "digest"`、`result ∈ {ok, degraded}` 的行时报 `WARN`（定时任务可能未生效）；账本为空或首行不满 3 天报 `OK`。
11. `inbox-backlog`：存在文件日期早于今天减 2 天（按 `clock`）、且不在游标 `digested.jsonl` 中的合法收件行时报 `WARN` 并列出条数。

`validate` 执行其中不依赖实例配置的部分：`vault-structure`、`inbox-names`、`inbox-lines`、`open-items`、`candidates`、`decisions`、`lessons`、`sync-conflicts`。

## 7. M3 新增规范

### 7.1 候选记忆（`hub/reports/candidates.md`）

```text
- [ ] C-260305-1｜inbox｜交稿前习惯先朗读一遍对话｜added=2026-03-05｜src=L-…
- [x] C-260305-2｜convention｜……｜added=2026-03-05｜to=profile｜closed=2026-03-06｜src=…
- [-] C-260306-1｜insight｜……｜added=2026-03-06｜closed=2026-03-07｜src=…
```

`[ ]` 待批、`[x]` 已批准（`to=profile` 或 `to=rules`）、`[-]` 已丢弃。编号 `C-YYMMDD-N` 不复用。旧格式行（`- 日期｜kind｜文本｜src=…`）读入时自动转成待批并补编号。待批数达到 `[limits] candidates_pending`（默认 20）后，模型给出的新候选不再加入，使用者自己写的照常加入。

批准把候选**原文**追加到 `hub/profile.md` 的 `## 已批准的记忆` 或 `hub/AGENTS.local.md` 的 `## 已批准的补充规则` 小节：`- <文本>（<日期> 批准，C-…）`。只有 `[approval] doors`（缺省：管家端加 `web`）里的端写的批准、丢弃行才生效。

### 7.2 机械动作补充

`批准 C-… 画像|规则`、`丢弃 C-…`；英文 `approve C-… profile|rules`、`discard C-…`。

### 7.3 开放项的推荐字段

开放项行新增可选字段 `rec=…`（位于 `src=` 之前；全角竖线和换行会被替换）。判断步每条 `items` 可带 `recommendation`（字符串或 null），只对 `owner=user_decision` 的项保留；新的非空推荐覆盖旧的。`hushai todo accept` 写出 `完成 <编号>：按推荐，<rec>`。

### 7.4 周报「已办未销」行格式

```text
- <编号>｜<理由>｜src=<line_id,…>
```

晨间简报的「疑似已办」取最近一份周报（距今不超过 2 天）里仍然开放的项，再加上已推迟 3 次及以上的开放项，最多 3 条。

### 7.5 M3 配置节（全部可省略）

```ini
[gate]
exchange_ext = md,txt,json,csv,html,pdf,png,jpg
exchange_max_kb = 5120

[routing]
enabled = false
trusted_doors =
max_daily = 10
steward = claude:heavy, agy:heavy
agy =

[approval]
doors = steward,web

[limits]
candidates_pending = 20

[sync]
tool = rclone
remote =
timeout = 60
```

`[routing]` 里除 `enabled`、`trusted_doors`、`max_daily` 外的键都是目标名，值是回退链；值为空表示该目标不接受路由。配置值后面不要写行内注释。`[sync]` 配置单向同步命令（默认 `rclone`）、远端根路径与超时秒数。

### 7.6 状态目录新增文件

| 文件 | 内容 |
| --- | --- |
| `routed.jsonl` | 每次路由一行：`line_id`、`target`、`result`（ok / failed）、`tries`、`ts` |
| `gate/<端>.seen.json` | 该端已确认收到的交换区文件 |
| `serve.log` | 待办页面访问日志（方法、路径、状态码；不含令牌） |
| `<配置目录>/serve.token` | 待办页面令牌，权限 600 |

### 7.7 待办页面接口

只监听 `127.0.0.1`。`GET /?t=<令牌>` 校验后种 `HttpOnly; SameSite=Strict` 的 cookie；没有有效令牌或 cookie 的任何请求返回 403 且响应体为空。`GET /` 页面，`GET /api/brief` 简报 JSON，`POST /api/act`（须带请求头 `X-Hushai: 1`，请求体不超过 4 KB）：

```json
{"action": "done|defer|drop|accept|approve|discard|accept_all", "id": "260302-1 或 C-…", "to": "profile|rules"}
```

成功返回新的简报 JSON（附 `_html`，页面用它局部重画）；参数不合法 400，目标不存在或已处理 409，其他路径 404，其他方法 405。

### 7.8 决定（`hub/decisions.md`）与教训（`hub/lessons.md`）

- **`hub/decisions.md`**：只追加，每条占一行，以全角竖线 `｜` 分为 5 段：
  ```text
  - YYYY-MM-DD｜<决定>｜<理由>｜<批准人>｜<来源收件行编号>
  ```
- **`hub/lessons.md`**：只追加，每条占一行，以全角竖线 `｜` 分为 4 段：
  ```text
  - YYYY-MM-DD｜<现象>｜<根因>｜<防线>
  ```
- **机械对账**：识别收件行「做了什么」开头的 `决定：…`（或 `decision: ...`）与 `教训：…`（或 `lesson: ...`），按收件行编号（`line_id`）幂等去重追加到对应文件，并在 `<状态目录>/cortex-ids.json` 记录已处理编号。`validate` 与 `doctor` 均校验这两个文件的存在与行格式。

### 7.9 静默自愈健康检查（`hushai heal`）与会话自动记录（`hushai capture`）

- **`hushai heal`**：
  - 检查项注册表（`heal.CHECKS`）：`vault_rw`（10 秒子进程探针写读删 `.heal-probe`）、`state_and_disk`（状态目录可写且剩余空间 ≥ 500 MB）、`timers_on_time`（`digest` > 26h / `weekly` > 8d 漏跑补跑与 `failed` 单元复位重启）、`watch_alive`（`hushai-<实例>-watch.path` 非 `active` 时复位启动）、`memory_write`（超 2 天未对账收件补跑 `reconcile`，钩子缺失重装 `doorkit`）、`model_available`（不调模型，统计近 24h 账本，回退链首家连续 3 次失败则写 `<状态目录>/adapter-skip.json` 让调用器 6 小时内跳过首家直走下一家，到期自动恢复）、`sync_ok`（配了 `[sync] remote` 时执行 `rclone lsd <remote> --timeout 15s`，失败重试 1 次）。
  - 白名单护栏：只允许操作 `hushai-<实例>-*` 与 `rclone-*.service` / `mnt-onedrive-*.mount`，白名单外的单元名一律拒绝操作并上报。
  - 静默与上报：全正常只写账本 `job=heal, result=ok`；已自愈写 `alerts.jsonl` 一条 info；修 2 次仍失败或不在白名单写一条 `needs_human`（`<状态目录>/heal-alerts.json` 24 小时去重），账本记 `result=needs_human`；该项检查恢复正常时自动撤下并清冷却。`steward_online` 不再做额度自动切换（2026-11-17 移除）。
- **`hushai capture`**：
  - 从标准输入读取钩子 JSON（`session_id`、`transcript_path`），配了 `[jobs] capture`（如 `claude:light`）时，把最后一条人类消息全文（≤4000 字）与其后 AI 回复（≤2000 字）交给模型，按 `prompts/capture.zh.md` 判断：维护 hushAi 本身、寒暄不记（只记账本），真实事务记成一句 ≤60 字并带待办；单独每日预算 `[capture] max_daily_calls`（默认 40，不占主预算），模型锁只等 5 秒；失败退回机械取前 120 字。写入 `hub/inbox/<日期>-<door>-auto.md`。
  - 同一 `session_id` 10 分钟内最多写一行（状态存于 `<状态目录>/capture-state.json`）。

### 7.10 目标（预留，未启用）

- **文件**：`hub/goals.md`（预留，未启用，当前不创建文件、不编写代码）。
- **格式**：每个目标一节，四栏 `目标｜现状｜理想状态｜服务它的项目`，上限 3 个。
