# Task 13：独立交付、公共装配、文档与统一验证报告

日期：2026-08-07（L2/L3 验证结果更新于 2026-08-17）

## 交付结论

enterprise-agent-platform/ 已成为可整体复制的独立交付单元，Python package、前端 SDK、Contracts、部署资产、迁移/安全文档和验证脚本均在本目录内；产品代码与测试不需要父仓库业务 module、绝对路径或私有服务地址。

本任务没有提供默认生产 fallback：

- Compose api/worker 只有选择 runtime profile 才启动；env.example 的两个 integration factory 默认留空。
- reference.local_stack 是显式 API-only、本地固定身份、InMemoryPlatformStore 的演示入口：它只能创建/读取/取消 synthetic Run，创建后保持 QUEUED，没有 scheduler 或 worker。
- 生产 Host adapter、worker factory、PostgreSQL、身份/资源/策略 Ports、Connector、Credential Broker 和企业依赖必须显式组装。

## 公共装配边界

包根现在导出的公共表面由测试锁定：

- Auth/Resource/HostContext/Policy Ports 与 PlatformStore/PlatformTransaction：create_app、create_router、create_in_memory_container、AgentPlatformContainer
- ApprovalDecisionService、DurableEffectExecutor
- Capability issuer/verifier、Runtime/Effect grant、key、revocation 和 runtime-fence Ports；Connector 与 Credential Broker、FailedEffectRecoveryService 及 Effect payload/capability/reconciliation Ports

作为 executor_id：Connector finish 与 watchdog 都使用 executor_id + execution_epoch ownership fence；FAILED Effect 则由 public recovery command 保留日志账本并调度新 Attempt 重新规划、提案和审批。宿主可以组合完整审批和 durable Effect 链路，而不导入 SQLAlchemy tables。Effect execute/reconcile route 仍是受 service identity 与 tenant/effect-bound capability 保护的内部 API，不属于浏览器 public surface。Internal execute route 把验证后的 effect-worker subject 与 effect-bound capability 绑定后执行。

可复制性验证：

- 测试把整个目录复制到空父目录，删除 venv、node_modules 和构建产物，证明 Python 只从复制件加载；前端在复制件内离线 npm ci 后执行协议测试。
- scripts/check-portability.py 用 AST/文本扫描 Python、TypeScript、相对路径、转义符号链接、宿主绝对路径、内部 URL 与高置信秘密。
- Hatchling wheel 包含 enterprise_agent_platform package 与 dist-info；clean venv 按 lock/hash 安装 runtime dependencies，使用 package root public surface 完成 reference workflow。
- generate-contracts.py 确定性生成 JSON Schema/OpenAPI；check-generated.sh 在临时目录逐字节比较 checked-in contracts。
- .dockerignore 排除 Git、Python/Node caches、venv、node_modules、dist 和 tsbuildinfo，镜像仍只以独立目录为 context。

## 统一验证结果

### L1（本地单测，2026-08-07）

最终执行：`./scripts/verify.sh l1`，结果 **L1 VERIFIED**，包括：

- Backend：frozen uv sync 与 npm ci 成功；npm 报告 vulnerabilities（上游依赖）。
- Ruff：All checks passed，276 passed；唯一 warning 是 FastAPI TestClient 上游的 Starlette deprecation。
- generated JSON Schema/OpenAPI byte parity：通过。
- Frontend：5 个文件 38 passed；typecheck、ESLint、四个 package build 与 embedded Vite build：通过。
- wheel build、53 个 hash-locked runtime dependency 的 clean venv 安装、installed-package reference smoke：通过。
- portability：246 authored file(s) scanned，通过。
- 所有 shell entrypoint 的 bash -n：通过。

### L2（Compose 依赖集成，2026-08-17 补跑）

最终执行：`./scripts/verify.sh l2`，结果 **L2 VERIFIED**。

- 一次性 Docker Compose 栈：PostgreSQL、NATS JetStream、MinIO 全就绪。
- 控制面镜像（无 dev 依赖）与 test-runner 镜像（含 dev 依赖）构建成功。
- migrate、minio-init（bucket + versioning + lifecycle）执行成功。
- test-runner：`5 passed in 3.73s`。

### L3（Kind 真实执行，2026-08-17 补跑）

最终执行：`./scripts/verify.sh l3`，结果 **L3 VERIFIED**。

- 一次性 Kind 集群（3 节点 + Calico NetworkPolicy）+ 本地 registry。
- 依赖服务（PostgreSQL/NATS/MinIO）真实运行于集群内，migrate job 完成。
- Sandbox Attempt 测试：`1 passed in 9.16s`——Job/Pod 真实运行到 Completed（Pod Scheduled → Pulled → Created → Started → Completed）。
- 环境：Docker 27.4.0 / kind v0.26.0 / kubectl v1.30.5 / helm v3.16.2 / uv 0.12.4。

### 历史环境门禁记录

早期（Docker daemon 不可用时）：

```text
./scripts/verify.sh l2 → exit 69, UNVERIFIED: Docker daemon is unavailable
./scripts/verify.sh l3 → exit 69, UNVERIFIED: Docker daemon is unavailable
```

上述门禁已随 Docker daemon 恢复而通过（见 L2/L3 小节），不再成立。

## L3 排障记录

打通 L3 过程中修复的问题（按顺序）：

1. kind 节点镜像（kindest/node）下载失败 → 经 dockerproxy.net 拉取；cluster.yaml nodes 指定 image tag。
2. calico 镜像 load 报 content digest not found → 多架构 manifest list 只导出 amd64 → 用 amd64 单平台 manifest digest 拉取。
3. calico 就绪等待失败 → namespace 写错 → calico-system 改为 kube-system。
4. postgres/nats/minio 依赖镜像同报多架构 → 逐镜像取 amd64 manifest digest。
5. 依赖服务起不来 → runAsNonRoot: true 与镜像默认 root 冲突 → 指定 runAsUser/fsGroup（postgres=70、nats/minio=65532）。
6. NATS 一直 0/1，JetStream 无 leader → 3 副本 StatefulSet 有序启动 × Raft 多数派死锁 → 降为单副本 + 移除 cluster/routes 配置。
7. docker push 超时 dial [::1]:5001 → registry 只监听 IPv4 → 双栈发布 127.0.0.1:5001 + [::1]:5001 + 就绪等待。
8. kind load 报 No such container → 集群刚建好就 load → 加 sleep 15 等 worker 就绪。
9. minio-versioning job 卡住 → mc 容器 readOnlyRootFilesystem 无写目录 → 挂 tmp + MC_CONFIG_DIR=/tmp/.mc。
10. migrate job 报 secret 不存在 → secret 在 dependencies ns，job 在 control ns → 两个 namespace 都建 secret。
11. migrate job 拉镜像失败 → 构建产物未预加载 → kind load docker-image control/runtime 镜像。
12. RuntimeClass name 为空 → values.yaml 空串 → helm 覆盖 sandbox.runtimeClassName=agent-platform-kind-sandbox。
13. secret 名不匹配 → chart 引用 agent-platform-dependencies 但实际是 -kind-dependencies → helm 覆盖 secrets.externalSecretName。
14. attempt Pod 一直 Pending / POD_NOT_OBSERVED（最终关键修复）→ PSA restricted 缺 seccompProfile，Pod 被静默拒绝（Job Running 但无 Pod）→ job_spec.py 在 pod 级 + 容器级 securityContext 补 seccompProfile: {type: RuntimeDefault}。
15. 手动测试镜像拉取失败 → 用了旧 digest → 必须用 docker image inspect --format '{{index .RepoDigests 0}}' 取当前 digest。

修复后验证链：Pod Scheduled → Pulled（本地 digest 匹配预加载镜像）→ Created → Started → Completed。

## 文档交付

- architecture.md：统一 Run/Step/ExecutionUnit/Attempt/Runner/Pod、Checkpoint/Snapshot、状态与数据流。
- security.md：Sandbox 防逃逸、capability、RBAC/network、Tool/Effect/A2UI 与残余风险。
- implementation.md：当前模块、public/internal API、已实现能力、缺口与证据层级。
- embedding-guide.md：Host Ports、反向代理、SSE、前端 SDK、A2UI 与 Artifact 接入。
- 全部现有 DOCS.md 已改为 agent-docs 的「文档索引/目录内容」两段导航格式。
- migration-guide.md：从宿主仓库内原型迁移到独立平台的 inventory、adapter、数据、shadow、cutover 与 rollback。

## 仍未签署

L4（gVisor/OIDC/HA/PITR/DR、生产 Connector、成本负载）必须在生产等价环境单独签署。真实 PostgreSQL repository 模块当前 2 skipped（未配置 AGENT_PLATFORM_DATABASE_URL），其中已包含 Effect finish-vs-cancel、active execution lease 以及 watchdog 与 late-success 重叠提交窗口。

## Git 与作用域

本任务未执行 Git add、commit 或 push；所有交付物均在独立目录 enterprise-agent-platform/ 内。
