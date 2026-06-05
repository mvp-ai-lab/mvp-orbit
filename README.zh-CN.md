<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="./assets/logo-light.png">
    <img alt="mvp-orbit" src="./assets/logo-dark.png" width="560">
  </picture>
</p>

<div align="center">by MVP Lab.</div>
## Orbit 是什么

`mvp-orbit` 是一个纯 HTTP 的 peer 命令 channel。

一台机器运行控制 `host`。每台 `client` 加入某个命名 channel 时只需要提供：

- 本机别名
- host 地址
- channel 名称

第一个加入 channel 的 client 会自动通过。后续 client 会创建 pending join request，任意已经加入该 channel 的 client 都可以批准或拒绝。审批通过后，同一 channel 的成员可以通过 host 发送命令、打开 shell、传输文件。client 之间不需要彼此直连。

```mermaid
flowchart LR
    H["控制 host\nHTTP + SSE 转发"]
    A["client-a"]
    B["client-b"]
    C["client-c"]

    A <-->|"加入审批\n命令 / shell / 文件"| H
    B <-->|"加入审批\n命令 / shell / 文件"| H
    C <-->|"加入审批\n命令 / shell / 文件"| H
```

## 安全模型

Channel 成员关系就是信任边界。

- 第一个 client 创建 channel，并获得 member token。
- 后续 client 必须被已有成员审批后才能加入。
- member token 在过期前可访问该 channel。
- 任意已批准成员都可以对任意在线成员执行命令。

这不是沙箱。只应该批准可信 client，也只应该在可信 channel 里执行命令。

## 命令

公开 CLI 命令刻意保持很小：

```bash
orbit host
orbit join
orbit join-requests
orbit approve <REQUEST_ID>
orbit reject <REQUEST_ID>
orbit peers
orbit exec <peer> -- <command>
orbit sh <peer>
orbit put <peer> <local> <remote>
orbit get <peer> <remote> <local>
```

`exec`、`sh`、`put/get` 是仅有的三类 peer 操作模式。

## 快速开始

### 1. 启动 Host

```bash
orbit host
```

host 使用 SQLite 保存 channel 状态，并通过 HTTP/SSE 转发事件。默认监听 `127.0.0.1:8080`；如果需要对网络开放，设置 `ORBIT_HUB_HOST=0.0.0.0`。

### 2. 第一台 Client 加入

```bash
orbit join --host http://HOST:8080 --alias client-a --channel team-a
```

`orbit join` 是前台常驻进程。加入成功后，它会启动 client loop，持续接收命令、shell、文件请求和加入审批。如果进程退出，这台 client 就不能再接收任务。

只想保存配置、不启动 client loop 时使用 `--no-start`：

```bash
orbit join --host http://HOST:8080 --alias client-a --channel team-a --no-start
```

### 3. 更多 Client 加入

另一台机器执行：

```bash
orbit join --host http://HOST:8080 --alias client-b --channel team-a
```

新 client 会等待审批。同一 channel 内任意正在前台运行的 `orbit join` 进程会直接弹出确认：

```text
[orbit] new client join request
  alias: client-b
  channel: channel-...
  request: join-...
[orbit] approve this client? [y/N]:
```

如果没有交互式终端里的 client 进程，可以从任意已有成员手动审批：

```bash
orbit join-requests
orbit approve <REQUEST_ID>
```

拒绝请求使用 `orbit reject <REQUEST_ID>`。如果新 client 只想提交申请后立即退出，可以加 `--no-wait`。

### 4. 查看 Peers

```bash
orbit peers
```

### 5. 执行单条命令

```bash
orbit exec client-b -- uname -a
```

需要 shell 操作符、变量、管道或 `cd` 时使用 `--shell`：

```bash
orbit exec client-b --shell "cd /tmp && pwd && ls -la"
```

命令在目标 client 的 workspace 内执行。`--working-dir` 必须保持在该 workspace 内。

### 6. 打开交互式 Shell

```bash
orbit sh client-b
```

### 7. 传输文件

发送本地文件到对端：

```bash
orbit put client-b ./local.txt inbox/local.txt
```

从对端下载文件：

```bash
orbit get client-b inbox/local.txt ./downloaded.txt
```

默认大小限制是 `1 MiB`。需要更大文件时显式提高：

```bash
orbit put --max-bytes 10485760 client-b ./model.bin models/model.bin
orbit get --max-bytes 10485760 client-b models/model.bin ./model.bin
```

相对 remote path 会解析到目标 client 的 workspace 下。绝对 remote path 也允许，但应谨慎使用。

## 配置

默认配置文件是：

```text
~/.config/mvp-orbit/config.toml
```

`orbit join` 会写入 host URL、本机 client 别名、member token 和 token 过期时间。非 join 命令会自动读取这个配置。也可以用 `--hub-url`、`--member-token`、`--token-expires-at` 覆盖。

常用运行环境变量：

```bash
ORBIT_CONFIG=~/.config/mvp-orbit/config.toml
ORBIT_WORKSPACE_ROOT=/path/to/workspace
ORBIT_HEARTBEAT_SEC=15
ORBIT_LOG_LEVEL=INFO      # DEBUG, INFO, WARNING, ERROR
NO_COLOR=1               # 禁用 ANSI 颜色
```

Host 环境变量：

```bash
ORBIT_HUB_HOST=127.0.0.1
ORBIT_HUB_PORT=8080
ORBIT_HUB_DB=./.orbit-hub/hub.sqlite3
ORBIT_OBJECT_ROOT=./.orbit-hub/objects
ORBIT_ACCESS_LOG=0        # 设为 1 可打开 uvicorn HTTP access log
```

## 空 Channel 自动清理

host 会自动删除没有在线 client 的 channel。`orbit join` 运行时会发送 heartbeat。若某个 channel 在 `ORBIT_CLIENT_OFFLINE_SEC` 内没有任何 client 更新在线状态，并且超过 `ORBIT_CHANNEL_EMPTY_TTL_SEC` 没有活动，就会被删除。

默认值：

```bash
ORBIT_CHANNEL_CLEANUP_ENABLED=1
ORBIT_CLIENT_OFFLINE_SEC=90
ORBIT_CHANNEL_EMPTY_TTL_SEC=3600
ORBIT_CHANNEL_CLEANUP_INTERVAL_SEC=60
```

删除 channel 会同时清理该 channel 的已批准成员、待审批请求、过期 client 记录、token、命令历史、shell 历史和文件传输历史。

## 日志

运行时日志使用紧凑的结构化单行格式：

```text
[15:52:34] INFO    client │ client.runtime     │ command.start client_id=client-a argv="python3 -V"
```

消息部分使用 `event key=value`，方便搜索和解析。

## Docker Host

Dockerfile 运行的是 host：

```bash
docker build -t mvp-orbit .
docker run --rm -p 8080:8080 -v orbit-data:/var/lib/orbit mvp-orbit
```

镜像默认设置 `ORBIT_HUB_HOST=0.0.0.0`，并把状态保存在 `/var/lib/orbit`。

## 网络模型

只需要满足：

- 每台 client 可以通过 HTTP 或 HTTPS 访问控制 host
- host 不需要主动连接 client
- client 之间不需要直接互通

实时链路使用 `host -> client` 的 SSE，以及 `client -> host` 的 HTTP POST。
