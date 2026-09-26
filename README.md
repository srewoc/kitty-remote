# Kitty Remote

中文 | [English](README.en.md)

从手机网页查看、操作和新建 Ubuntu 上的 **Kitty 终端窗口**。电脑上的任务继续在原进程中运行，手机通过可信自有中转连接本机代理。

首版提供文字快照、历史阅读、中文多行输入、按键栏、独立桌面窗口创建、登录配对与撤销。新窗口启动普通 Shell，默认目录为本机用户的 `~`。按已确认的选择，首版采用文字模式；Kitty 0.49 的原生截图列为后续功能。

## 工作原理

```
手机浏览器 ⇄ HTTPS/WSS ⇄ 自建中转 ⇄ WSS（电脑主动连出） ⇄ 本机代理 ⇄ Kitty 远控 Socket ⇄ 已在运行的窗口
```

- **不用事先准备**：直接接管已经在运行的 Kitty 窗口，不需要 tmux，也不用换成特定命令启动。Claude Code、Codex、普通 Shell 都一样。
- **只传变化的行**：代理约每秒读取一次窗口的完整缓冲，只把变化的行发给手机；手机原地替换这些行，阅读位置不动。
- **输入严格命中目标**：窗口身份校验（窗口 ID 被复用时拒绝旧目标）、请求去重、回执区分“已提交”与“结果不确定”、每个窗口只有一个控制者。
- **自建中转**：电脑主动连到你的服务器，不需要开放电脑端口，也不依赖第三方隧道。

**当前状态**：作者在自有服务器上以公网 HTTPS 部署并日常使用。新建桌面窗口、原生滚动的终端视图（只改变化的行，不分阅读/跟随模式）都已通过本地与公网独立 Kitty 的真实链路验收。安卓真机、Codex/Claude 完整任务及审批流程还待专项验收。

## 本地运行

环境：Ubuntu 桌面、Python 3.12、uv、Node.js 24 LTS、Kitty。当前验收版本 Kitty / kitten 为 0.49.0。

在项目根目录安装依赖、构建网页并创建唯一管理员：

```bash
uv sync --locked --python 3.12
npm ci --prefix web
npm run build --prefix web
uv run kitty-remote create-admin --username admin
```

密码通过交互输入，至少 12 个字符。SQLite 默认位于 `.state/relay.sqlite3`。

启动中转并保持运行：

```bash
KR_DEV=1 KR_ORIGIN=http://127.0.0.1:8765 uv run kitty-remote relay
```

浏览器打开 **http://127.0.0.1:8765** 并登录。在另一个终端发起配对：

```bash
uv run kitty-remote pair --relay http://127.0.0.1:8765 --allow-http --name '我的 Ubuntu'
```

网页点击“配对电脑”，输入命令显示的 8 位配对码，核对名称并确认。命令会领取凭据，写入 `~/.config/kitty-remote/agent.json`，权限 `0600`。

随后启动代理：

```bash
uv run kitty-remote doctor
uv run kitty-remote agent
```

网页选择窗口后即可读写。开发 HTTP 仅允许 localhost；安卓外网使用后续 HTTPS/WSS 部署入口。

## Kitty 接入

先运行 `doctor` 检查现有配置。已启用远控 Socket 的实例可直接接入。Kitty Remote 使用独立的窗口变量，不影响其他工具。

新配置示例，放入 `~/.config/kitty/kitty.conf`：

```conf
allow_remote_control socket-only
listen_on unix:${XDG_RUNTIME_DIR}/kitty-remote-{kitty_pid}
```

启用监听通常需要新启动 Kitty 实例。先安排现有任务；本项目不会自动关闭或重启终端。

默认发现 `$XDG_RUNTIME_DIR/kitty-remote-*`。已有其他命名的监听地址时，用下面的参数指定：

```bash
uv run kitty-remote doctor --socket unix:/绝对路径/kitty.sock
uv run kitty-remote agent --socket unix:/绝对路径/kitty.sock
```

`--socket` 可以重复，显式指定时仅接入所列地址；可另加 `--socket-glob '/绝对路径/kitty-*'`，指定后不再使用默认的 `kitty-remote-*`。使用当前桌面用户运行代理，Socket 保留在本机。

## 手机操作

- 选择设备下的窗口。每个窗口只有一个浏览器控制者，其他连接只读；控制者离开后可申请控制权。
- 输入框 Enter 换行；“发送文字”仅粘贴，“发送并回车”另行发送 Enter。发送交给连接后清空输入框；未发送的草稿按窗口分别保留。
- 输入法组合文字期间禁用发送。语音输入使用手机输入法自带的语音转文字。
- 常驻两排按键提供 Esc、Tab、Shift+Tab、Enter、Ctrl+C、四个方向键及 Backspace；“更多按键”提供 Home/End、PageUp/PageDown、编辑键、Ctrl/Alt 快捷键及 F1–F12，均明确匹配所选窗口。
- 选择窗口后自动加载 Kitty 当前保留的全部历史，作为一整段文字用手机原生滚动查看；滑动只读取本地内容，不会随滚动分页请求。之后有变化只替换变化的行，不再整页重画。停在底部时自动跟随新输出；往上翻时位置保持不动，右下角出现“回到最新 ↓”，有新内容时变为“有新输出 ↓”。字号变化保留阅读行，宽行可横向查看；中文按终端两格对齐。
- “固定布局”由用户手动开启：终端、输入框与发送按钮留在可视区域，设备、更多按键和回执通过抽屉查看；软键盘打开时按可视高度调整。刷新页面后需重新开启固定模式。
- PageUp/PageDown 是发送给电脑程序的按键，与手机本地滑动历史不同。备用屏幕程序自行保存、尚未绘制的历史不在 Kitty 的可读缓冲中。例如 Claude Code 设置 `"tui": "fullscreen"` 时运行在备用屏幕，Kitty 里只有当前一屏。在 Claude Code 里执行 `/tui default`，或启动 Codex 时加 `--no-alt-screen`，历史就会留在 Kitty 的回滚区。
- “已提交至 Kitty”仅表示控制调用返回；任务进展看屏幕。“结果不确定”时先查看屏幕，系统不会自动重发。
- “撤销设备”使设备凭据立即失效。重新使用需移走旧凭据文件后再次配对。

代理重启、Kitty 实例变化、窗口关闭或身份标记变化会使旧目标失效。电脑键盘仍可输入，应避免两端同时编辑同一行。

## 检查与验收

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
npm run typecheck --prefix web
npm test --prefix web
npm run build --prefix web
```

真实桌面验收：

```bash
uv run playwright install chromium
uv run python scripts/e2e.py
```

脚本创建并最终关闭独立 Kitty 实例、两个测试窗口、临时中转、代理及无头浏览器，仅向专用测试窗口注入文字。结果和手机/桌面截图写入 `artifacts/`（已忽略）。可用 `KR_CHROMIUM_PATH` 指定已有 Chromium。

升级 Kitty 后，用 `kitty +launch scripts/gen_widths.py` 重新生成字符宽度表。

## 文档与结构

- [实施方案与状态](docs/implementation-plan.md)
- [验证记录及待验项](docs/verification.md)
- [部署、服务安装和卸载](docs/deployment.md)
- [协议 v1](docs/protocol-v1.md)
- [初始设计草案（历史）](docs/design-background.md)

| 部分 | 位置 |
| --- | --- |
| 共享模型、ANSI 过滤、有界队列 | `src/kitty_remote/protocol.py` |
| Kitty 身份校验、命令封装 | `src/kitty_remote/kitty.py` |
| 代理、输入去重 | `src/kitty_remote/agent.py` |
| 中转、会话、租约与路由 | `src/kitty_remote/relay.py` |
| SQLite、密码和凭据摘要 | `src/kitty_remote/storage.py` |
| 管理命令 | `src/kitty_remote/cli.py` |
| React / TypeScript，终端行渲染与差量 `web/src/screen.ts` | `web/` |
| Docker、Caddy、systemd 模板 | `deploy/` |

限制：文字模式不还原图片、动画或全部字体特性；复杂 Emoji、连字、宽字符仍需针对实际应用检查。连续历史无固定行数上限，UTF-8 总量保护上限为 16 MiB，单条 WebSocket 消息仍不超过 1 MiB；自动分块，超限明确报错而不截断。Kitty 已丢弃的历史无法恢复。可信中转能够看到内容，默认不持久化正文与输入。中转仅运行一个进程，不支持多副本路由。

## 与同类项目的区别

截至 2026-09 的调研：
- **同样用 Kitty 远控接口的项目**：[kitty-remote-deck](https://github.com/identxxy/kitty-remote-deck)、[term-deck](https://github.com/adrian-wulf/term-deck)。它们每次刷新都重传整份文本，查看历史时暂停更新。
- **手机操控 AI 编程工具的项目**：[Happy](https://github.com/slopus/happy)、[VibeTunnel](https://github.com/amantus-ai/vibetunnel)、[handmux](https://github.com/handmux/handmux)、[itwillsync](https://github.com/shrijayan/itwillsync) 等。它们要求任务通过自己的包装命令或 tmux 启动。
- **官方方案**：Claude Code 的 [Remote Control](https://code.claude.com/docs/en/remote-control) 只接管单个 Claude 会话，界面是聊天形式；Codex 的[手机远控](https://developers.openai.com/codex/remote-connections)目前要求主机是运行 Codex App 的 Mac。

## 许可证

[MIT](LICENSE)
