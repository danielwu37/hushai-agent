# hushAi-agent 安装与维护手册

> 面向安装者（为自己或替家人部署维护的人）。目标是：30 分钟内在一台 Ubuntu 服务器上从零装好，之后日常维护时间为 0。

---

## 1. 环境要求与目录布局

- **操作系统**：Ubuntu 22.04 LTS 或更高版本（内存建议 ≥ 2 GB）。
- **Python**：Python 3.10 及以上（只用标准库，无需安装任何第三方 Python 依赖包）。
- **AI 命令行工具**：本机已安装并登录至少一家适配工具（推荐同时安装 `claude` 与 `agy`）。
- **四个标准路径**（按实例名 `<实例>` 隔离）：
  - 框架代码：`/opt/hushai-agent/`
  - 数据正本（必须在服务器本地盘，绝不放在网盘挂载点上）：`~/hushai-vault/`
  - 实例配置：`~/.config/hushai/<实例>/`
  - 运行状态与账本：`~/.local/state/hushai/<实例>/`

---

## 2. 30 分钟从零安装（一站式向导）

### 第 1 步：安装命令行入口

```bash
# 非 root 用户先创建目录并赋权：sudo mkdir -p /opt/hushai-agent && sudo chown "$USER:$USER" /opt/hushai-agent
git clone https://github.com/danielwu37/hushai-agent.git /opt/hushai-agent
cd /opt/hushai-agent
# 方式 A（推荐 Debian/Ubuntu）：免 pip 直接软链到 PATH
mkdir -p ~/.local/bin && ln -sf /opt/hushai-agent/bin/hushai ~/.local/bin/hushai
# 方式 B：或在 Python venv 内以可编辑模式安装
# python3 -m venv .venv && .venv/bin/pip install --no-build-isolation --no-deps -e .
hushai version
```

### 第 2 步：运行一站式安装向导

```bash
hushai install wizard \
  --instance demo \
  --vault ~/hushai-vault \
  --timezone Asia/Shanghai \
  --lang zh \
  --steward claude \
  --agy-light gemini-3.8-flash-low \
  --agy-heavy gemini-4-argon-high \
  --claude-light sonnet \
  --claude-heavy opus
```

向导会自动完成四件事：
1. 初始化完整 `vault` 目录结构与本地 Git 仓库（仅作本地审计与回滚，不推送外部）。
2. 生成 `~/.config/hushai/demo/config.ini`，配好 `agy` 与 `claude` 双适配项及回退链（`digest = agy:light, claude:light`、`weekly = agy:heavy, claude:heavy`）。
3. 在 `~/.config/hushai/demo/units/` 生成 7 个 systemd 服务单元，在 `~/.config/hushai/demo/doorkit/` 生成两家工具的端接入包。
4. 自动执行 `hushai doctor --instance demo` 体检。

### 第 3 步：自检两家适配项

```bash
hushai adapter test agy --instance demo --tier light
hushai adapter test claude --instance demo --tier light
```

### 第 4 步：启用 systemd 定时与事件监听单元

核对向导打印的命令后执行：

```bash
sudo cp ~/.config/hushai/demo/units/hushai-demo-*.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  hushai-demo-watch.path \
  hushai-demo-digest.timer \
  hushai-demo-weekly.timer \
  hushai-demo-serve.service
```

---

## 3. 多实例与同账号零冲突隔离方案

框架支持两种多实例隔离方式：

| 场景 | 推荐方式 | 做法 |
| --- | --- | --- |
| 一家人共用一台服务器 | **一个使用者一个 Linux 系统用户** | 每个用户在各自家目录下运行 `hushai install wizard`，由操作系统天然隔离 |
| 同一账号下并存多个实例（或在旧系统旁试用） | **原生参数零冲突隔离** | 使用 `doorkit` 生成的 `start-claude-steward.sh` 启动管家 |

### 同账号下启动 Claude 管家的原理

`~/.config/hushai/<实例>/doorkit/start-claude-steward.sh` 使用以下命令启动：

```bash
cd "<vault>" && exec claude --setting-sources project,local --settings "<配置目录>/doorkit/claude-settings.json"
```

- `--setting-sources project,local`：**完全跳过** `~/.claude/CLAUDE.md` 与 `~/.claude/settings.json`，旧系统的全局规则和钩子不会混入新实例。
- **共用同一个 OAuth 登录凭据**：无需建第二个配置目录重复登录，0 踢下线风险。
- 后台问答式调用（`ClaudeAdapter.complete`）固定携带 `--safe-mode --no-session-persistence --tools ""` 并以空目录 `empty-cwd` 为工作目录，绝不触发任何全局钩子或落盘会话垃圾。

---

## 4. 待办页面与网盘同步配置

### 4.1 待办页面（`hushai serve`）

- 安全边界：`hushai serve` 强制只监听 `127.0.0.1:<端口>`。
- 首次启动自动生成权限为 `600` 的令牌文件 `~/.config/hushai/<实例>/serve.token`。
- 本机或经反向代理访问 `https://<你的域名>/?t=<令牌内容>` 即可种下 `HttpOnly; SameSite=Strict` 会话 Cookie，之后直接打开首页即可点选。

### 4.2 按归属单向同步（`hushai sync`）

在 `config.ini` 的 `[sync]` 节填入 `rclone` 远端（如 `remote = onedrive:hushai-vault`）：
- `hushai sync --instance demo --direction both`：
  - **push（服务器 → 网盘）**：只推约定层、派生层、简报与管家自己的收件。
  - **pull（网盘 → 服务器）**：只拉其他端的收件与 `exchange/in/`，拉完立即触发 `on-event` 对账。
- **绝不读写任何挂载点**，防止网盘故障时卡死进程；`[privacy] local_only` 下的路径（默认 `domains/health`）硬性排除在同步与模型提示词之外。

---

## 5. 体检、北极星指标与生命周期维护

### 5.1 体检与运行账本

```bash
hushai doctor --instance demo
hushai ledger --instance demo --limit 20
```

- **北极星指标**（使用者每周维护时间目标为 0）：
  1. **需人工介入次数**：账本 `runs.jsonl` 中 `result == "needs_human"` 的记录数 + `doctor` 报 `FAIL` 数。
  2. **系统自嗨率**：系统自身运行记录只进 `runs.jsonl`，绝不写进收件、近况或今日待办（目标 0%）。

### 5.2 备份导出、模板升级与卸载回滚

```bash
# 1. 完整导出（不含令牌等敏感凭据与 .git，可打包为 .tar.gz 随时迁机）
hushai export --instance demo --out ~/hushai-demo-backup.tar.gz

# 2. 升级框架托管规则模板（只更新 AGENTS.md 等模板，绝不碰 profile.md、AGENTS.local.md 与收件数据）
hushai upgrade --instance demo

# 3. 卸载实例（默认永久保留 vault 记忆库！不加 --yes 先演练预览；加 --yes 删除配置/状态；加 --with-systemd 一并清理 systemd；仅在确认无需个人资料时才可追加 --delete-vault）
hushai uninstall --instance demo
hushai uninstall --instance demo --yes --with-systemd

# 4. 独立一键卸载 / 环境清理脚本（默认永久保留 vault；删除 vault 需单独二次危险确认或显式 --delete-vault；亦可在 hushai config 菜单 8 -> 8 触发）
/opt/hushai-agent/bin/hushai-uninstall
```

---

## 6. 账号条款与安全须知

- **个人订阅条款**：每个使用者应使用自己的 AI 账号。各家工具关于个人订阅能否用于后台定时任务、以及工具间调用的条款以官方最新服务条款（Terms of Service）原文为准，建议将每日模型调用预算 `[budget] max_daily_calls` 保持在合理低值（默认 20 次/天）。
- **默认安全护栏**：
  1. 定时汇总与跨端路由全部使用**无工具问答式接口（`complete`）**，外来内容即使包含提示词注入也无法改动任何文件或执行命令。
  2. 框架所有文件写入强制经过 `guard.ensure_writable` 校验，越界路径与外部符号链接一律硬性拒绝。
  3. 拼入提示词的文件强制经过 `fsio.read_for_prompt` 校验，`[privacy] local_only`（如 `domains/health`）内容永不出本机。

---

## 9. 常驻管家会话与模型切换（`hushai steward`）

服务器可运行一个常驻 tmux 会话（`hushai-<实例>`，由 `hushai-<实例>-steward.service` 托管），工作目录固定为实例的 `vault`，并在本机已登录认证的 **Claude** 与 **Antigravity (`agy`)** 之间通过一条命令切换。

### 9.1 常用命令

```bash
# 查看当前后端（claude 或 agy）、systemd 单元状态与 tmux 会话存活情况
hushai steward status --instance <实例>

# 切换到 agy 后端并重启常驻管家会话（切换记录自动写入 hub/inbox/<日>-steward.md）
hushai steward use agy --instance <实例>

# 切换到 claude 后端并重启常驻管家会话（若仅改配置不立即重启可加 --no-restart）
hushai steward use claude --instance <实例>

# 手动重启常驻管家服务
hushai steward restart --instance <实例>
```

### 9.2 手机端如何连接常驻管家

- **当后端为 `claude` 时**：
  `start-steward.sh` 运行 `claude remote-control --name "hushAi 管家" --spawn same-dir --permission-mode acceptEdits`。手机打开 Claude App（同一账号），在会话列表选择 `hushAi 管家` 即可直接对话。
  > 注：`claude remote-control` 子命令解析器不接受 `--setting-sources` 与 `--settings` 参数；会话在 `vault` 目录启动时自动加载 `<vault>/CLAUDE.md` 与 `<vault>/.claude/settings.local.json`，并通过环境变量 `HUB_CAPTURE_CHILD=1` 跳过宿主机全局旧钩子；`settings.local.json` 里的 `claudeMdExcludes` 排除宿主机 `~/.claude/CLAUDE.md`（尊重 `CLAUDE_CONFIG_DIR`），实现同账号零冲突隔离。
- **当后端为 `agy` 时**：
  `start-steward.sh` 运行 `agy --remote-control --mode accept-edits --settings <doorkit>/agy-settings.json`。
  > 注：`agy-settings.json` 仅包含 `settings.json` 合法字段（如 `trustedWorkspaces`）；生命周期钩子由工作区 `<vault>/.agents/hooks.json` 加载。
  1. **官方手机网页伴侣（Web Companion）**：Antigravity 无独立原生手机 App，手机浏览器（Safari / Chrome）访问 `https://antigravity.google`，登录同一 Google 账号，在顶部实例切换器中选择服务器会话即可远程操控（可「添加到主屏幕」作为 Web App 使用）。
  2. **SSH + tmux 保底方案**：若手机网络不便访问 Google 网页端或 WebRTC 受阻，可用手机 SSH 客户端（如 Termius、Blink）连接服务器后执行 `tmux attach -t hushai-<实例>` 直接进入同一个管家终端。

### 9.3 管家在线检查（`heal` 第 8 项 `steward_online`）

`hushai heal` 每小时 :50 检查一次 `steward_online`（管家在线）：若 `hushai-<实例>-steward.service` 不在运行或 `tmux -L hushai-<实例>` 会话不在，自动重启服务拉起会话，最多 2 次；仍不行写一条 `needs_human` 系统告警。

额度用尽**不会**自动切换模型（2026-11-17 起彻底移除：remote-control 下测不到真实额度信号，误判会重启并打断对话）。需要换模型时手动执行 `hushai steward use agy|claude`。

---

## 10. Muse / 移动端 SSH 闸门配置

在 `config.ini` 的 `[gate]` 节可配置只读外部目录根、私密路径黑名单文件、允许写入前缀与日志路径：

```ini
[gate]
read_roots = workos=/path/to/workos, music=/path/to/music
private_paths_file = /path/to/private-paths.txt
# 可选：private_paths_file 的规则只作用于这些读根（相对该根匹配），逗号分隔
private_paths_scope = workos
write_allow = 00-我/交换/入/, hub/exchange/in/, music/Playlists/_输入清单/待下载/
log_file = /var/log/hushai-<实例>-gate.log
```

在 `~/.ssh/authorized_keys` 中将对应公钥绑定到独立入口脚本 `bin/hushai-gate-ssh`（兼容 `python3 -I`）：

```text
restrict,command="/usr/bin/python3 -I /path/to/hushai-agent/bin/hushai-gate-ssh --instance <实例> --door muse" ssh-ed25519 AAAA...
```

---

## 11. 版本查看与自动发布工作流

### 如何知道当前部署版本及更新了什么

```bash
# 查看本机当前安装版本号
hushai version

# 查看完整版本更新说明
cat /opt/hushai-agent/CHANGELOG.md

# 拉取远端标签与代码，查看即将升级的提交清单
git -C /opt/hushai-agent fetch --tags
git -C /opt/hushai-agent log HEAD..origin/master --oneline
```

或直接访问仓库 [GitHub Releases](https://github.com/danielwu37/hushai-agent/releases)，每个发布版本都会列出人类可读的功能/修复摘要以及对应的提交列表。

### 自动发布工作流如何运作

项目提供“本地一键自动化发布”与“GitHub Actions 云端发布模板”两种协同方式（二者共用同一套提取与校验脚本 [`tools/release_tool.py`](../tools/release_tool.py)）：

1. **本地一键发布（开箱即用，不依赖 `workflow` OAuth 权限）**：
   ```bash
   # 1) 同步四处版本号并把 CHANGELOG.md 的 [Unreleased] 升格为 0.2.2
   python3 tools/release_tool.py bump 0.2.2
   git add pyproject.toml src/hushai_agent/__init__.py README.md CHANGELOG.md
   git commit -m "release(v0.2.2): <一句话摘要>"

   # 2) 一条命令自动：校验版本一致 → 跑通全量测试 → 打标并推送 → 提取变更说明与提交清单创建 GitHub Release
   python3 tools/release_tool.py publish 0.2.2 --title "v0.2.2: <Release 标题>"
   ```

2. **启用 GitHub Actions 云端自动测试与标签自动发版（可选）**：
   仓库已在 [`tools/workflows/ci.yml`](../tools/workflows/ci.yml) 与 [`tools/workflows/release.yml`](../tools/workflows/release.yml) 备好全套 GitHub Actions 配置。GitHub 规定向 `.github/workflows/` 推送文件需要账号令牌具备 `workflow` scope；授予该权限后执行一次即可启用云端 CI 与“推送 `v*` 标签自动发布 Release”：
   ```bash
   gh auth refresh -h github.com -s workflow
   mkdir -p .github/workflows && cp tools/workflows/*.yml .github/workflows/
   git add .github/workflows && git commit -m "ci: enable GitHub Actions CI and tag release" && git push
   ```

