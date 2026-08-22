## 文档索引
| 文档 | 说明 |
| --- | --- |
| operations.md | 数据路径、L2/L3、生产 Helm/Sandbox、容量和 L4 证据边界。 |
| helm/README.md | Helm Chart 部署细则：values 逐项说明、resources/limits、探针、HPA/KEDA、PVC、Ingress/TLS、无集群静态校验。 |

## 目录内容
| 名称 | 类型 | 说明 |
| --- | --- | --- |
| config/ | 目录 | NATS 与 MinIO 生命周期配置 |
| helm/ | 目录 | 生产 ControlPlane、orchestrator、namespace、安全和弹性清单（Phase 3.6：可选 `frontend` 工作负载 + Ingress 根路径/SSE 反缓冲） |
| images/ | 目录 | Frozen、non-root 的 control-plane/runtime 镜像 |
| kind/ | 目录 | Disposable L3 集群、依赖和覆盖 values |
| observability/ | 目录 | OTel processor/pipeline 与 Prometheus correctness/SLO 规则 |
| runbooks/ | 目录 | Effect、Lease、数据库、NATS、对象、凭据和 DR 故障处理 |
| env.example | 文件 | L2/runtime profile 的非秘密环境变量契约 |
| docker-compose.yml | 文件 | L2 依赖栈与显式 runtime profile |

## 验证状态
- L2（Docker Compose 真实 PostgreSQL/NATS/MinIO 栈）：通过（scripts/test-compose.sh，5 tests）。
- L3 静态门：通过（scripts/check-k8s.sh，四 profile：default 33 / kind 29 / extended(ingress+pvc) 31 / frontend 33 manifests，含第 4 profile 的 frontend Deployment+ConfigMap+Ingress SSE annotation 断言）。
- 前端联调：scripts/verify-frontend-live.sh 直连 + vite dev proxy 双通道 /v1/chat 201 全过（本机实测 4/4）。
- L3 动态门：backend/tests/kind/test_attempt_job.py（需要 Kind 集群，由 scripts/test-kind.sh 编排；本机未安装 kind/helm 时跳过）。
