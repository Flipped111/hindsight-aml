# Hindsight AML 项目进度

更新时间：2026-08-06

## 当前基线

- 开发分支：`aml-hindsight`。
- Hindsight 版本：`0.8.6`。
- 固定上游基线：`436bc7c156f1c94714ea1f757bfc930ab89f883b`。
- 文档提交：`d56b28d3 docs: condense AML project requirements`，已在 `origin/aml-hindsight`。
- 适配层提交：`49f26aed feat: add AML adapter baseline`，当前仅在本地。
- 交付基线提交：`28e3e57c feat: complete AML adapter submission baseline`，当前仅在本地。
- 干净环境复现和密钥扫描修正由包含本进度文件的本地提交记录；以上提交均未 push。
- Docker 实测和健康探测短超时修复提交：`8bf20b63 fix: bound AML dependency health checks`，当前仅在本地。
- 已创建并复验本地单根 `aml-submission` 分支，作为不携带上游历史凭证的安全交付基线。
- 未创建 release/tag，未执行新的 push 或公开仓库操作。

## 已完成

### 1. 参赛约束和项目说明

- `AGENTS.md` 已压缩到 168 行，保留实现边界、接口契约、幂等、测试和部署要求。
- `参赛要求.md` 已压缩到 196 行，保留赛事路线、禁止行为、提交和复现要求。
- 两份文档保持分工：`AGENTS.md` 指导开发，`参赛要求.md` 保存赛事依据。
- 已记录上游项目、MIT 许可证、固定版本和基线 commit。

### 2. 本地 Hindsight 环境

- 已安装 `uv 0.12.2` 和 Python `3.11.15`。
- 已按锁文件安装 Hindsight API `0.8.6`、`pg0-embedded` 和本地 ML 依赖。
- 已下载本地 embedding 模型 `BAAI/bge-small-en-v1.5`。
- 已下载本地 reranker 模型 `cross-encoder/ms-marco-MiniLM-L-6-v2`。
- 本地忽略的 `.env` 已配置 `openai + gpt-4o-mini`，Base URL 为
  `https://hub.oaifree.com/v1`；API Key 未写入任何提交文件。
- embedding 和 reranker 均使用本地模型，retain batch 模式已关闭。
- 已安装 Docker Desktop `4.85.0`、Docker Engine `29.6.2` 和 Compose `5.3.1`，WSL2 集成可用。

### 3. 原始 Hindsight 基线验证

- Hindsight `/health` 返回 healthy，数据库状态为 connected。
- `/version` 确认 API 版本为 `0.8.6`。
- 已执行真实同步 retain，响应为 `success=true`、`async=false`。
- 已立即 recall 并成功检索“用户现在住在东京”。
- 已确认 REST 请求应使用 `async:false`；Python client 使用 `retain_async=False`。
- 已使用固定 `document_id=aml-baseline-smoke-doc-v1` 完成写入和召回。
- 仓库自带 `scripts/smoke-test-slim.sh` 已通过 retain/recall smoke test。
- Hindsight 进程已优雅停止并以同一配置重启。
- 重启后未重复 retain，仍可从固定 Bank 召回东京，证明 pg0 数据持久化有效。
- 重启后读取固定源文档，确认原文、角色、会话、事件时间和 metadata 均被保留。

### 4. AML 适配层代码

提交 `49f26aed` 已包含：

- `aml_adapter/app.py`：FastAPI 的 `POST /add`、`POST /search`、`GET /health`。
- `aml_adapter/schemas.py`：AML 请求、响应和内部边界模型。
- `aml_adapter/service.py`：Bank 映射、retain/recall 转换和结果排序。
- `aml_adapter/storage.py`：SQLite 持久化幂等状态和原子 claim。
- `aml_adapter/requirements.txt`：固定 FastAPI 和 Uvicorn 版本。
- `aml_adapter/Dockerfile`：独立 AML API 镜像构建入口。

当前实现包括：

- `sha256(user_id)` 到 Hindsight Bank 的确定性映射。
- `sha256(f"{request_id}:{index}")` 稳定文档 ID。
- 毫秒时间戳到 UTC ISO 8601 的转换。
- `Speaker`、`Session`、`Event time` 和原始 `Message` 的完整保留。
- metadata 中保存 request、session、role 和原始时间戳。
- 通过 `aretain_batch(..., retain_async=False)` 执行同步写入。
- Search 只调用 `arecall(..., budget="mid")`，未调用 `reflect`。
- 使用 `scores.final` 排序并按 `top_k` 截断，过滤空 ID 或空内容。
- 仅在 Hindsight 提供可靠时间时返回 `created_at`。
- SQLite 在 retain 前原子写入 `processing`，成功后才标记 `completed`。
- 相同请求等待首次处理完成，冲突 payload 返回冲突，失败占位允许重试。
- 使用处理租约和 owner token 恢复失联占位，避免旧 worker 完成新 claim。
- Hindsight 健康探测使用独立 3 秒超时，不继承可达 600 秒的 retain/search 业务超时。

### 5. 自动化契约测试

已新增：

- `tests/aml_adapter/test_add.py`。
- `tests/aml_adapter/test_search.py`。
- `tests/aml_adapter/test_isolation.py`。
- `tests/aml_adapter/test_idempotency.py`。
- `tests/aml_adapter/support.py` fake Hindsight 和应用测试支持。

AML 测试共 24 项，已全部通过，覆盖：

- Add 响应格式、每条消息的稳定文档 ID、同步 retain 客户端参数和完整成功确认。
- 时间戳、角色、会话、原文和字符串 metadata 保留。
- Search 的 Bank 映射、`budget="mid"`、`scores.final`、排序、过滤、`top_k` 和空数组。
- user A 东京与 user B 上海严格隔离。
- 同一 session 的不同 request/chunk 不覆盖。
- 2025 上海、2026 东京场景中新事件优先。
- 已完成请求重试、冲突 payload、并发相同请求等待、等待超时、失败释放和安全重试。
- 处理租约过期后的 owner token 替换，旧 worker 不能完成新 claim。
- SQLite 重开后幂等完成记录仍存在。
- `/health` 健康、Hindsight 不可用路径和依赖调用挂起时的独立短超时。

相关 Hindsight Python client 纯单元测试 28 项通过，覆盖同步 retain 参数序列化、recall 参数和版本调用。

### 6. 真实 AML 端到端验证

- 已启动本地 Hindsight `0.8.6` 和真实 AML FastAPI 适配层。
- 真实 `/health` 返回 adapter、Hindsight 和 SQLite 全部 healthy。
- 真实 `/add` 使用唯一 user/request 写入“现在住在东京晴空塔附近”，仅在同步 retain 完整结束后返回 200。
- Hindsight 日志确认稳定 SHA-256 Bank、稳定 document ID、事实抽取、embedding、写库和 ANN 索引均完成。
- 随后真实 `/search` 返回两条包含 `Tokyo Skytree` 的记忆证据及可靠的 `2024-01-01T00:00:00Z` 事件时间。
- 相同 `/add` 请求再次提交后直接返回成功，真实 SQLite 幂等路径生效。
- 测试结束后 AML API、Hindsight 和嵌入式 pg0 均已优雅停止。
- 已通过 Compose 真实写入“现在住在东京浅草附近”，并立即检索到带
  `2025-01-01T00:00:00Z` 事件时间的证据。
- 重试同一 Add 仅用 76 ms；Hindsight 来源接口确认仅有一个稳定 SHA-256 document，
  `memory_unit_count=1`。
- 重启两个 Compose 容器后仍可检索同一稳定证据 ID；再次重试 Add 仅用 14 ms，证明 pg0 记忆和
  SQLite completed 记录均由 volume 持久化。
- 停止 Hindsight 后，修复后的 AML `/health` 在 2.016 秒内返回 HTTP 503；恢复 Hindsight 后重新返回
  adapter、Hindsight 和 SQLite 全部 healthy。

### 7. Docker、文档和配置

- 已新增 `docker-compose.aml.yml`，从当前仓库构建固定源码，不使用 `latest`。
- Compose 包含 `hindsight`、`aml-api`、pg0 volume 和 SQLite volume，AML API 暴露端口 8000。
- Hindsight 健康检查同时验证 `status=healthy` 和 `database=connected`；AML 健康检查要求自身返回 200 healthy。
- Compose 强制 `openai + gpt-4o-mini`、本地 embedding/reranker、同步 retain batch 配置和抽取失败即失败。
- 已新增 `README_AML.md`，记录架构、启动、配置、接口、持久化、测试和已知限制。
- 已新增 `ATTRIBUTION.md`，披露上游作者、MIT、版本、commit、论文和全部 AML 改动。
- 根 `.env.example` 的活动 API Key 已置空，并固定赛事模型、本地检索模型和 AML 配置。
- `hindsight-embed/hindsight_embed/env.example` 已同步，两个模板 byte-identical。
- `.dockerignore` 和 `.gitignore` 已排除 `.env` 与 SQLite 运行数据。
- Compose YAML 和关键服务、端口、依赖、volume 结构已通过静态解析验证。
- `docker compose -f docker-compose.aml.yml up --build -d` 已从当前仓库完成首次真实构建；本地 embedding、
  reranker 和 tiktoken 数据已成功预载进固定源码镜像。
- Compose 创建的 `hindsight-data` 和 `aml-data` volume 已通过容器重启持久化验证。

### 8. 质量和代码审查

- `ruff check aml_adapter tests/aml_adapter --no-force-exclude` 通过。
- `ruff format --check aml_adapter tests/aml_adapter` 通过。
- `ty check aml_adapter tests/aml_adapter` 通过。
- `python -m compileall aml_adapter` 通过。
- 项目完整 `./scripts/hooks/lint.sh` 通过。
- `./scripts/hooks/check-unused.sh` 已运行，只报告上游既有 advisory 项，没有 AML 文件发现。
- 按 `.claude/skills/code-review/SKILL.md` 完成审查，没有 AML must-fix 或 should-fix。
- `git diff --check` 通过，环境模板一致性和 Compose 静态契约检查通过。
- AML 新增/修改文件未扫描到真实 API Key 或 Bearer token；分支新增提交和全历史高熵模式扫描未发现密钥。
- 已从提交和当前候选工作树分别创建不含 `.git`、`.env` 和本地虚拟环境的干净导出；按修正后的
  README 三步流程安装依赖和本地 `hindsight-client` 后，当时的 23 项 AML 测试全部通过；当前工作树新增
  health 超时测试后为 24 项。
- 已从 `8bf20b63` 使用 `git clone --no-local --single-branch` 创建独立 Git 对象的全新 clone；确认没有
  `.env`、虚拟环境或运行数据库，按 README 安装后 24 项 AML 测试全部通过。
- 全新 clone 使用独立 Compose 项目和 volume 完成构建、`/health -> /add -> /search -> restart -> /search`；
  重启后京都祇园证据仍可检索，同一 Add 仅用 6 ms 返回，来源接口确认只有一个稳定 document 且
  `memory_unit_count=1`。
- 干净导出首次验证发现 README 原单行测试命令未安装 workspace client；文档已改为明确的
  `uv sync`、安装本地 client、运行 pytest 三步流程。
- 使用 `detect-secrets 1.5.0` 聚焦扫描 AML 提交文件，只发现环境模板中的示例数据库 URL；已用
  allowlist 注释明确标记该占位符，两个模板保持一致。
- 使用固定 Gitleaks `v8.30.1`（镜像摘要
  `sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f`）完成扫描：
  - 固定上游基线之后的 AML 提交零命中。
  - 当前全部 tracked 文件的保守构建上下文扫描只有模型名、测试占位符、文档 Bearer 示例和公开数据 URL
    等误报；AML 文件无真实密钥，实际 `.dockerignore` 还会排除 benchmark、`.env` 和运行数据库。
  - `aml-api` 镜像全部 layer 零命中；Hindsight layer 仅有系统 mplayer 补全变量误报，镜像配置仅有
    Python 基础镜像公开 GPG 指纹；两镜像构建历史均无敏感模式。
- 对 2347 个非合并上游提交扫描 913 MB 历史补丁后，确认继承的上游历史曾跟踪包含真实凭证的已删除
  `.env.dev`；这些值不在当前树、AML 提交、Docker context 或镜像中，但开发分支历史不能直接公开。
- 已创建内容相同的单根 `aml-submission` 分支，保留 LICENSE、ATTRIBUTION 和上游精确 commit 记录，
  同时不携带上游历史凭证；其全新 clone 24 项测试、Compose 构建、Add/Search、容器重启、持久化和最终
  healthy 状态均已复验。不得直接公开 `aml-hindsight` 的完整继承历史。
- 全仓扫描的 3 个 JWT 告警均来自上游 LoCoMo benchmark 中公开图片 CDN 的签名 URL，不是模型或服务凭证；
  `hindsight-dev/benchmarks` 已排除出 Docker build context。

## 已实现但尚未验证

- 尚未在比赛平台执行官方 Smoke/Full；本地全新 clone、真实 Compose、重启持久化和安全扫描均已完成。

## 未完成

### 1. Docker 和持久化部署

- [x] 验证 `docker compose -f docker-compose.aml.yml up --build`。
- [x] 验证容器重启后 Hindsight 记忆和 SQLite 幂等记录仍存在。

### 2. 完整交付验证

- [x] 在全新 clone 中完成安装、Compose 启动和 smoke test。
- [x] 使用 Gitleaks 扫描 AML 提交、tracked context 和镜像层，并识别上游历史凭证风险。
- [x] 创建本地提交，记录测试、Compose、环境模板、README、ATTRIBUTION 和进度更新。
- [x] 创建并复验不含上游历史的单根 `aml-submission` 交付 commit。
- [ ] 将本地 AML 提交 push 到远端；该操作尚未执行。
- [ ] 完成后固定最终 commit/tag，并更新 README 和 ATTRIBUTION 中的版本记录。

### 3. 基线后的可选优化

以下内容必须等可部署基线稳定后再做：

- [ ] 原始消息检索。
- [ ] 时间重排。
- [ ] 去重和来源多样化。
- [ ] 多跳搜索。

## 建议的下一步

1. 更新 README 和 ATTRIBUTION 中的最终 submission commit/tag。
2. 发布时只 push/公开 clean-history `aml-submission` 分支，不公开继承上游完整历史的开发分支。
3. 基线交付验证完成后，再依次评估原始消息检索和时间重排。
4. 经用户明确授权后再 push、创建最终 tag 或公开仓库。
