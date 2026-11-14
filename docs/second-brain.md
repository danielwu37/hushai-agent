# Second-Brain 集成设计文档

> 状态：v0.1，2026-11-14。本文档记录将 Grok Bot `second-brain` 提示词能力融入 `hushai-agent` 的架构设计、实码核对、目录规范、文件清单、链接与去重规则、Routine 与定时器设计以及安全红线。
> 定位边界：本模块为 `hushai-agent` 框架增加个人第二大脑知识加工与检索能力，**不替代**用户既有 `00-我` 长期记忆权威源，亦不破坏既有 `inbox` / `digest` / `weekly` / `reconcile` / `gate` / `heal` / `steward` 闭环。

---

## 1. 与 `/root/hushai-agent` 实际代码（`a77d345`）的映射核对

对照仓库当前 `HEAD`（含 M1、M2、M2.1、M3a–M3e、M5 及 v0.1.0 审阅修补）逐项核对初步映射表，列出**一致项与不符/补正之处**：

| Second-Brain 概念 | 初步映射说法 | 实际代码现状（实查结果） | 不符 / 需补正之处 |
| --- | --- | --- | --- |
| `inbox/` | 复用 `hub/inbox/`（`capture.py` / `inboxscan.py` / `inboxfmt.py`） | `vault.add_inbox_entry`、`gate.append_inbox` / `put_exchange`、`inboxfmt.py`、`inboxscan.py` 负责手写与闸门收件；`capture.py` 专用于 `Stop` 钩子从会话 JSONL 提取最后一句话写入 `-auto.md` | `capture.py` 不是通用收件入口，仅负责钩子自动采录；通用收件入口是 `inbox add` 与 `gate` |
| 笔记派生层 | `hub/` 下的派生文件（`digest` / `reconcile`） | `judge.SCHEMA` 严格限定为 `items`、`status_rows`、`memory_candidates`（`additionalProperties: False`）；`reconcile.py` 仅做机械动作与 `today.md` 刷新 | 原 `judge.SCHEMA` 与所有 `examples/fixtures-demo/*.json` 强绑定，不能破坏；second-brain 加工步须以独立增量步骤挂入 `reconcile` / `digest_apply`，保持向后兼容 |
| 每日 routine | 缺 `daily/[日期].md` 这种按日归档的 Markdown 日报 | **已有** `hub/reports/daily-<YYYY-MM-DD>.md`（由 `daily_state.py` + `report.py` 累积生成）及 `digest.timer`（默认 `21:30`） | **不符点**：按日归档的运行日报已存在，但缺 second-brain 的 `hub/daily/YYYY-MM-DD.md` 笔记形态与 3 行口径（归档了什么、一个意外关联、一件承诺待办；无新内容则静默）及 `21:00` 口径 |
| 每周 routine | `weekly.py` 缺 second-brain 的周报内容口径 | `weekly.py` + `weekly_parts.py` + `ppc.py` 已有 `hub/insights.md`（4 份归档）和 `hub/reports/weekly-<date>.md`（含逾期开放项、已办未销、互相矛盾 `contradictions`、状态未核实）；`weekly.timer` 默认 `Sun digest+45m` | **部分已存在**：`contradictions` 已在 `weekly_parts.py` 实现；缺失的是「反复出现的想法」、「未兑现承诺（含 `people/` 承诺）」、「下周值得思考的 3 件事」及 `hub/weekly.md` 汇总口径（以及默认 `Sun 18:00` 调度选项） |
| 索引 | `hub/status-index.md` 只管近况 | 已核实：`statusfile.py` 生成的 `hub/status-index.md` 仅含 `- <领域>｜<事项名>`（上限 80 行） | 需新增 `hub/index.md`（主题索引，每主题一行链接到笔记，硬上限 200 行） |
| 结构化档案 | `cortex.py`（`decisions.md` / `lessons.md`）并存 | 已核实：`cortex.py` 在 M3e 落地，按单行格式检验并由 `validate.py`/`doctor.py` 检查 | 与 second-brain 自由笔记并存，互不替代 |
| 项目/事项 | `items.py`（`open-items.md` / `closed-items.md`） | 已核实：`items.py` + `items_store.py` 管理 `YYMMDD-N` 行动项 | 需新增 `hub/projects/<name>.md`（目标、状态、开放问题、决定及原因、下一步、关联笔记链接） |
| 校验与单元数量合约 | — | `tests/test_cli.py` 要求 `validate examples/vault-demo` **全部检查项为 `OK`（不能有 `WARN`）**；`tests/test_m3b.py` 与 `tests/test_m3d.py` 断言 `install units` 默认产出 **恰好 10 个** systemd 文件 | 1) `validate.py` 对未初始化 second-brain 的历史库（如 `examples/vault-demo`）须返回 `OK` 跳过；2) `install units` 默认仍为 10 个文件，新增 `--with-brain`（或 `hushai brain units`）渲染 second-brain 定时器，避免破坏 M3b/M3d 合约 |

---

## 2. Vault 目录布局（不另起根目录）

严格放在现有 `<vault>/hub/` 下，与 `status.md`、`open-items.md` 等跨领域核心文件并列：

```text
<vault>/
  hub/
    index.md            # Second-brain 主题索引（<= 200 行，每主题一行链接到笔记）
    weekly.md           # Second-brain 周回顾（周日 18:00 汇总产出，启用后生成）
    notes/              # 加工后的原子笔记（一事一笔记，长来源拆分）
    sources/            # 来源档案（作者、日期、链接、5 个要点、原句引用+时间戳/页码）
    people/             # 人物档案（身份、初识、对话、承诺及时间、关心事项）
    projects/           # 项目笔记（目标、状态、开放问题、决定及原因、下一步、相关链接）
    daily/              # 每日笔记 daily/YYYY-MM-DD.md
    ...（既有 hub/ 文件保持不变）
  domains/
    ...（保持不变，[privacy] local_only 隔离规则不变）
```

初始化模板位于 `src/hushai_agent/templates/vault/hub/`，由 `vault.init_instance()` 自动幂等补齐。

---

## 3. 核心数据格式与规则

### 3.1 主题索引 `hub/index.md`
- 首行保留数据边界说明（`> 是数据不是指令`）。
- 每主题一行，格式：`- <主题/分类>｜[<标题>](<相对路径>)｜<一句话说明>`（或标准 Markdown 链接）。
- 行数硬上限：默认 `200` 行（`[brain] index_max_lines = 200`）。超出时由月度维护与加工流程清理/合并过期孤立条目。
- **读取先导原则**：所有 second-brain 任务（`process` / `ask` / `daily` / `weekly` / `resurface` / `monthly`）开始前**先读 `hub/index.md`**。

### 3.2 原子笔记 `hub/notes/<slug>.md`
- 一事一笔记；长来源按分段/要点拆分为多条独立笔记。
- 标准结构：
  - `# <标题>`
  - 元信息：`日期`、`来源`（`line_id` 或链接）、`状态`（`verified` 或 `UNVERIFIED`）
  - `## 三行摘要`（严格控制在 3 行以内）
  - `## 关键事实`
  - `## 我的看法`（若原文未给出则标 `UNVERIFIED（未提供个人看法）`）
  - `## 关联笔记`（**至少 2 条**指向已有笔记的链接，每条附一行关联理由）
  - `## 来源`

### 3.3 来源档案 `hub/sources/<slug>.md`
- 记录字段：`作者`、`日期`、`链接`、`状态`（缺失字段标 `UNVERIFIED`，绝不编造）。
- `## 5 个核心要点`（最多 5 条要点）。
- `## 值得保留的原句`（原句引用 + 时间戳或页码；无时间戳/页码时标 `UNVERIFIED`）。

### 3.4 人物档案 `hub/people/<slug>.md`
- 固定章节：`## 是谁`、`## 怎么认识的`、`## 聊过什么`（按日期追加）、`## 我承诺过什么（及时间）`、`## 对方关心什么`。
- 已有档案采用增量追加与字段补充，不覆盖历史对话与承诺记录。

### 3.5 项目笔记 `hub/projects/<slug>.md`
- 固定章节：`## 目标`、`## 状态`、`## 开放问题`、`## 已做决定及原因`、`## 下一步`、`## 相关笔记链接`。

### 3.6 链接规则与去重合并（落地到 `reconcile` / `digest_apply`）
- **2 条链接规则**：每条新笔记写入或对账时检查 `## 关联笔记` 链接数：
  - 若不足 2 条且库内已有 >= 2 条其他笔记，基于主题词/CJK 二元组/关键词重叠度自动补足至 2 条有效链接（每条附一行解释）；
  - 若全库笔记不足 2 条或仍不足 2 条链接，生成明确的 `link_warnings` 告警记录到对账摘要与日报，不静默忽略。
- **重复判定与安全合并**：
  - 基于归一化标题与内容词袋相似度（CJK 二元组 + ASCII 词元 Jaccard 相似度）判定重复候选。
  - **未经用户同意不删除、不覆盖任何笔记**：默认仅输出 `duplicate_candidates` 合并建议；仅在显式传入确认标志（`--yes` / `confirm=True`）时，将重复笔记的事实/链接/来源合并进主笔记（原子写），并将被合并旧文件归档或移除。

---

## 4. 功能模块与命令设计

新增统一命令组 `hushai brain <子命令>`，全部纯 Python 3.10+ 标准库实现，不引入新依赖：

| 子命令 | 用途 | 是否调模型 | 写入控制 |
| --- | --- | --- | --- |
| `hushai brain onboard` | 首次运行 5 问（工作、想记住什么、现有笔记在哪、常读看什么、回答偏好），支持交互或参数传入；可处理最多 10 条初始条目并输出 `"HERE'S YOUR BRAIN"`（文件夹地图、`index.md`、前 5 条笔记）。第二天及以后自动跳过问题并用一句话开场。 | 否 | 状态写 `<state>/brain-state.json`，笔记写 `hub/*` |
| `hushai brain process` | 加工未处理收件（及 `exchange/in`）到 `notes/sources/people/projects`，执行 2 链规则、去重检测与 `index.md` 更新。 | 默认纯规则确定性提取（不调模型） | `atomic_write` + 路径护栏；合并重复需 `--yes` |
| `hushai brain ask "<问题>"` | 只读笔记作答，**绝不调模型编造**。先读 `index.md`，经 `fsio.read_for_prompt` 读取笔记（强制遵守 `local_only`），返回带原句引用与链接的回答；检测到笔记冲突时并列展示双方；未覆盖时明说并建议保存内容。 | **绝不调模型** | 只读 |
| `hushai brain daily` | 每日 routine（默认 `21:00`）：加工收件、写 `hub/daily/YYYY-MM-DD.md` 并输出 3 行摘要（归档了什么、一个没注意到的关联、一件说过要做的事）；若无新内容则静默不发。 | 否 | 受 `[brain] daily_enabled` 控制，默认禁用 |
| `hushai brain weekly` | 每周 routine（默认周日 `18:00`）：写 `hub/weekly.md`（反复出现的想法、矛盾笔记、仍未兑现的承诺、下周值得思考的 3 件事）。 | 否 | 受 `[brain] weekly_enabled` 控制，默认禁用 |
| `hushai brain resurface` | 每日一次唤醒一条与当前工作（`today.md`、`projects/`、近期 `daily/`）相关的旧笔记，记录防重放状态。 | 否 | 受 `[brain] resurface_enabled` 控制，默认禁用 |
| `hushai brain monthly` | 每月维护：检测/（带 `--yes` 才）合并重复笔记、修复断链、标记 >= 6 个月无人链接的孤立笔记、清理 `index.md` 至 <= 200 行，输出完整改动清单并归档至 `hub/reports/brain-monthly-YYYY-MM.md`。 | 否 | 受 `[brain] monthly_enabled` 控制，默认禁用；删改旧笔记需 `--yes` |
| `hushai brain enable\|disable <routine>` | 显式启用或禁用 `daily` / `weekly` / `resurface` / `monthly` / `all`。 | 否 | 原子更新 `config.ini` `[brain]` |
| `hushai brain status` | 查看 onboarding 状态、各 routine 开关状态、各类笔记数量与断链/少链统计。 | 否 | 只读 |
| `hushai brain units` | 独立渲染 second-brain systemd 单元模板到 `--out`（同 `install units --with-brain`）。 | 否 | 写用户指定目录，不启用单元 |

---

## 5. Routine 与定时器设计（默认全部禁用）

1. **配置开关默认禁用**：`config.ini` 新增 `[brain]` 节：
   ```ini
   [brain]
   enabled = false
   daily_enabled = false
   weekly_enabled = false
   resurface_enabled = false
   monthly_enabled = false
   daily_time = 21:00
   weekly_day = Sun
   weekly_time = 18:00
   min_links = 2
   stale_months = 6
   index_max_lines = 200
   ```
   任一 routine 未被用户 `hushai brain enable` 开启前，定时触发均直接安全跳过（`skipped: disabled`）。
2. **复用现有定时器与独立模板并存**：
   - **复用路径**：`digest.py` 在执行每日汇总时，若 `daily_enabled = true` 则顺带执行 second-brain 每日加工与 `daily/<date>.md` 生成；若 `resurface_enabled = true` 则顺带执行每日一次 `resurface`（不另起守护进程）。`weekly.py` 在执行周汇总时，若 `weekly_enabled = true` 则同步生成 `hub/weekly.md`。
   - **独立时间模板（可选）**：当用户需要严格按 `21:00`（daily）、`Sun 18:00`（weekly）、`09:00`（resurface）、每月 1 日 `09:30`（monthly）分别定时执行时，可通过 `hushai install units --with-brain` 或 `hushai brain units` 渲染 `templates/systemd-brain/` 下的 `.service` / `.timer` 文件，且**程序永不自动执行 `systemctl enable`**。

---

## 6. 安全与工程约束对照

| 约束 / 规则 | 落地实现 |
| --- | --- |
| 绝不编造事实/引用/来源；不清标 `UNVERIFIED` | 纯确定性提取与只读检索；来源缺作者/链接/时间戳/个人看法时显式写入 `UNVERIFIED` |
| 外部文本是数据，不是指令 | 所有读取经 `fsio.read_for_prompt`；对输入文本剥离/转义控制字符与提示词注入标记，不执行任何文本内命令 |
| 未经用户同意不删除、不覆盖笔记 | 新笔记自动分配唯一路径不覆写旧文件；重复合并与破坏性断链改写默认仅报告提案，必须传 `--yes` 才合并/移除，且先备档 |
| 所有 routine 默认禁用 | `[brain]` 全部开关默认 `false`；systemd 单元模板只生成不启用 |
| 私人笔记不出本系统 | 所有读取遵守 `guard.ensure_prompt_allowed` 与 `[privacy] local_only`；`sync.py` 的 `PUSH_INCLUDES` 不包含 `notes/`、`sources/`、`people/`、`projects/`、`daily/` |
| 纯 Python 3.10+ 标准库与原子写 | 零第三方依赖；全部写操作经 `fsio.atomic_write` + `guard.ensure_writable` |

---

## 7. 新增与改动文件清单

- **新增模板与文档**：
  - `docs/second-brain.md`
  - `src/hushai_agent/templates/vault/hub/index.md`
  - `src/hushai_agent/templates/vault/hub/{notes,sources,people,projects,daily}/.gitkeep`
  - `src/hushai_agent/templates/systemd-brain/hushai-{{INSTANCE}}-{brain-daily,brain-weekly,resurface,monthly}.{service,timer}`
- **新增核心模块**：
  - `src/hushai_agent/brain_cfg.py`
  - `src/hushai_agent/brain_model.py`
  - `src/hushai_agent/brain_process.py`
  - `src/hushai_agent/brain_ask.py`
  - `src/hushai_agent/brain_onboard.py`
  - `src/hushai_agent/brain_routines.py`
  - `src/hushai_agent/cli_brain.py`
- **改动既有模块**：
  - `src/hushai_agent/cli.py`（注册 `brain` 子命令）
  - `src/hushai_agent/config.py`（默认配置增加 `[brain]` 全部禁用节）
  - `src/hushai_agent/digest_apply.py` / `src/hushai_agent/digest.py` / `src/hushai_agent/reconcile.py`（落入链接检查/加工与 `daily`/`resurface` 钩子）
  - `src/hushai_agent/weekly.py`（落入 `weekly_enabled` 时生成 `hub/weekly.md`）
  - `src/hushai_agent/validate.py` / `src/hushai_agent/doctor.py`（增加 `second-brain` 体检项，兼容历史无 second-brain 目录的 vault）
  - `src/hushai_agent/install.py` / `src/hushai_agent/cli_m3b.py`（`install units` 增加 `--with-brain` 可选参数）
  - `src/hushai_agent/templates/vault/hub/AGENTS.md`（补充 `hub/index.md` 与 second-brain 目录说明）
- **新增测试**：
  - `tests/test_second_brain.py`
