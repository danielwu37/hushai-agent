# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and release notes are aggregated automatically via [GitHub Releases](https://github.com/danielwu37/hushai-agent/releases).

## [Unreleased]

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
