# 部署、服务安装与停用

部署后需完成 [公网与安卓验收](verification.md)。

## 公网中转

需要 Docker Compose、解析到服务器的域名、80/443 入站访问。Ubuntu 代理主动连接服务器，Kitty Socket 不离开本机。

在服务器项目根目录执行：

```bash
cp deploy/env.example deploy/.env
# 编辑 deploy/.env 中的 DOMAIN

docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm relay create-admin
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

创建管理员时交互输入密码。Caddy 自动管理 TLS，并转发 HTTP/WebSocket；中转容器只向内部网络暴露 8765，使用非 root 用户，数据库保存到 `relay-data` 卷的 `/data/relay.sqlite3`。

`KR_ORIGIN` 必须与浏览器入口完全一致，例如 `https://terminal.example.com`，末尾没有 `/`。生产环境保持 `KR_DEV=0`。浏览器 Cookie 带 Secure、HttpOnly、SameSite=Strict。

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail 50 relay caddy
```

`GET /api/health` 返回进程健康和协议版本。日志不记录输入、屏幕或明文令牌。关注健康检查、进程重启、设备在线状态和回执超时。实际公网延迟在手机上测量。

## 服务器已有 HTTPS 代理

当 80/443 已被其他服务使用时，使用 `deploy/compose.existing-proxy.yaml` 替代上面的 `deploy/compose.yaml`，并在所有命令中固定加上 `-p kitty-remote`，保持同一个项目名和数据库卷。此模板只运行中转，监听 `127.0.0.1:18765`。

在现有代理配置中添加独立站点（域名必须与 `DOMAIN` 相同）：

```caddyfile
terminal.example.com {
    request_body {
        max_size 8KB
    }
    reverse_proxy 127.0.0.1:18765
}
```

该上游地址适用于宿主机代理或 host 网络容器。其他容器网络需要单独配置可达地址。修改现有代理前备份配置，并校验完整配置；按现有代理的管理方式应用变更。不要启动基础模板中的第二个 Caddy 争用 80/443。

## 不使用域名：公网 IP HTTPS

公网 IP 也可以申请可信证书。现有代理部署可设置 `DOMAIN=203.0.113.10`（换成你的公网 IP），使用 `compose.existing-proxy.yaml`，将 `deploy/Caddyfile.ip` 中的独立站点合并到现有 Caddy 配置（替换 `{$DOMAIN}` 为实际 IP）。在现有全局块中设置 `default_sni 203.0.113.10`，以支持访问 IP 时不发送 SNI 的客户端。

此方案已在 Caddy 2.11.4 验证。Let's Encrypt IP 证书采用 `shortlived` profile，由 Caddy 自动续签；HTTP-01 校验需要公网 80 端口持续可达。无需自签证书或关闭客户端 TLS 校验。不要覆盖其他站点配置。

官方资料：[IP 证书](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)、[Caddy ACME issuer](https://caddyserver.com/docs/caddyfile/directives/tls#issuers)。

## 设备配对

在 Ubuntu 项目根目录运行：

```bash
uv run kitty-remote pair --relay https://terminal.example.com --name '我的 Ubuntu'
```

手机打开相同 HTTPS 地址，登录后输入配对码并确认设备名称。之后执行 `uv run kitty-remote agent`。中转仅存令牌摘要，明文凭据保存在本机 `0600` 文件中。

## systemd 用户服务

先手动验证代理，再安装服务。同一凭据文件的代理通过文件锁限制为一个进程。

1. 复制 `deploy/kitty-remote-agent.service` 到 `~/.config/systemd/user/kitty-remote-agent.service`。
2. 将其中两个 `/REPLACE/ABSOLUTE/PROJECT/PATH` 改为项目绝对路径，`ExecStart` 保留可执行路径外的引号；`WorkingDirectory` 不加引号，整行值可以包含空格。
3. 如需要，在 `ExecStart` 后追加 `--credentials`、`--socket` 或 `--socket-glob`。模板 PATH 包含 `~/.local/bin`；Kitty 安装在其他自定义目录时，将该目录加入模板 PATH。
4. 若 Socket 位于 `/tmp`，需移除模板的 `PrivateTmp=true`，否则服务无法看到桌面的临时目录 Socket。

```bash
systemctl --user daemon-reload
systemctl --user enable --now kitty-remote-agent.service
systemctl --user status kitty-remote-agent.service
journalctl --user -u kitty-remote-agent.service --since '10 minutes ago'
```

代理不需要 root。注销、休眠、重启或 Kitty 退出后，任务能否存活由桌面和原程序决定，本项目不提供进程持久化。

## 配置与限额

| 项目 | 默认值或说明 |
| --- | --- |
| `KR_DATABASE` | `.state/relay.sqlite3`，相对工作目录 |
| `KR_ORIGIN` | `http://127.0.0.1:8765`，此默认值须搭配 `KR_DEV=1` |
| `KR_DEV` | `0`；开发 HTTP 只允许 localhost |
| `KR_WEB_DIR` | `web/dist` |
| 设备凭据 | `~/.config/kitty-remote/agent.json`，可用 `--credentials` 指定 |
| 配对码 / 会话 / 控制权租约 | 5 分钟 / 12 小时 / 30 秒，活跃连接心跳续约 |
| 输入有效期 | 收到后最多 10 秒 |
| 去重记录 | 内存中最多 4,096 条、保留 60 秒，重启不恢复 |
| 文字输入 | UTF-8 64 KiB |
| 连续历史 | UTF-8 16 MiB，无固定行数上限；自动分块，每条消息不超过 1 MiB |
| WebSocket | 编码后 1 MiB |

中转必须单进程；连接、租约、速率限制和回执路由在内存中维护，不要增加 Uvicorn worker 或副本数。

## 更新、备份与卸载

更新前停止代理，更新代码后运行 `uv sync --locked`、`npm ci --prefix web`、`npm run build --prefix web`。重启中转或代理会断开连接，旧输入不会补发。

版本更新时的兼容性要求：

- 2026-09-25 连续历史：扩展了协议 v1，网页、中转、代理必须使用同一版本。中转 Docker 镜像已包含网页构建，需要重建镜像并更新中转容器；本机执行 `uv sync --locked` 后重启 `kitty-remote-agent.service`。只改网页布局的更新，只需重建中转镜像，不用重启本机代理。
- 2026-09-26 新建桌面窗口：新增 `create_window` 协议消息，网页、中转、代理要一起更新。新窗口从代理所属本机用户的 `~` 启动默认 Shell。
- 2026-09-26 原生滚动终端视图：只改网页（React 组件、样式、依赖），协议、中转逻辑和代理都没变。重建中转镜像并执行 `up -d --no-build` 即可；本机代理不用重启，断线后会自动重连。

Kitty 本身不需要重启，现有任务会保留。账号数据库卷和设备凭据可以继续沿用。回滚涉及协议变更的版本时，网页、中转、代理要作为一组一起还原。建议升级前先给旧镜像打标签，例如 `docker tag kitty-remote:0.1.0 kitty-remote:before-upgrade`；回滚时把旧标签重新标记为 `kitty-remote:0.1.0`，再执行 `up -d --no-build`。

停用时先在网页撤销设备，再停止服务：

```bash
systemctl --user disable --now kitty-remote-agent.service
```

确认停用后可删除本项目用户服务文件和本机凭据，再执行 `systemctl --user daemon-reload`。保留其他项目的 Kitty 配置及 Socket。

中转通过 `docker compose --env-file deploy/.env -f deploy/compose.yaml down` 停止，默认保留数据库卷。删除卷会删除账号和授权，只在明确需要清空时执行。

备份数据库前停止中转，或使用 SQLite 在线备份接口。凭据文件属于敏感配置，不加入 Git。

## 构建镜像源（可选）

服务器访问 PyPI 较慢时，可以在构建时指定镜像：

```bash
docker build --build-arg PIP_INDEX_URL=https://<你的 PyPI 镜像>/simple -f deploy/Dockerfile -t kitty-remote:0.1.0 .
docker compose -p kitty-remote --env-file deploy/.env -f deploy/compose.existing-proxy.yaml up -d --no-build
```

默认仍使用 PyPI。运行依赖由 uv 从锁文件导出，带固定版本和哈希，安装时强制校验，镜像无法替换依赖内容。
