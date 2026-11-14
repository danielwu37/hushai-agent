# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and release notes are aggregated automatically via [GitHub Releases](https://github.com/danielwu37/hushai-agent/releases).

## [Unreleased]

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
