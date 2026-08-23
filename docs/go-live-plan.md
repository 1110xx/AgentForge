# Go-Live Plan — 生产上线差距与分阶段计划

> 对应主 `SDD.md` §Phase 4（生产上线 Go-Live，2026-08-23 立项）。
> 结论先行：工程能力已到 **kind 集群全链闭环**（L3 动态门实跑、Helm 四 profile 静态门、三扇门全绿），
> 离生产上线还差 **发布/交付自动化、生产安全与 HA 接线、观测运维、SDD 后置深度优化项（Phase 3.5 D/E）** 四块。

## 1. 已具备的生产骨架（不重复造）

| 能力 | 证据 |
| --- | --- |
| K8s 执行链 | `execution/k8s_worker.py` + HttpRunner + L3 实跑（真 digest helm） |
| 可靠事件投递 | NATS outbox relay + wakeup consumer（`ef022e6`） |
| Internal 6-port 适配 + token 鉴权 | `internal_adapter.py`（projected / runtime-token / service-token / effect-token） |
| 幂等 / 租约 / 检查点 / 子进程重试 | 全控制面机制（Idempotency-Key、Lease、Checkpoint 快照、RECOVERING 重试） |
| 部署资产 | Helm 全参数化（resources / probes / HPA / PDB / PriorityClass / Ingress / PVC / NetworkPolicies / RuntimeClass / ExternalSecret 契约）；api 探针指向真实 `/api/agent-platform/v1/health/live\|ready` 端点 |
| 前端 | Launcher / AgentPanel / FollowupPanel + mock/live 双模式 + Ingress SSE 反缓冲 |

## 2. 差距清单（主 SDD G.1，按优先级）

### 🔴 上线阻断级

| # | 缺口 | 现状证据 | 要做 |
| --- | --- | --- | --- |
| G1 | **镜像发布管道缺失** | `values.yaml` digest 全为 `sha256:000…0` 占位；F-D 契约「镜像不本仓库构建，宿主 CI 注入」 | 应用 CI/仓库构建 runtime+control+frontend 三镜像 → push 生产 registry → GitOps（ArgoCD/Flux）或 `helm upgrade --set` 注入真 digest |
| G2 | **生产 Secret 未实接** | `external-secret-contract.yaml` 是模板契约（`storeRefName: enterprise-secret-store`） | 部署真实 External Secrets Operator + SecretStore（Vault/KMS）；拉取 PG/NATS/model-key 凭据；模型 API key 生产注入路径验证 |
| G3 | **TLS/DNS 未配** | `ingress.enabled: false`；host `agent-platform.example.invalid`；tls 空 | 真实域名 + cert-manager clusterIssuer（模板已支持）或现有 TLS secret；验证根路径 + `/api/agent-platform` 最长前缀 |

### 🟡 灰度/可用性级

| # | 缺口 | 说明 |
| --- | --- | --- |
| G4 | **生产级安全（SDD 明示不在交付范围）** | gVisor RuntimeClass 模板/SAname 已备，需集群具备该 RuntimeClass 并验证 egress 行为；**OIDC/多租户身份未做**（当前为参考 token 最小鉴权）；HA 未验证（多副本 + PDB 模板有） |
| G5 | **观测闭环缺后端** | `platform/telemetry.py` 有完整 OTLP traces/metrics 导出（HTTP OTLP endpoint 校验），但无 collector / 告警规则 / Grafana 面板；**日志无集中化**（Loki/ELK） |
| G8 | **容量/长 Run 验证缺** | HPA 配置在（`autoscaling.apiCpuAverageUtilization`），无真实负载与数小时长 Run 验证——**决定 Phase 3.5 D（快照膨胀消解）/ E（2PC write-ahead）是否转必做** |

### 🟢 维护级

| # | 缺口 | 说明 |
| --- | --- | --- |
| G6 | **PG 备份/DR 未文档化** | 持久化在外部 PG；备份策略、PITR、跨区 DR 全为宿主职责，无文档条目 |
| G7 | **零停机发布/回滚未实战** | `deploy/helm/README.md` 有 upgrade/rollback 章节，未在集群演练（migration Job 与 KEDA/HA 组合） |

补充维护项（非 G 编号）：后端 ruff 96 存量 errors（S110 等，安全 lint 债）；前端 Google Fonts 公网依赖（内网部署需自托管字体）；L3 门当前为 kind 形态（生产形态验证归入 4.2）。

## 3. 分阶段执行计划（主 SDD G.2）

- **Phase 4.1 发布管道（G1）**：应用 CI/仓库构建三镜像（backend runtime/control + frontend nginx）→ 生产 registry → GitOps 清单或注入真 digest → helm 渲染真镜像一键部署可复现。
- **Phase 4.2 生产接线（G2/G3）**：ESOP 实接 + 域名/TLS 生效；**L3 动态门重跑为生产形态**（当前 L3 为 kind）。
- **Phase 4.3 可观测（G5）**：OTLP collector 接入（values 无 collector 段，需补或复用现有观测栈）+ 告警 + Grafana 面板 + 日志集中化。
- **Phase 4.4 容量与长 Run（G8）**：HPA 压力验证 + 数小时长 Run 稳定性 → 评估立项 Phase 3.5 D/E。
- **Phase 4.5 安全加固 + 备份文档（G4/G6/G7）**：gVisor 形态验证或替代决策、OIDC 决策、备份演练、发布/回滚演练。

## 4. 验收标准（主 SDD G.3，逐阶段可勾选）

| 阶段 | 验收 |
| --- | --- |
| 4.1 | 三镜像可构建可推送；git 内含真 digest 的 helm 渲染（沿用 check-k8s profile 断言）全绿；生产一键部署可复现 |
| 4.2 | SecretStore 运行且凭据注入生效；域名 + TLS 生效；L3 门在生产形态重跑全绿 |
| 4.3 | OTLP traces/metrics 可在观测后端正查（≥1 Grafana 面板）；≥3 条告警规则生效；日志集中可检索 |
| 4.4 | HPA 压力验证通过（消息积压→扩容→回缩）；数小时长 Run 无快照膨胀异常；出具 D/E 立项结论 |
| 4.5 | gVisor 验证结论或替代方案；OIDC 决策记录；备份恢复演练一次 PASS；发布/回滚演练一次 PASS |

## 5. 最短上线路径（建议顺序）

1. **G1 镜像管道**（其余项都依赖真镜像才能部署验证）
2. **G2/G3 生产接线 + L3 生产形态重跑**
3. **G5 可观测**
4. **G8 容量/长 Run**（触发 D/E 评估）
5. **G4/G6/G7 安全加固 + 备份文档**