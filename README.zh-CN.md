<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="./assets/logo-light.png">
    <img alt="mvp-orbit" src="./assets/logo-dark.png" width="560">
  </picture>
</p>

<div align="center">by MVP Lab.</div>

## 为什么是 Orbit

`mvp-orbit` 是为一种非常具体的工作流设计的：

1. 在一台机器上准备代码
2. 把它发送到目标机器
3. 在目标机器上执行命令
4. 立刻把输出流式回传
5. 尤其适合因为某些原因不能使用 SSH，或者想要一种比手工复制粘贴更可重复的方式

这让它非常适合：

- 需要远程执行能力的 AI coding agent
- GPU / NPU / 嵌入式设备调试闭环
- 一台机器构建，另一台机器运行的工作流

## 产品模型

Orbit 围绕三个面向用户的动作构建：

- `package`
  把目录打成确定性的 `.tar.gz` 并上传到 Hub。
- `command exec`
  把一条命令发送给指定 Agent。`package_id` 可选。
- `shell`
  打开一个持久的远程 shell，会话支持重连。

关键运行语义：

- Agent 启动目录就是基础工作区。
- 不带 `package_id` 的命令直接在基础工作区执行。
- 带 `package_id` 的命令会在基础工作区下对应的 package 子目录执行。
- shell 默认在基础工作区启动；如果提供了 `package_id`，则在对应 package 工作区启动。
- Hub、CLI 和 Agent 之间只通过 HTTP + Bearer token 通信。

## 快速开始

### 1. 初始化 Hub

```bash
orbit init hub
orbit hub serve
```

`orbit init hub` 会输出：

- Hub URL
- API token
- `ORBIT_NODE_SHARED_CONFIG`

### 2. 初始化节点 / Agent

```bash
orbit init node --agent-id agent-a
```

或者直接使用 Hub 生成的共享串完成初始化：

```bash
orbit init node --agent-id agent-a --shared-config "$ORBIT_NODE_SHARED_CONFIG"
```

然后启动 Agent：

```bash
orbit agent run
```

## 核心流程

### 上传文件包

```bash
orbit package upload --source-dir /path/to/project
```

示例返回：

```json
{
  "package_id": "sha256-...",
  "size": 12345,
  "created_at": "2026-03-10T00:00:00+00:00"
}
```

### 在 package 上执行命令

```bash
orbit command exec \
  --agent-id agent-a \
  --package-id <PACKAGE_ID> \
  python3 train.py --epochs 1
```

### 直接在 Agent 基础工作区执行命令

```bash
orbit command exec \
  --agent-id agent-a \
  bash -lc 'pwd && ls'
```

### 执行复合 shell 命令

```bash
orbit command exec \
  --agent-id agent-a \
  --shell \
  "cd /cache/models/ && HF_TOKEN=hf_xxx hf download repo --local-dir model-dir"
```

说明：

- 如果远端命令里包含 `cd`、`&&`、管道、重定向或内联环境变量，请把整条命令作为一个带引号的字符串传入。
- 不带引号的 `&&` 会先被你本地 shell 吃掉，所以后半段会在本机执行。

### 只提交，不等待

```bash
orbit command exec \
  --agent-id agent-a \
  --package-id <PACKAGE_ID> \
  --detach \
  python3 train.py
```

之后再查看：

```bash
orbit command status --command-id <COMMAND_ID>
orbit command output --command-id <COMMAND_ID>
orbit command output --command-id <COMMAND_ID> --follow
orbit command cancel --command-id <COMMAND_ID>
```

### 打开远程 shell

基础工作区：

```bash
orbit shell start --agent-id agent-a
```

package 工作区：

```bash
orbit shell start --agent-id agent-a --package-id <PACKAGE_ID>
```

重连或关闭：

```bash
orbit shell list
orbit shell list --agent-id agent-a
orbit shell attach --session-id <SESSION_ID>
orbit shell close --session-id <SESSION_ID>
```

本地 attach 时：

- `/detach` 会保留远端 shell，只断开当前本地连接
- `/close` 会关闭远端 shell
- `shell start` 在 attach 前会先打印新的 `session_id`，方便后续重连或关闭

## 配置

默认配置文件路径：

```text
~/.config/mvp-orbit/config.toml
```

当前配置结构：

```toml
[hub]
host = "127.0.0.1"
port = 8080
db = "./.orbit-hub/hub.sqlite3"
object_root = "./.orbit-hub/objects"
url = "http://127.0.0.1:8080"

[auth]
api_token = "..."

[agent]
id = "agent-a"
workspace_root = "./workspace"
poll_interval_sec = 5.0
heartbeat_interval_sec = 5.0
```
