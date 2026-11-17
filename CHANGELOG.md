# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and release notes are aggregated automatically via [GitHub Releases](https://github.com/danielwu37/hushai-agent/releases).

## [Unreleased]

<!-- 后续新增改动先写在此处，下个版本发布时自动归档至版本章节 -->

### Changed
- **第二大脑重做为 LLM Wiki 式（2026-11-17，`docs/second-brain.md` v2）**：借鉴卡帕西 LLM Wiki（原料 → 模型维护的维基 → 规则；ingest / query / lint）、Obsidian（文件名即标题、`[[双链]]`、YAML 属性、日记页）与 LifeOS（类型化档案、重温、体检）。只收显式材料：`笔记：`/`来源：`/`人物：`/`项目：`/`入脑：` 前缀收件行（`[brain] since` 之后）、`hub/exchange/in/入脑/` 投放文件、各端交换区 `入脑-*` 文件；模型返回 JSON，由代码把内容融合进 `projects/`、`notes/`、`people/`、`sources/` 页（按标题/别名合并、来源只增不减、`## 我的笔记` 与 `locked: true` 页不改、「已核实」只能 `hushai brain verify` 标）；`index.md` 目录与 `daily/<日期>.md` 入脑日志由代码生成。新模块 `brain_page`、`brain_raw`、`brain_ingest`、`brain_write`、`brain_lint`，重写 `brain_cfg`、`brain_ask`、`cli_brain`；命令 `hushai brain enable|disable|ingest|ask|lint|status|add|verify|units`。
- **第二大脑离开对账与汇总路径**：`reconcile`、`digest`、`weekly`、检查点收尾不再含第二大脑步骤；编译由新定时器 `hushai-<实例>-brain.{service,timer}`（默认 22:30）或手动触发，模型调用不持 `runner.lock`，落盘时短暂持锁并在释锁后排空对账积压。
- **单独预算**：`[brain] max_daily_calls`（默认 12）只数 `brain` 调用；主预算改为不数 `capture` 与 `brain`（`ledger.SEPARATE_BUDGET_JOBS`）。
- **晨间简报**新增「第二大脑」一节（昨天以来新入脑的页 + 一页旧页重温，隔离领域不出现）；`doctor` 的 `second-brain` 一项改为 `brain lint` 结论；`hushai config` 菜单 6 改为开关、参数、立即编译、检索、体检。

### Removed
- **旧版第二大脑（v1）**：`brain_collect`、`brain_daily`、`brain_docs`、`brain_index`、`brain_items`、`brain_links`、`brain_maint`、`brain_merge`、`brain_model`、`brain_onboard`、`brain_process`、`brain_routines`、`checkpoint_verify_brain` 与 8 个旧单元模板（`brain-daily`、`brain-weekly`、`resurface`、`monthly`）；`[brain]` 旧键（`daily_enabled` 等）读取时忽略。`hushai brain disable` 会清配置目录里的新旧单元并列出系统目录残留。
- **额度用尽自动切 agy（2026-11-17）**：`steward_online` 不再识别额度字样、不再自动切换和重启管家；需要时手动 `hushai steward use agy|claude`。

### Added
- **`hushai alert`**（`cli_alert.py`）：`add`/`resolve`/`list`/`active`；告警可撤下（`resolved` 行），heal 某项检查恢复即撤下其需人工告警并清 24h 冷却；晨间简报与 `doctor` 只看未恢复的 warning 及以上；管家会话开场钩子 `alert active` 主动报告。周边任务（hushai-ext）改用它，不再写管家收件。
- **capture 模型判断**（`capture_model.py`、`prompts/capture.zh.md`）：配 `[jobs] capture = claude:light` 后由轻档模型读最后一轮对话全文决定记不记、记成一句话；单独预算 `[capture] max_daily_calls`（默认 40），主预算不再计 capture 调用；模型锁只等 5 秒，失败退回机械采集（前 120 字）。
- **北极星统计**（`northstar.py`）：`hushai ledger --stats`，周报末尾附「北极星」一节（需人介入、待办有用率、系统自嗨率）。
- **系统告警独立账本 (`alerts.jsonl`)**：[`alerts.py`](src/hushai_agent/alerts.py) 将 `heal` 自愈/人工介入提示与管家切换告警统一写入 `<state>/alerts.jsonl`，不再污染 `hub/inbox/` 与 `hub/today.md`；[`brief.py`](src/hushai_agent/brief.py) 与 [`brief_html.py`](src/hushai_agent/brief_html.py) 在简报中展示「系统通知」，[`doctor.py`](src/hushai_agent/doctor.py) 检查 `system-alerts`（近 24 小时严重告警）与 `inbox-expired`（超出 `window_days` 的晚到未消化收件）。
- **`[privacy] local_only_domains`**：默认隔离 `health,健康`，覆盖 `collect`、`judge`、`weekly_parts`、`routing`、`brain_collect`、`brain_process` 与 `brain_routines`，机械对账先局部处理再剥离敏感域，绝不进入外部模型提示词；并在 `hushai config` 菜单 `1 -> 7` 开放配置。
- **`--with-model` 与 `[sync] pull_doors` / `server_doors`、`[capture] default_domain`**：`digest`/`weekly` 的 `--dry-run` 默认仅在全 `fake` 链路调模型，非 `fake` 适配需显式传 `--with-model` 才调真实模型；新增 `hushai digest --for-date`。
- **`doorkit` 权限与高危操作拦截**：`claude-settings.json` 与 `agy-settings.json` 同步写入细粒度 `permissions.deny` 与 `permissions.ask` 规则。
- `sync`：配了 `[sync] remote` 时渲染每 15 分钟的同步定时器（`templates/systemd-sync/`）。
- `[gate] private_paths_scope`：`private_paths_file` 的规则只作用于列出的读根（相对该根匹配）；不配时保持原行为（对所有路径生效）。`local_only` 始终生效。

### Changed
- **定时调度与事件防雪崩**：[`install.py`](src/hushai_agent/install.py) 模板将 `heal.timer` 错峰至 `*:50:00`、`sync.timer` 改为 `*:00,15,45:00` 并加 `RandomizedDelaySec=30` 与 `TimeoutStartSec=15min`、全部 `.service` 显式注入 `Environment=TZ={{TZ}}`；`sync.py` 与 `on_event.py` 增加 `budget_seconds` 预算与 `sync pull --update` / 服务端写入端排除；`lock.py` 新增 `model.lock` 串行化 `runner` 与 `dispatch` 的外部模型调用。
- **Muse 闸门通用化与安全写盘**：将个人历史映射（`legacy_prefix_map` / `legacy_fallback`）、`txt_only_dirs` 与 `deny_names` 解耦至 `[gate]` 配置；`put` 禁止回落 `legacy` 目录且改用 `-1`/`-2` 防覆盖分配；`append-inbox` 触发即时机械对账并用日志/账本记录异常。
- **常驻管家与会话采集**：`steward.py` 与 `steward.service` 改用 `tmux -L hushai-<inst>` 独立 socket 隔离；`capture.py` 对 ASCII 领域启用词边界匹配，未命中回退至中性领域（`[capture] default_domain` / `生活`），并过滤系统通知噪音与记录状态读取异常。
- **第二大脑增量性能与安全加固**：`brain_onboard` 过滤纯数字误输入（如 `"0"`）并按最多 10 条冷启动分批；`PERSON_AT_RE` 排除邮箱地址；笔记状态统一标为 `有出处`；`merge_duplicate_notes` 先改写全库入链再删除归档重复稿；`reconcile_brain_step` 跳过 O(n²) 全库去重扫描。
- **大模块拆分与行数约束**：将超长模块拆分为职责单一的小文件，全部 `src/hushai_agent/*.py` 模块均控制在 300 行以内。
- `weekly`：重写 `insights.md` 时保留全部 `insights-archive/insights-*.md` 历史归档；上一版未覆盖条目继续保留在「往期观察」一节。
- 今日待办排序：今天起 2 天内到期的排在已逾期之前。
- 闸门 `append-inbox` 拒收时给出具体原因（开头、段数、「产物：」「待办：」）；越界拒绝提示列出实际配置的读根。
- `steward.service` 模板改为 `Type=simple`：`ExecStartPre` 起 tmux 会话，主进程守着会话，会话没了由 `Restart=always` 拉起（`Type=forking` 在 tmux 3.2 上会卡在 activating）。

### Fixed
- **复审补漏（2026-11-16）**：`digest` 默认日期改为「到 `digest_time` 才把今日待办翻到明天」，凌晨或白天手动跑都写当天；`weekly --scheduled`（定时器用）基准日取预定周日，晚到的补跑若本期已出则跳过；Claude 端 `Write`/`Edit` 拦截规则改用 `//绝对路径/**` 写法；隔离领域（`local_only_domains`）里带待办的行由 [`local_items.py`](src/hushai_agent/local_items.py) 直接落成开放项，不再被静默消化；`capture` 对管家维护类对话只记账本（`[capture] system_markers` 可改）；`brain disable all` 清理配置目录里的 second-brain 单元并提示系统目录残留。
- **修复跨午夜补跑与跨周误重跑 (`digest` / `weekly` / `heal`)**：凌晨 6 点前昨日未出 `digest` 自动回指昨日（目标为今日）；`heal_checks` 按 `digest_time` 计算补跑日，同 ISO 周内已跑过周报不再在次周初误触发补跑；修复 `hushai config` 菜单 8 手动跑 `digest`/`weekly` 缺少 `as_of` 参数报错。
- **修复同步冲突文件名误判**：收紧 `validate_basic.SYNC_CONFLICT_RE`（兼容半全角 `(冲突)`/`（冲突）` 与 `_冲突版`），避免 `记忆冲突裁决规则.md` 等正常标题被误报 `WARN`。
- `steward`：claudeMdExcludes 排除宿主机用户级 CLAUDE.md（remote-control 不支持 `--setting-sources`）。
- `steward`：capture-pane 目标加冒号，适配 tmux 3.2。

## [0.2.1] - 2026-11-15

### Added
- **P0 / O1 事件流并发防丢与目标文件校验型崩溃恢复**：
  - 新增 [`checkpoint.py`](src/hushai_agent/checkpoint.py) 与 [`checkpoint_verify.py`](src/hushai_agent/checkpoint_verify.py)（检查点落于 `<state>/checkpoints/`），恢复阶段直接对 Vault 与 State 目标文件做内容摘要、行编号与提交态核验（绝不轻信 JSON `stage` / `brain_done` 字段）：覆盖 `open-items.md`、`closed-items.md`、`today.md`、`status.md`、`status-index.md`、`corrections.md`、`status-archive.md`、`candidates.md`、`profile.md`、`AGENTS.local.md`、`decisions.md`、`lessons.md`、`morning-brief.md` 与 `.html` 渲染一致性、`routed.jsonl` 与管家回执、`daily/<date>.json`、`notes/`、`sources/`、`people/`、`projects/`、`daily/<date>.md` 与 `index.md` 链接；损坏或结构非法检查点自动隔离至 `checkpoints/corrupt-*`；`heal.run_heal` 同步接入非阻塞遗留检查点接管。
  - [`lock.py`](src/hushai_agent/lock.py) 新增 `acquire_or_mark_dirty`（落盘 dirty 标记与二次抢锁重检）及 `drain_after_unlock`，消除 `on-event` / `reconcile` / `routing` 在 `lock-busy` 下的事件漏触发。
  - `brain_routines`（`daily` / `weekly` / `resurface` / `monthly`）与 `brain_process` 统一受 `runner.lock` 互斥保护并在释锁后排空机械积压；[`brain_model.py`](src/hushai_agent/brain_model.py) 与 [`brain_process.py`](src/hushai_agent/brain_process.py) 扩展 `sources`/`people`/`projects`/`notes` 全目录 `source_ref` 写入与按目标拆分幂等复用。新增多阶段崩溃、真实子进程 `SIGKILL` 与多进程锁竞争测试 [`test_p0_o1_resilience.py`](tests/test_p0_o1_resilience.py)。
- **P0 / O2 `<state>/outbox/` 纯文件交付状态机（at-least-once）**：
  - 新增 [`outbox.py`](src/hushai_agent/outbox.py)、[`outbox_store.py`](src/hushai_agent/outbox_store.py) 与 [`cli_outbox.py`](src/hushai_agent/cli_outbox.py)（注册 `hushai outbox <status|list|flush|recover|retry|archive>`），实现 `<state>/outbox/{pending,sending,sent,failed,archive}/` 分态迁移（以单调递增 `transition_seq` 防止同时间戳或 `SIGKILL` 写删间隙崩溃后的重复状态覆盖）、独占锁下 `sending` 中断回收、指数退避重试、幂等键去重（支持 `dedupe.json` 丢失或损坏时连同 `archive/` 全量重建与诊断健康度上报、且空实例静默）、DST/跨时区 UTC instant 比较及历史归档，显式声明 **at-least-once（至少一次）** 投递语义（不宣称外部消息恰好发送一次）。
  - 集成至 [`checkpoint_verify.py`](src/hushai_agent/checkpoint_verify.py)（`exchange_out` 交付登记）、[`gate.py`](src/hushai_agent/gate.py)（`check-inbox --ack` 标定 `acked_at`）、[`on_event.py`](src/hushai_agent/on_event.py) 与 [`heal.py`](src/hushai_agent/heal.py)（无待投递/待恢复信封保持完全静默，并在崩后或索引损坏时保留 `today.md` 24 小时告警冷却去重）。新增测试 [`test_p0_o2_outbox.py`](tests/test_p0_o2_outbox.py)（8 项生命周期、同时间戳双份对账、真实 `SIGKILL`、`archive` 兜底重建及跨时区/DST/时钟回拨与 `heal` 崩后去重验证）。
- **自动化发布工作流与版本校验工具**：
  - 新增纯标准库工具 [`tools/release_tool.py`](tools/release_tool.py)（支持 `bump` / `verify` / `notes` / `publish`）：一键同步 `pyproject.toml`、`__init__.py`、`README.md` 与 `CHANGELOG.md` 版本号、提取版本说明与本次提交清单，并在全量测试通过后自动打标推送、创建或更新 GitHub Release；配套单测 [`tests/test_release_tool.py`](tests/test_release_tool.py)。
  - 新增 GitHub Actions 工作流模板 [`tools/workflows/ci.yml`](tools/workflows/ci.yml) 与 [`tools/workflows/release.yml`](tools/workflows/release.yml)（为避免 OAuth 凭据缺少 `workflow` scope 时阻塞推送，工作流模板存放在 `tools/workflows/`，待授予 `workflow` 权限后复制至 `.github/workflows/` 即可启用云端标签自动发布），并在 [`docs/admin-guide.md`](docs/admin-guide.md) 第 11 节补充完整操作与版本查看指引。

### Fixed
- **修复 `[brain]` 时间与星期参数未进入 systemd 单元**：`install.render_brain_units`、`install.render_units(..., with_brain=True)` 与 `hushai brain units` 改为默认读取 `config.ini` 中的 `daily_time` / `weekly_day` / `weekly_time` / `resurface_time` / `monthly_time`（`BrainConfig` 新增 `monthly_time` 并校验 `HH:MM` 与 `Mon..Sun`）；`brain-weekly.timer` 与 `monthly.timer` 模板改用变量替换，不再写死 `Sun` 与 `09:30`。
- **修复既有 `digest.timer` 自定义时间被重生成单元覆盖**：新增 `install.detect_existing_digest_time`，`hushai install units` 与 `hushai config` 5→4 在未显式传 `--digest-time` 时默认沿用既有 `.timer` 触发时间。
- **修复 `hushai serve` 启动后缓存配置不生效**：每次鉴权请求实时重载 `config.ini`（`Handler._live_cfg`），修改 `known_doors`、`today_items` 等配置无需重启即可生效，被移除的端即时返回 `403`。
- **修复 `run_daily_brain` 同日多次覆盖日报明细**：同一天多次执行 `daily` 时改为去重累计合并当日 `## 今日归档明细`，不再覆盖早先归档记录。
- **修复 `hushai sync` 漏传第二大脑目录**：`PUSH_INCLUDES` 补入 `/index.md`、`/weekly.md`、`/notes/**`、`/sources/**`、`/people/**`、`/projects/**`、`/daily/**`，同时保留 `[privacy] local_only` 排除规则且不触碰 `domains/`。
- **修复配置校验、大小写与回滚漏洞**：`update_ini_text` 支持大小写不敏感键匹配；新增 `validate_cross_constraints` 拦截非法 `vault`、`local_only` 越界路径、孤立适配器引用、`approval`/`trusted` 越界端及与既有收件冲突的领域修改并自动回滚；禁止 `sync.timeout` / `exchange_max_kb` / `candidates_pending` 填 `0`；`set_brain_routine_enabled` 与 `steward.set_backend` 统一经 `config_edit` 原子写入。
- **修复第二大脑自动补链与 Ask 检索质量问题**：新增 `extract_semantic_body` 剔除头部元数据与 `## 关联笔记` 段落并过滤状态模板词/日期/`L-...` 编号/疑问虚词；`brain ask` 仅在正文选引用并提高数量提问数值加权；`_extract_title` 规整删除 URL 后的多余空白。

### Changed
- **独立一键卸载脚本与交互式初始化/卸载增强（含 `vault` 默认永久保留保护）**：
  - 新增可执行脚本 [`bin/hushai-uninstall`](bin/hushai-uninstall)（无参进入交互向导、亦支持 `--instance`/`--all`/`--yes`/`--delete-vault`/`--remove-bin-link`/`--remove-app`），[`lifecycle.py`](src/hushai_agent/lifecycle.py) 与 [`cli_m3d.py`](src/hushai_agent/cli_m3d.py) 强制默认 **`keep_vault=True`** 永久保留用户 `vault` 记忆库（不带 `--delete-vault` 仅清程序配置、运行状态与 systemd 单元；交互卸载若选删除 vault 须额外输入 `DELETE-VAULT` 二次确认；清理部署目录时亦硬性保护开发仓库避免误删）。
  - [`cli_config.py`](src/hushai_agent/cli_config.py) 菜单 `8. 手动触发整理与系统维护` 新增第 8 项一键卸载/重置入口，卸载当前实例后安全切换或退出；`hushai install wizard`（[`cli_m3b.py`](src/hushai_agent/cli_m3b.py)）与 `_wizard_create_interactive` 支持省略或回车采用默认实例名 `my` 并交互提示记忆库路径；[`README.md`](README.md)、[`docs/admin-guide.md`](docs/admin-guide.md) 与 [`docs/design.md`](docs/design.md) 部署路径切换为 `/opt/hushai-agent`。
- **`hushai config` 菜单反馈与单元/Vault 联动**：每次保存明确提示 `[生效方式]`；修改时区、`vault`、`[brain]` 开关与时间后主动询问一键重生成 systemd 单元并给出 `systemctl` 命令；`vault` 未初始化时询问一键初始化；回退/路由链支持输入 `clear` 清空；适配器名统一小写规范化；补充 `monthly_time` 设置项与同步口径说明。
- **文档与测试补充**：对齐 `README.md` 与 `docs/second-brain.md` 中关于手工同步边界、`monthly` 定时器前提及 `人物：`/`项目：`/`@姓名` 触发说明；`tests/test_config_menu.py` 新增 `test_04`～`test_07` 行为传播与回归测试。

## [0.2.0] - 2026-11-14

### Added
- **交互式设置与控制中心（`hushai config`）**：
  - 提供 `1–9` 分级编号交互菜单，不用记命令行和参数即可统一修改实例配置（时区、语言、vault 路径、`local_only` 隐私目录、篇幅与窗口上限、领域、接入端、审批权、AI 适配器、调度回退链与预算、跨端 `@` 路由、`rclone` 同步、第二大脑开关与时间参数）。
  - 所有配置写入前自动执行全量校验（`load_instance_config` / `load_ext` / `load_brain_config`），校验不通过自动回滚原 `config.ini` 并保留原有注释。
  - 直接在菜单内集成日常与运维操作：切换/初始化多实例、查看今日待办与晨间简报、记收件、待办销账/推迟/放弃、审批候选记忆、切换与重启常驻管家、Ask 问答、执行 `reconcile` / `digest` / `weekly` / `heal` / `doctor` / `export` / `upgrade`。
- **个人第二大脑集成（`hushai brain`）**：
  - 新增 `<vault>/hub/index.md`（主题索引，每主题一行，硬上限 200 行）与 `hub/{notes,sources,people,projects,daily}/` 结构（集成至 `vault.init_instance` 幂等初始化，与既有 `00-我` 长期记忆正交并存，兼容未启用 second-brain 的历史记忆库）。
  - 增量知识加工（`hushai brain process` 及挂入 `reconcile` / `digest_apply`）：将收件与 `hub/exchange/in/` 内容加工为原子笔记（长来源自动拆分）、来源档案（含作者/日期/链接/5 要点/原句与时间戳，缺失项标 `UNVERIFIED`）、人物档案（含对话与承诺追踪）和项目笔记；外部文本一律仅作数据处理。
  - 链接规则与安全合并：检查并基于主题词/CJK 二元组相似度自动补足至少 2 条关联链接及一句说明，不足时告警；重复笔记默认仅提合并建议，仅在用户显式同意（`--yes`）时才合并并归档原件至 `hub/reports/brain-archive/`。
  - 只读检索问答（`hushai brain ask`）：**绝不调用模型编造**，先读 `hub/index.md` 并经 `fsio.read_for_prompt` 读取允许范围笔记（严格遵守 `local_only`），原句引用并附链接；遇到笔记冲突时并列展示双方；未覆盖时明说并给出补充建议。
  - 首次运行 5 问引导（`hushai brain onboard`）：询问 5 个问题并处理最多 10 条初始条目，输出包含文件夹地图、`index.md` 与前 5 条笔记的 `"HERE'S YOUR BRAIN"` 总览；第二天起自动跳过问题并用一句话开场。
  - 4 个周期 Routine 与独立 systemd 模板（`[brain]` 开关**默认全部禁用**，须执行 `hushai brain enable` 才开启）：`daily`（21:00，`hub/daily/YYYY-MM-DD.md` + 3 行摘要、无新内容静默）、`weekly`（周日 18:00，`hub/weekly.md`）、`resurface`（每日 1 次唤醒相关旧笔记）、`monthly`（月度重复整理/断链修复/6 个月孤立笔记标记/`index.md` 清理并产出变更报告）；新增 `hushai install units --with-brain` 与 `hushai brain units`。
- **文档升级（`README.md` 与 `docs/second-brain.md`）**：
  - 全面重写 `README.md`，明确「多端统一 AI 记忆中枢与集中调度管家」核心定位，补充办公室电脑、家庭笔记本及手机端（Claude App、Muse、Duet、手机浏览器）的接入方式与 `hushai config` 菜单指引。
  - 新增集成设计文档 [docs/second-brain.md](./docs/second-brain.md)。

### Security
- `agy` 问答式调用加会话级拒绝规则（命令、读写文件、网页、MCP 全部禁用）。实测此前 `agy -p` 能写文件、跑命令，收件原文可借汇总执行命令。
- 文件追加加 `O_NOFOLLOW` 并写满为止；新建文件权限 `0600`，覆写沿用原权限。
- 交换区存文件用 `O_EXCL` 先占名，并发同名上传不再互相覆盖。
- Muse 闸门写入白名单按目录边界匹配；`append-inbox` 拒绝不存在的日期（如 2026-02-31）。

### Fixed
- 自愈：挂载单元先过白名单再卸载（此前会先 `fusermount -uz` 再被白名单拒绝，挂载起不来）；白名单接受顶级 `mnt-onedrive.mount`。
- 开放项保存改为先归档、后重写，并按编号去重；解析不了的条目行存入 `reports/quarantine.md`，不再随重写消失。
- 额度用尽自动切换后检查重启结果，失败如实上报；tmux 目标改精确匹配，额度识别只看屏幕最后 40 行。
- 汇总、周重写拿锁时最多等 60 秒（`HUSHAI_LOCK_WAIT`），不再因同一刻的几秒对账而整晚跳过；自检有独立 `heal.lock`。
- 子进程超时整组终止，不留下孙进程。
- `install units` 的启用命令包含 `steward.service`。

## [0.1.0] - 2026-11-14

### Added
- **纯文本记忆底座（Vault）**：初始化（`init`）、规范校验（`validate`）、全量体检（`doctor`）、路径护栏（`guard` / `fsio`）、只追加收件（`inbox add`）及 `line_id` 确定性去重。
- **每日与每周闭环**：机械对账（`reconcile` / `todo`）、每日汇总（`digest`）、每周近况归档与认知重写（`weekly`）、运行账本（`ledger`）。
- **双模型适配与回退链**：内置 `claude`、`agy` 与离线测试 `fake` 适配器，支持 `light/heavy` 档位回退、每日预算限制、单实例 `flock` 文件锁与 `dispatch` 代理派活。
- **各端受控接入与简报界面**：JSON 与 `muse-gate` 兼容 SSH 闸门（`gate` / `bin/hushai-gate-ssh`）、事件监听与跨端只读路由（`on-event`）、晨间简报（`brief`）与带令牌验证的本机待办页面（`serve`）。
- **常驻管家与零维护自愈**：`hushai steward`（`status` / `use claude|agy` / `restart`）、`hushai heal` 8 项静默自检（含 `steward_online` tmux 拉起与 Claude 额度用尽自动切 `agy`）、会话结束自动采集（`capture`）、按归属单向同步（`sync`）、安装向导与端接入包（`install wizard` / `doorkit` / `units`）以及导出/升级/卸载（`export` / `upgrade` / `uninstall`）。
