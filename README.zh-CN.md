# mvp-orbit

`mvp-orbit` 是一个轻量级远程调试运行时，包含两个平面：

- 内容平面：所有敏感对象都存放在 GitHub Release Assets 中。
- 控制平面：Hub API 只保存运行元数据和对象 ID。

运行时围绕三个对象构建：

- `file_package`：从目录中筛选文件后打成的 `.tar.gz` 包。
- `command`：结构化执行 JSON，包含 `argv`、`env_patch`、`timeout_sec`、`working_dir`。
- `task`：对 `package_id + command_id + constraints` 进行签名绑定后的对象。

Hub 不接收文件包、命令 JSON、task 内容、日志正文或结果正文。Agent 只从 Hub 获取 ID，然后再去 GitHub 拉取真实对象并在本地校验。

## 安全模型

- `package_id = sha256(package_bytes)`
- `command_id = sha256(canonical_json(command))`
- `task_id = sha256(canonical_json(task))`
- `task_signature = Ed25519(task canonical bytes)`
- `run_ticket` 使用 HMAC 保护，带短时效，并通过 nonce 防止重放

Agent 执行前会按顺序校验：

1. `run_ticket` 是否有效且未重放
2. `task_id` 是否与内容哈希一致
3. `task_signature` 是否验证通过
4. `task.package_id / task.command_id` 是否与 Hub 下发的 lease ID 一致
5. 从 GitHub 下载的 `package` 和 `command` 是否与对应 ID 一致

GitHub 访问通过 `gh` CLI 完成。`mvp-orbit` 默认假设当前机器已经执行过 `gh auth login`。

## 存储模型

v1 仅支持 GitHub。对象统一存放在独立 relay 仓库的 GitHub Release Assets 中。

- `package/<package_id>.tar.gz`
- `command/<command_id>.json`
- `task/<task_id>.json`
- `log/<log_id>.json`
- `result/<result_id>.json`

存储层通过 `ObjectStoreBackend` 抽象，后续扩展到 S3、Hugging Face 等后端时，不需要改执行链路。

## 快速开始

### 1) 前置条件

- Python 3.11+
- GitHub CLI (`gh`)
- 一个私有 GitHub relay 仓库
- Hub/开发机与 Agent 机器上都已经完成 `gh auth login`
- 如需代理，可设置 `HTTPS_PROXY`

当前版本通过 `gh` CLI 完成 GitHub 存储读写，不再需要给 `mvp-orbit` 传入 `ORBIT_GITHUB_TOKEN`。

### 2) 生成 task 签名密钥对

```bash
orbit keys generate
```

### 3) 准备 Hub secrets

你可以手动提供：

```bash
export ORBIT_TICKET_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export ORBIT_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

如果不提供，`orbit hub serve` 会在启动时自动生成这两个值，并打印到标准输出。

规则：

- `ORBIT_TICKET_SECRET` 在 Hub 和 Agent 上必须完全一致。
- `ORBIT_API_TOKEN` 在 Hub 和所有 Hub 客户端上必须完全一致。

### 4) 导出 GitHub relay 配置

```bash
export ORBIT_GITHUB_OWNER="<owner>"
export ORBIT_GITHUB_REPO="<relay-repo>"
export ORBIT_GITHUB_RELEASE_PREFIX="mvp-orbit"
```

### 5) 启动 Hub

```bash
export ORBIT_HUB_HOST="127.0.0.1"
export ORBIT_HUB_PORT="8080"
export ORBIT_HUB_DB="./.orbit-hub/runs.sqlite3"

orbit hub serve
```

如果缺少 `ORBIT_TICKET_SECRET` 或 `ORBIT_API_TOKEN`，Hub 会打印类似下面的内容：

```text
ORBIT_TICKET_SECRET=...
ORBIT_API_TOKEN=...
Generated missing Hub secrets for this process. Export the values above if other processes must reuse them.
```

之后需要把这些相同的值提供给 Agent 和 CLI 客户端。

### 6) 上传文件包

`orbit package upload` 具备 Git 感知能力。如果源目录位于 Git 仓库中，它会使用：

- `git ls-files --cached --others --exclude-standard`

这意味着 `.gitignore` 会生效。选中的文件会被打成确定性的 `.tar.gz`，所以相同内容会得到相同的 `package_id`。

```bash
orbit package upload \
  --source-dir /path/to/project \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"
```

输出：

- `package_id`
- `file_count`

### 7) 上传 command 对象

先创建 `command.json`：

```json
{
  "argv": ["python3", "train.py", "--epochs", "1"],
  "env_patch": {
    "MODE": "debug"
  },
  "timeout_sec": 3600,
  "working_dir": "."
}
```

上传：

```bash
orbit command upload \
  --file command.json \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"
```

输出：

- `command_id`

### 8) 上传签名后的 task 对象

```bash
orbit task upload \
  --package-id <PACKAGE_ID> \
  --command-id <COMMAND_ID> \
  --private-key <ORBIT_TASK_PRIVATE_KEY_B64> \
  --created-by "$USER" \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"
```

输出：

- `task_id`
- `package_id`
- `command_id`

### 9) 提交运行任务

```bash
orbit run submit \
  --hub-url http://127.0.0.1:8080 \
  --agent-id agent-a \
  --task-id <TASK_ID> \
  --package-id <PACKAGE_ID> \
  --command-id <COMMAND_ID> \
  --api-token "$ORBIT_API_TOKEN"
```

输出：

- `run_id`
- `run_ticket`

### 10) 启动 Agent

```bash
export ORBIT_AGENT_ID="agent-a"
export ORBIT_HUB_URL="http://127.0.0.1:8080"
export ORBIT_API_TOKEN="$ORBIT_API_TOKEN"
export ORBIT_TICKET_SECRET="$ORBIT_TICKET_SECRET"
export ORBIT_TASK_PUBLIC_KEY_B64="<ORBIT_TASK_PUBLIC_KEY_B64>"

orbit agent run
```

Agent 使用与上传侧相同的 GitHub 环境变量，但只需要只读权限。

### 11) 查询状态、日志和结果

```bash
orbit run status --hub-url http://127.0.0.1:8080 --run-id <RUN_ID> --api-token "$ORBIT_API_TOKEN"

orbit run logs \
  --hub-url http://127.0.0.1:8080 \
  --run-id <RUN_ID> \
  --api-token "$ORBIT_API_TOKEN" \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"

orbit run result \
  --hub-url http://127.0.0.1:8080 \
  --run-id <RUN_ID> \
  --api-token "$ORBIT_API_TOKEN" \
  --github-owner "$ORBIT_GITHUB_OWNER" \
  --github-repo "$ORBIT_GITHUB_REPO" \
  --github-release-prefix "$ORBIT_GITHUB_RELEASE_PREFIX"
```

## 当前范围

已实现：

- `file_package + command + task` 对象模型
- GitHub Release Asset 对象存储后端
- Hub `/api` 的 run 提交、lease、heartbeat、complete 和状态查询
- Agent 的 pull-execute-complete 执行循环
- 确定性的 Git 感知文件打包
- task 签名校验和 run ticket 重放保护
- 日志/结果存放于 GitHub，Hub 只保存对象 ID

尚未实现：

- artifact 上传/下载
- 流式日志分片
- 多对象存储后端
- 更丰富的调度或 placement constraint
