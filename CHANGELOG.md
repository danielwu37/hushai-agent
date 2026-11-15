# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and release notes are aggregated automatically via [GitHub Releases](https://github.com/danielwu37/hushai-agent/releases).

## [Unreleased]

### Added
- **P0 / O1 事件流并发防丢与目标文件校验型崩溃恢复**：
  - 新增 [`checkpoint.py`](src/hushai_agent/checkpoint.py) 与 [`checkpoint_verify.py`](src/hushai_agent/checkpoint_verify.py)（检查点落于 `<state>/checkpoints/`），恢复阶段直接对 Vault 与 State 目标文件做内容摘要、行编号与提交态核验（绝不轻信 JSON `stage` / `brain_done` 字段）：覆盖 `open-items.md`、`closed-items.md`、`today.md`、`status.md`、`status-index.md`、`corrections.md`、`status-archive.md`、`candidates.md`、`profile.md`、`AGENTS.local.md`、`decisions.md`、`lessons.md`、`morning-brief.md` 与 `.html` 渲染一致性、`routed.jsonl` 与管家回执、`daily/<date>.json`、`notes/`、`sources/`、`people/`、`projects/`、`daily/<date>.md` 与 `index.md` 链接；损坏或结构非法检查点自动隔离至 `checkpoints/corrupt-*`；`heal.run_heal` 同步接入非阻塞遗留检查点接管。
  - [`lock.py`](src/hushai_agent/lock.py) 新增 `acquire_or_mark_dirty`（落盘 dirty 标记与二次抢锁重检）及 `drain_after_unlock`，消除 `on-event` / `reconcile` / `routing` 在 `lock-busy` 下的事件漏触发。
  - `brain_routines`（`daily` / `weekly` / `resurface` / `monthly`）与 `brain_process` 统一受 `runner.lock` 互斥保护并在释锁后排空机械积压；[`brain_model.py`](src/hushai_agent/brain_model.py) 与 [`brain_process.py`](src/hushai_agent/brain_process.py) 扩展 `sources`/`people`/`projects`/`notes` 全目录 `source_ref` 写入与按目标拆分幂等复用。新增多阶段崩溃、真实子进程 `SIGKILL` 与多进程锁竞争测试 [`test_p0_o1_resilience.py`](tests/test_p0_o1_resilience.py)。
- **P0 / O2 `<state>/outbox/` 纯文件交付状态机（at-least-once）**：
  - 新增 [`outbox.py`](src/hushai_agent/outbox.py)、[`outbox_store.py`](src/hushai_agent/outbox_store.py) 与 [`cli_outbox.py`](src/hushai_agent/cli_outbox.py)（注册 `hushai outbox <status|list|flush|recover|retry|archive>`），实现 `<state>/outbox/{pending,sending,sent,failed,archive}/` 分态迁移（以单调递增 `transition_seq` 防止同时间戳或 `SIGKILL` 写删间隙崩溃后的重复状态覆盖）、独占锁下 `sending` 中断回收、指数退避重试、幂等键去重（支持 `dedupe.json` 丢失或损坏时连同 `archive/` 全量重建与诊断健康度上报、且空实例静默）、DST/跨时区 UTC instant 比较及历史归档，显式声明 **at-least-once（至少一次）** 投递语义（不宣称外部消息恰好发送一次）。
  - 集成至 [`checkpoint_verify.py`](src/hushai_agent/checkpoint_verify.py)（`exchange_out` 交付登记）、[`gate.py`](src/hushai_agent/gate.py)（`check-inbox --ack` 标定 `acked_at`）、[`on_event.py`](src/hushai_agent/on_event.py) 与 [`heal.py`](src/hushai_agent/heal.py)（无待投递/待恢复信封保持完全静默，并在崩后或索引损坏时保留 `today.md` 24 小时告警冷却去重）。新增测试 [`test_p0_o2_outbox.py`](tests/test_p0_o2_outbox.py)（8 项生命周期、同时间戳双份对账、真实 `SIGKILL`、`archive` 兜底重建及跨时区/DST/时钟回拨与 `heal` 崩后去重验证）。

### Fixed
- **修复 `[brain]` 时间与星期参数未进入 systemd 单元**：`install.render_brain_units`、`install.render_units(..., with_brain=True)` 与 `hushai brain units` 改为默认读取 `config.ini` 中的 `daily_time` / `weekly_day` / `weekly_time` / `resurface_time` / `monthly_time`（`BrainConfig` 新增 `monthly_time` 并校验 `HH:MM` 与 `Mon..Sun`）；`brain-weekly.timer` 与 `monthly.timer` 模板改用变量替换，不再写死 `Sun` 与 `09:30`。
- **修复既有 `digest.timer` 自定义时间被重生成单元覆盖**：新增 `install.detect_existing_digest_time`，`hushai install units` 与 `hushai config` 5→4 在未显式传 `--digest-time` 时默认沿用既有 `.timer` 触发时间。
- **修复 `hushai serve` 启动后缓存配置不生效**：每次鉴权请求实时重载 `config.ini`（`Handler._live_cfg`），修改 `known_doors`、`today_items` 等配置无需重启即可生效，被移除的端即时返回 `403`。
- **修复 `run_daily_brain` 同日多次覆盖日报明细**：同一天多次执行 `daily` 时改为去重累计合并当日 `## 今日归档明细`，不再覆盖早先归档记录。
- **修复 `hushai sync` 漏传第二大脑目录**：`PUSH_INCLUDES` 补入 `/index.md`、`/weekly.md`、`/notes/**`、`/sources/**`、`/people/**`、`/projects/**`、`/daily/**`，同时保留 `[privacy] local_only` 排除规则且不触碰 `domains/`。
- **修复配置校验、大小写与回滚漏洞**：`update_ini_text` 支持大小写不敏感键匹配；新增 `validate_cross_constraints` 拦截非法 `vault`、`local_only` 越界路径、孤立适配器引用、`approval`/`trusted` 越界端及与既有收件冲突的领域修改并自动回滚；禁止 `sync.timeout` / `exchange_max_kb` / `candidates_pending` 填 `0`；`set_brain_routine_enabled` 与 `steward.set_backend` 统一经 `config_edit` 原子写入。
- **修复第二大脑自动补链与 Ask 检索质量问题**：新增 `extract_semantic_body` 剔除头部元数据与 `## 关联笔记` 段落并过滤状态模板词/日期/`L-...` 编号/疑问虚词；`brain ask` 仅在正文选引用并提高数量提问数值加权；`_extract_title` 规整删除 URL 后的多余空白。

### Changed
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
