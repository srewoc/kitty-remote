# 本地与公网验证记录

最新本地与公网验证：2026-09-26 17:42。状态：**原生滚动终端视图、连续历史、手机固定布局、发送后清空输入和新建独立 Kitty 桌面窗口已通过独立 Kitty 的本地与公网真实链路验收；安卓真机专项仍待完成。**

## 环境

- Ubuntu 桌面，本机 Kitty / kitten 0.49.0。
- Python 3.12，uv 创建项目 `.venv`，依赖锁定在 `uv.lock`。
- Node.js 24.16.0、npm 11.13.0；前端依赖由 `web/package-lock.json` 锁定。
- Playwright + 本机 Chromium，桌面 1280×920、手机模拟视口 390×844。
- 已检查安装版本：Codex CLI 0.156.1、Claude Code 2.1.281。下面的输入验收对象为独立原始终端测试程序，并非已完成这两个产品的全部交互验收。

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| Python pytest | 45 项通过 |
| Ruff 静态检查 | 通过 |
| Ruff 格式检查 | 通过 |
| TypeScript 类型检查与 Vite 生产构建 | 通过 |
| Vitest 前端测试 | 10 项通过 |
| Docker Compose 配置解析 | 通过 |
| 真实浏览器 → 中转 → 代理 → Kitty | 通过 |

后端测试覆盖跨机器正负时钟偏差、保守截止时间转换、输入 UTF-8 上限、非法按键、过期请求、队列上限、输出超限终止、重复/冲突 ID、部分发送、断线不补发、旧 epoch、窗口关闭/ID 复用/标记丢失/Socket 身份变化、历史上限、消息编码后上限、密码及凭据摘要、来源校验、未登录访问、控制权隔离、撤销与登出。

pytest 有一条上游 Starlette 关于未来使用 httpx2 的弃用提示，当前测试仍全部通过。

## 2026-09-25 手机版交互验收

新增覆盖：完整缓冲的分块组装、超过 1 MiB 的中文与 Emoji、重复行与删除/替换差量、基础版本错误、分块缺失/乱序、16 MiB 超限、慢连接合并最新状态、回执优先发送、中转重新全量同步以及扩展按键白名单。

`uv run python scripts/e2e.py` 在专用 Kitty 测试实例输出 10,000 行，核对从 `large history 00000` 至 `large history 09999` 的完整内容，UTF-8 大于 1 MiB；代理自动分块，手机滚动不发送历史读取请求。

- Chromium 触摸事件模拟滑动后自动进入历史阅读；新输出到达时阅读位置不变，点击回到最新后显示新内容。
- 阅读中增大、减小字号，首个可见文本行保持不变。
- 固定模式覆盖 390×844、390×480（缩小可视高度）和 844×390 横屏，输入与发送按钮在视口内，无外层纵向滚动。
- PageUp、F12、Shift+Tab 不只核对网页请求，还在专用 Kitty 输入日志中验证实际接收到对应字节。
- 中文、多行、Emoji、组合输入保护、中断、备用屏幕、窗口隔离、单一控制者、重连不重复输入和撤销继续通过；浏览器错误为空。

产物：`artifacts/e2e-results.json`、`artifacts/console-mobile-pinned.png`、`artifacts/console-mobile-keyboard-height.png`、`artifacts/console-mobile-landscape.png`。截图与结果为本地验收；`android_device_tested=false`、`public_network_tested=false`。模拟触摸和缩小视口不替代真实安卓输入法、浏览器工具栏及软键盘验收。

2026-09-25 后续布局更新：390×844 固定布局的终端显示框实测 547 像素；默认保留输入，按键布局可切换，未发送草稿切回后保留。两种文字发送操作成功交给 WebSocket 后清空输入；390×480 与 844×390 的可见性检查继续通过。按键布局截图为 `artifacts/console-mobile-keys.png`。

2026-09-26 新建窗口验收：45 项 Python 测试覆盖固定创建参数、旧目标拒绝、调用结果不确定、重复请求只执行一次，以及中转控制权与回执路由。前端 10 项测试、TypeScript、Vite 构建、Ruff 检查通过。本地及公网 `scripts/e2e.py` 在专用 Kitty 实例中从手机模拟视口点击“新建桌面窗口”，确认新 OS 窗口位于同一 Kitty 实例、网页自动订阅，并在新 Shell 执行 `pwd` 得到本机用户 `~`。公网运行返回 `create_os_window_home_shell=true`、`public_network_tested=true`、`browser_errors=[]`、`android_device_tested=false`；测试设备已撤销，测试 Kitty 已清理。

## 2026-09-26 原生滚动终端视图验收

手机滑动历史不跟手，且存在“阅读历史 / 实时跟随”两套模式。原因是 xterm.js 每次内容变化都会清屏，再把整份历史重写一遍：Claude Code 的转圈计时每秒都会触发一次。为了保住阅读位置，旧版只能在上滑时冻结画面。本次改为原生滚动的网页文字行：每次只替换变化的行，识别顶部滚出的历史；停在底部时跟随，否则保持阅读行不动。中文按终端两格对齐。协议、中转和代理均未改变。

- 前端 29 项测试覆盖以下内容：
  - SGR 16/256/真彩色（分号与冒号形式）、跨行样式继承、样式变化触发重绘；
  - 增量解析与全量解析结果一致；
  - 顶部滚出（包括新顶行与旧顶行相同的情况）、单行匹配和重复空行不误判；
  - emoji 序列宽度，含 VS15/VS16、肤色、国旗和 ZWJ。
- 宽度表由 `kitty +launch scripts/gen_widths.py` 从 Kitty 0.49.0 的 `wcswidth` 生成（`web/src/widths.ts`）。另用 20,000 个随机场景 × 10 次更新做模糊测试，未发现增量结果或补丁不一致。
- 本地和公网 `scripts/e2e.py` 的新增项全部通过：`native_touch_scroll`、`reading_position_kept_on_output`、`font_preserves_reading_anchor`、`reading_position_kept_on_reconnect`、`auto_follow_at_bottom`、`main_screen_redraw_while_scrolling`、`alternate_redraw_while_scrolling`。退出备用屏幕后会回到主屏幕最新内容。持续刷新测试中，主屏幕逐帧改写最后一行、备用屏幕每 80 ms 整屏重画，同时用逐帧触摸拖动滑动：阅读行不跳动，视口内始终有文字行，浏览器错误为空。
- Chromium 降速 4 倍模拟手机：10,000 行历史中改动一行的更新约 90 ms。同一视图未做增量解析的初版约 900 ms，旧 xterm 版本未测。2,000 行满回滚滚出顶部行约 14–30 ms，2,044 行首次加载约 1.4–1.6 s。
- 无头 Chromium 不执行 touch 类型的 `synthesizeScrollGesture`；最小页面对照已确认这一点，因此验收改用逐帧 `dispatchTouchEvent` 拖动。
- 实测本机 Claude Code 使用 `"tui": "fullscreen"`，处于备用屏幕，Kitty 可读内容只有一屏（46 行）。更早的对话由 Claude 自身保存，需要用翻页键滚动。
- 代码审查发现并已修复 5 个问题：新顶行重复时顶部滚出漏判，导致整段重建并跳行；重连或重新同步时只含当前屏的快照覆盖历史，丢失阅读位置；退出备用屏幕落到历史首行；内容缩短后不再跟随；带 VS16 的 emoji 宽度与 Kitty 不一致。每项均在 Chromium 中复验。
- 公网（2026-09-26 17:40 前后）：中转镜像 `sha256:0c31734de608…`，状态 healthy，重启次数 0；网页资源为 `index-CFeV2aNc.js`、`index-DyQnoWKr.css`，与本地构建一致。`public_network_tested=true`，`browser_errors=[]`，`android_device_tested=false`。测试设备已撤销；生产代理自动重连，没有重启。
- 公网第一次运行时，全部功能检查和撤销都已通过，但在最后一步点“退出”时 30 秒内找不到按钮。带诊断重跑后完整通过，原因尚未查明，暂不归因为产品缺陷。
- 手机“滑动时黑屏”未在真机复现，本次按最可能的原因处理：旧版每次更新都会清屏重写，新版不再清屏。仍需真机观察确认。

## 真实完整链路

`scripts/e2e.py` 启动独立 Kitty 实例和两个专用标签页，并使用临时数据库、设备凭据、中转和代理；与用户已有 Kitty 实例隔离。输入接收程序只记录原始字节和绘制测试画面，不执行接收到的 shell 命令。

已通过：

1. 浏览器登录、输入配对码、查看设备名称、确认配对及代理上线。
2. 前台/后台标签页文字读取，中文多行与 Emoji 原样传入。
3. 模拟组合输入期间按钮禁用；发送文字不附带回车，Enter 单独发送。
4. 相同请求重复到达只执行一次，同 ID 不同内容拒绝。
5. Ctrl+C、文字后回车、备用屏幕进入/退出；可访问屏幕列表确认旧画面被替换。
6. 暂停跟随、回到最新及历史读取。
7. 切换窗口后输入只出现在所选窗口。
8. 第二个浏览器只能只读，不能抢占已持有的控制权。
9. 浏览器断线后重连，重新订阅完整快照，原输入没有重复执行。
10. 撤销设备立即移除窗口，登出返回登录页。
11. 浏览器未出现 JavaScript 异常，手机模拟视口无页面级横向溢出；终端自身保留横向滚动。

结果与界面截图在 `artifacts/e2e-results.json`、`artifacts/console-mobile.png`、`artifacts/console-desktop.png`、`artifacts/login-mobile.png`、`artifacts/login-desktop.png`。这些属于可重新生成的本地验收产物，未纳入源码。

## 公网部署验收（2026-09-24）

- 服务器：自有云服务器，Docker 26.1.4 / Compose 2.27.1；与已有 HTTPS 反向代理共用 80/443。
- HTTPS：Caddy 2.11.4 成功签发 Let's Encrypt IP 证书；外部 curl TLS 验证结果为 0，页面返回 200。
- `kitty-remote-relay-1` 为 running / healthy，仅绑定 `127.0.0.1:18765`。
- 初次公网输入暴露约 34 秒的跨机器时钟差，修复中转时间握手后，完整链路全部通过。
- 公网验收同样使用独立测试 Kitty；结果与截图在 `artifacts/public/`，`public_network_tested=true`，`android_device_tested=false`，浏览器异常为空。
- 生产设备在线，发现 5 个已有窗口；实际窗口未注入测试文字。用户服务 active/running，检查时 `NRestarts=0`。
- 同机已有站点访问正常；修改前已备份既有 Caddy 配置。

```bash
KR_E2E_ADMIN_FILE=.state/server-admin.json uv run python scripts/e2e.py
```

## 公网更新验收（2026-09-25）

- 网页与中转已部署手机版更新，随后再次更新固定布局网页；当前公网资源为 `index-CJUoyMKZ.js`、`index-rImakNzb.css`，中转镜像为 `sha256:f40f3cd95f850e567d117929d675f0aa290913cf3f6066be0995e0536c8ed74b`。
- 中转 `healthy`、重启次数 0；本机代理在中转切换后自动重连，未因本次网页布局更新而重启。
- 公网独立 Kitty 验收最终通过：`pinned_screen_height_px=547`、`public_network_tested=true`、`browser_errors=[]`；发送清空、模式切换与草稿保留、历史滑动、扩展按键、窗口隔离、重连和测试设备撤销均通过。
- 首次公网重跑在备用屏幕的 5 秒页面断言处超时，随后重跑通过；尚未将这一次超时归因为产品缺陷。安卓真机仍未验证。

此命令使用私有管理员文件，创建并最终撤销测试设备；测试密码和令牌不进入结果文件。

## 尚未验证

| 项目 | 当前边界与后续验收 |
| --- | --- |
| Codex / Claude Code | 已确认工具版本；实际长回复、审批选项、组合键、150 ms 回车间隔及复杂 Unicode 布局，需在两者专用会话逐项验收 |
| 安卓真机 | 模拟手机尺寸不等于真实安卓；检查输入法组合、语音转文字、粘贴、横竖屏、锁屏返回 |
| 移动公网场景 | HTTPS/WSS 完整链路已验证；流量/Wi-Fi 切换、慢网络背压，以及真机刷新与输入回显延迟待验证 |

## 性能与能力记录

历史 0.48.2 单窗口 get-text 五次约 16～18 ms。升级至 0.49.0 后，前台/后台两个窗口的单次 get-text 分别约 17.18 / 15.98 ms；这些仅是 Kitty 调用耗时，真实代理另有身份校验、网络和网页渲染开销，不构成手机延迟保证。

0.49.0 原生截图也曾只读验证：可见窗口返回 1920×902 PNG，约 230 KB、240 ms；后台标签页明确拒绝。用户已选择首版保持文字模式，因此没有加入截图传输接口。

## 复现

项目根目录运行 README 中的检查命令。真实链路测试需要桌面与 Kitty；`uv run python scripts/e2e.py` 会清理自己创建的临时进程和凭据，不启用常驻服务。仅在自己控制的本机环境运行该验收脚本。
