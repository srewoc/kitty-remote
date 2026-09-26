# 协议 v1

同源 HTTPS + WSS；代理使用 Bearer 设备凭据。每条 WebSocket JSON 消息含 `v:1` 和 `type`，编码后最多 1 MiB。

## HTTP

| 路径 | 方法 | 输入 / 结果 |
| --- | --- | --- |
| `/api/health` | GET | 健康和版本 |
| `/api/auth/login` | POST | `{username,password}` → 会话 Cookie |
| `/api/auth/me` | GET | 校验会话 |
| `/api/auth/logout` | POST | 失效会话并关闭其连接 |
| `/api/pairings/start` | POST | `{name}` → `{pairing_id,code,secret,expires_at}` |
| `/api/pairings/lookup` | POST | `{code}` → 待配对电脑信息 |
| `/api/pairings/approve` | POST | `{code,pairing_id}` → 确认配对 |
| `/api/pairings/claim` | POST | `{pairing_id,secret}` → `{device_id,token}` 或 `{pending:true}` |
| `/api/devices` | GET | 设备和窗口状态 |
| `/api/devices/{id}/revoke` | POST | 撤销设备凭据和连接 |

浏览器写操作要求 Origin 匹配配置。机器配对 start/claim 是限流匿名接口，claim 依赖高熵临时秘密。其余管理接口要求已登录。

## 身份与消息

`target` 含 UUID 字符串 `device_id,agent_epoch,kitty_instance_id` 和正整数 `window_id`。实例绑定 Unix Socket inode、变化时间、对端 PID 和进程启动时间。窗口绑定创建时间、PID 和随机用户变量 `kitty_remote_token`。标记丢失或 ID 复用会改变实例身份，拒绝旧目标。

| 方向 | type | 主要字段 |
| --- | --- | --- |
| 代理 → 中转 | `hello`, `windows` | `agent_epoch,windows` |
| 中转 → 浏览器 | `state` | `server_time,devices:[{id,name,online,windows}]` |
| 中转 → 代理 | `welcome` | `server_time`，必须在代理 `hello` 后首先返回 |
| 浏览器 → 代理 | `subscribe`, `unsubscribe`, `history` | `target`；浏览器 unsubscribe 可省略 |
| 代理 → 浏览器 | `snapshot`, `history` | `target,seq,sampled_at,rows,cols,alternate,content` |
| 浏览器 → 中转 | `claim` | 当前订阅申请控制权 |
| 中转 → 浏览器 | `lease` | `target,owned,available,expires_at` |
| 浏览器 → 代理 | `input` | `request_id,target,op,text,keys,expires_at` |
| 中转 / 代理 → 浏览器 | `receipt` | `request_id,target,status,detail` |
| 浏览器 → 代理 | `create_window` | `request_id,target,expires_at`；目标为当前受控窗口 |
| 中转 / 代理 → 浏览器 | `create_window_result` | `request_id,target,status,detail`；成功时附 `window` |
| 代理 → 浏览器 | `target_error`, `history_error` | `target,detail` |
| 中转 → 浏览器 | `error` | `detail` |
| 双向 | `ping`, `pong` | 心跳 |

连续历史扩展（2026-09-25）：

| 方向 | type | 字段与行为 |
| --- | --- | --- |
| 浏览器 → 代理，经中转 | `subscribe` | 增加 `buffer:true`，自动同步整个可读缓冲；普通快照继续作为初始画面和失败回退 |
| 代理 → 中转 → 浏览器 | `buffer_start` | `target,seq,base_seq,sampled_at,rows,cols,alternate,start,delete_count,insert_count,bytes` |
| 代理 → 中转 → 浏览器 | `buffer_chunk` | `target,seq,index,content`；index 从 0 开始连续递增 |
| 代理 → 中转 → 浏览器 | `buffer_end` | `target,seq,chunks`；收到全部字节及块后才原子应用 |
| 浏览器 → 中转 → 代理 | `buffer_resync` | 当前订阅重新全量同步；不重放输入 |

`base_seq=0` 表示全量替换。其他更新以基础版本的 `content.split('\n')` 为行数组，从 `start` 删除 `delete_count` 行，插入拼接各块后得到的 `insert_count` 行；零行插入与一行空字符串严格区分。前后公共行比较只决定连续替换范围，不猜测终端新增输出。版本不匹配时重新同步。中转与浏览器分别检查版本、范围、块序、字节数及最终 16 MiB 上限。

代理约每秒读取一次订阅窗口的完整缓冲（空闲时随原采样退避至约两秒），内容或尺寸变化才发送；传输发送相对上次已发送完整版本的差量。每一跳各自维护发送基础版本，新的浏览器总是先得到完整内容。每个发送队列保留当前传输和最新待同步缓冲，不积累中间版本；回执等控制消息在块之间优先发送。单块正文按最多 96 Ki 字符划分，包含 JSON 转义后仍小于 1 MiB。切换目标时取消旧传输及缓存。

代理/浏览器之间都经中转鉴权转发。Window 字段为 `target,title,cwd,os_window_id,tab_id,rows,cols,alternate`，不转发环境变量、完整命令行或前台进程信息。

输入的 `op` 为 `text`、`text_enter` 或 `keys`。`request_id` 使用 UUID 字符串；`expires_at` 为中转时间基准的 Unix 秒。浏览器以 `state.server_time` 加单调时钟增量产生截止时间，最多 10 秒；代理通过 `hello/welcome` 往返测量将其转换成本机时间，采用整个往返耗时保守估计，宁可提前拒绝也不延长有效期。代理握手超过 10 秒拒绝继续；缺少时间握手时不接受输入。升级时中转、网页和代理需一起更新。按键一次最多 16 个，逐个调用 Kitty 保持顺序。

创建窗口扩展：`create_window` 与输入使用相同的目标身份、控制权、到期时间和去重机制。中转每个设备同时最多转发一个创建请求，并限制频率；代理只对当前已验证 Kitty 实例调用 `launch --type=os-window`，以本机用户的 `~` 为工作目录、不指定启动命令，因此运行默认 Shell。`create_window_result.status` 为 `received`、`created`、`rejected` 或 `uncertain`；仅 `created` 携带经新窗口列表确认的完整 `window`。连接中断或结果不确定时不自动重试。网页在当前窗口仍被选中时自动切换到新窗口。须先选中已有窗口，才能确定要使用的设备与 Kitty 实例。

按键白名单：`enter,esc,tab,up,down,left,right,ctrl+c,shift+tab,backspace,home,end,page_up,page_down,insert,delete,shift+enter,ctrl+enter,alt+b,alt+f,alt+backspace`，以及 `ctrl+a/e/u/k/w/r/l/d/z`、`f1` 至 `f12`。后端以 `protocol.KEYS` 校验，前端分组定义在 `web/src/keys.ts`。

回执状态为 `received`、`submitted`、`rejected`、`failed`、`uncertain`；前端另有本地 `pending`。`submitted` 仅表示 Kitty 控制调用返回，无法证明目标应用处理了输入。

相同 ID、相同负载返回已记录回执；同 ID 不同内容拒绝。去重记录有界且不跨进程崩溃持久化。连接或回执丢失时不自动重发。

## 显示与背压

快照 seq 在代理周期内递增。浏览器只接受当前完整目标的新序号，不能将快照不断追加。连续历史以 Kitty 当前完整缓冲为真值；列数保留电脑布局，显示行数按手机可视高度调整，末尾空白屏幕行不挤走最后一段有效输出。历史准备好后不再用仅含当前屏幕的快照覆盖历史；读取失败时回退到实时快照。网页把完整内容渲染为固定行高的原生滚动行，每次更新只替换变化的行（识别顶部滚出的行），不暂停、不整页重画；位于底部时跟随，否则保持阅读行不动（2026-09-26 起）。旧 `history` 按需消息仅为兼容保留，仍受旧单帧限制，新网页不再使用。

每个订阅仅保留最新待发快照，控制回执使用独立有界队列。慢连接的控制队列溢出会关闭连接，未确认输入转为结果不确定。

代理与网页双重过滤 ANSI，仅允许展示样式、光标定位、样式和可见性。OSC（含链接、剪贴板）、DCS、查询等序列被去除；文字不会作为 HTML 执行。
