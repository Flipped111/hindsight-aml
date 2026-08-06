# AGENTS.md

项目架构、命令和编码规范见 [CLAUDE.md](./CLAUDE.md)。开始写 Python 或 TypeScript 前，必须完整阅读
`.claude/skills/code-review/SKILL.md`；完成实现后运行项目要求的 lint、测试和代码审查。
赛事契约、提交材料和评测流程见 [参赛要求.md](./参赛要求.md)。

## 目标与边界

目标不是重写 Hindsight，而是新增一个符合 Agent Memory Leaderboard（AML）契约、可由 Docker Compose
一键启动的适配层：

```text
AML POST /add    -> AML adapter -> Hindsight retain
AML POST /search -> AML adapter -> Hindsight recall -> memory evidence
AML GET  /health -> AML adapter health
```

硬性边界：

- 不改动 Hindsight 原有 API 语义；适配代码放在独立目录。
- Search 只返回证据，禁止调用 `reflect`、生成最终答案或操纵 Answer/Eval。
- `user_id` 是唯一检索隔离边界，不同用户绝不能共用 Bank。
- Add 必须同步完成：只有 retain、抽取、向量和索引全部成功后才返回 HTTP 200。
- Add/Search 阶段使用 `openai` + `gpt-4o-mini`；密钥只从环境变量读取。
- 先完成可部署基线，再做原始消息检索、时间重排等优化。

## 版本与分支

- 当前基线：Hindsight `0.8.6`，commit `436bc7c156f1c94714ea1f757bfc930ab89f883b`。
- 上游仓库：`https://github.com/vectorize-io/hindsight`。
- 上游许可证：MIT（以仓库 [LICENSE](./LICENSE) 为准）。
- 不在 `main` 上开发；从固定基线创建 `aml-hindsight` 分支。
- 最终提交记录确切 commit/tag，不在提交前频繁同步上游。
- 不擅自改 remote、push、创建公开仓库或 tag；这些外部操作需要用户明确授权。

## 目标文件

```text
aml_adapter/
  __init__.py
  app.py
  schemas.py
  service.py
  storage.py
  requirements.txt
tests/aml_adapter/
  test_add.py
  test_search.py
  test_isolation.py
  test_idempotency.py
docker-compose.aml.yml
.env.example
README_AML.md
ATTRIBUTION.md
```

## POST /add

请求字段：`request_id`、`messages[]`、`user_id`、`session_id`。成功响应必须原样返回三个 ID：

```json
{"success":true,"request_id":"...","user_id":"...","session_id":"..."}
```

实现规则：

1. Bank ID：`sha256(user_id.encode("utf-8")).hexdigest()`。
2. 每条消息的文档 ID：`sha256(f"{request_id}:{index}".encode("utf-8")).hexdigest()`。
3. 文档 ID 必须放在对应 item 上；禁止只用 `session_id`，否则同一会话的多个 chunk 会互相覆盖。
4. 将官方毫秒时间戳转成 UTC ISO 8601，并传给 Hindsight 的 `timestamp` 字段。
5. 内容保留 `Speaker`、`Session`、`Event time` 和原始 `Message`，不要只保存抽取后的改写。
6. metadata 至少保存 `request_id`、`session_id`、`role`、`original_timestamp`。当前 Hindsight
   `MemoryItem.metadata` 的值类型是字符串，因此时间戳也要转成字符串。
7. 调用同步语义 retain。同步代码使用 `retain_batch(..., retain_async=False)`；异步 FastAPI 代码使用
   `await aretain_batch(..., retain_async=False)`，避免阻塞事件循环。
8. 不得启用要求异步 retain 的批处理配置；失败或超时不能登记为成功，也不能返回 200。

建议写入内容：

```text
Speaker: user
Session: eval:run:session-0
Event time: 2024-01-01T00:00:00Z
Message: 我现在住在东京。
```

## 幂等

持久化保存已完成请求，至少包含：

```sql
CREATE TABLE IF NOT EXISTS processed_requests (
  request_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('processing', 'completed')),
  completed_at TEXT
);
```

- 调用 retain 前先原子插入 `processing` 占位；仅在同步 retain 全部成功后改为 `completed`。
- 已完成且 `user_id`、`session_id`、payload 均相同的请求直接返回成功。
- 同一 `request_id` 携带不同用户、会话或 payload 时返回冲突，不能误报成功。
- 并发的同 payload 请求应等待首次处理完成；失败/失联占位必须允许安全重试。
- 不能只在 retain 后插入记录；唯一约束、原子占位和稳定文档 ID 共同防止重复。
- 数据库文件必须放入 Compose 持久化 volume。

## POST /search

请求字段：`query`、`options`、`user_id`、`top_k`。`options` 为契约字段，可接收但不能据此生成答案。

```python
result = await client.arecall(
    bank_id=user_to_bank_id(request.user_id),
    query=request.query,
    budget="mid",
)
```

禁止调用 `reflect`。将 recall 结果转换为：

```json
{"data":[{"id":"stable-id","content":"memory evidence","score":0.91,"created_at":"2026-01-15T09:00:00Z"}]}
```

- `data` 始终存在；无结果时返回 `{"data":[]}`。
- `id`、`content` 必填且非空；使用可复现的来源 ID，不能每次随机生成。
- `content` 来自 Hindsight `text` 或原始消息证据，不得混入最终答案或系统提示。
- 使用 `scores.final` 作为 score；按相关度降序并在适配层截断到 `top_k`（正式评测最大 100）。
- `created_at` 可选；只有存在可靠的 UTC 来源时间时才返回，禁止伪造当前时间。
- 永远只查询由该 `user_id` 映射出的 Bank。

## GET /health

- 无需鉴权，返回任意 2xx JSON。
- 应检查适配层和 Hindsight 依赖是否可用；不能在 Hindsight 未就绪时虚报健康。

## 必须通过的测试

1. 写入“用户住在东京”后立即搜索能返回东京。
2. user A 的东京与 user B 的上海完全隔离。
3. 同一 `request_id` 重试不会产生第二套记忆。
4. 同一 `session_id` 的不同 request/chunk 都保留。
5. 2025 上海、2026 搬到东京时，“现在住哪里”优先东京。
6. `top_k=5` 时最多返回 5 条；无结果返回空数组。
7. 时间戳、角色、会话和原文均被保留。
8. Docker 重启后记忆和幂等记录仍存在。
9. `/health` 返回 2xx，仓库扫描不存在真实密钥。

测试应使用 fake/mock Hindsight 覆盖契约和故障路径，并另做真实 retain/recall 与 Docker 端到端测试。

## Docker、文档与安全

- 启动命令：`docker compose -f docker-compose.aml.yml up --build`。
- Compose 至少包含 `hindsight`、`aml-api` 和持久化 volume；`aml-api` 对外端口为 8000。
- 从本仓库固定代码构建，或锁定明确 tag/digest；禁止使用未固定的 `latest`。
- `.env.example` 只保留空密钥和固定模型配置；`.env`、数据库、运行数据和密钥不得提交。
- README 写明架构、基线 commit、环境变量、启动命令、接口示例、持久化和已知限制。
- ATTRIBUTION 写明上游项目、作者、MIT 许可证、固定版本、技术报告/论文和全部改动。

## 实施顺序

1. 创建功能分支并记录基线。
2. 跑通原始 Hindsight retain、recall 和重启持久化。
3. 完成适配层、幂等、隔离和基础测试。
4. 完成 Docker、README、ATTRIBUTION 和全新 clone 验证。
5. 基线稳定后再依次做：原始消息检索、时间重排、去重/来源多样化、多跳搜索。
