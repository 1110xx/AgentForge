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
| images/ | 目录 | Frozen、non-root 的 control-plane/runtime/frontend 镜像（Phase 4.1：三镜像；frontend 含非 root nginx SPA + 懒解析反代） |
| prod/ | 目录 | golden 生产 values（真 sha256 digest）× 镜像 refs JSON（发布管道写回） |
| kind/ | 目录 | Disposable L3 集群、依赖和覆盖 values |
| observability/ | 目录 | OTel processor/pipeline 与 Prometheus correctness/SLO 规则 |
| runbooks/ | 目录 | Effect、Lease、数据库、NATS、对象、凭据和 DR 故障处理 |
| env.example | 文件 | L2/runtime profile 的非秘密环境变量契约 |
| docker-compose.yml | 文件 | L2 依赖栈与显式 runtime profile |

## 验证状态
- L2（Docker Compose 真实 PostgreSQL/NATS/MinIO 栈）：通过（scripts/test-compose.sh，5 tests）。
- L3 静态门：通过（scripts/check-k8s.sh，四 profile：kind 29 / 生产默认+golden 37 / extended(ingress+pvc) 31 / frontend 33 manifests；prod profile 断言 api/orchestrator/frontend 三 Deployment 均为真 sha256 digest 钉死，占位/示例 registry fail-closed）。
- 镜像发布管道：通过（scripts/build-images.sh：三镜像构建→推 localhost:5001→golden `deploy/prod/values.yaml` 写回真 digest→前端镜像 standalone 冒烟 uid101 + SPA 200/200）。
- **生产接线门（Phase 4.2 G2/G3，2026-08-23 实跑全绿）**：scripts/bootstrap-prod-wiring.sh（ingress-nginx + ESO v0.9.20 + cert-manager v1.15.3 + dev Vault + ClusterSecretStore + ExternalSecret 预置 + round-trip 9 键逐字节一致）；scripts/test-prod-form.sh（golden 形态 helm 部署 → G2 注入断言（含 DeepSeek 模型 key 生产路径）→ G3 TLS 断言（root/api 200、leaf SAN、CA 链可验、SSE off）→ L3 动门 2/2）。动门抓出并修 4 个静态门覆盖不到的缺口（frontend PodSecurity、非 root bind<1024、frontend→api 反代通道、ingress→frontend 后端通道）。
- 前端联调：scripts/verify-frontend-live.sh 直连 + vite dev proxy 双通道 /v1/chat 201 全过（本机实测 4/4）。
- L3 动态门：backend/tests/kind/test_attempt_job.py（需要 Kind 集群，由 scripts/test-kind.sh 编排；本机未安装 kind/helm 时跳过）。
