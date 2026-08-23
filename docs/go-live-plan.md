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
| ~~G5~~ **观测闭环** ✅ 已交付（2026-08-23，Phase 4.3） | 原：`telemetry.py` 导出齐全但**全仓零接线**，无 collector/告警/Grafana，日志无集中化 | 后端埋点全链路（trace 上下文 + 四组业务指标 + JSON 日志关联）+ OTLP collector + 告警 15 条（CRD+rule_files 双形态）+ Grafana 三看板 + Loki 集中化（promtail）+ helm `observability` 段 + Runner Job env 透传；明细见 `deploy/observability/README.md` 与 SDD §G.3 4.3 |
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
- **Phase 4.3 可观测（G5）** ✅ 已交付（2026-08-23）：OTLP collector 接入（helm values `observability` 段 + Runner Job env 透传）+ 告警（15 条）+ Grafana 三看板 + Loki 日志集中化；`scripts/test-observability.sh` 门就绪（kind 实跑待镜像网络恢复）。
- **Phase 4.4 容量与长 Run（G8）**：HPA 压力验证 + 数小时长 Run 稳定性 → 评估立项 Phase 3.5 D/E。
- **Phase 4.5 安全加固 + 备份文档（G4/G6/G7）**：gVisor 形态验证或替代决策、OIDC 决策、备份演练、发布/回滚演练。

## 4. 验收标准（主 SDD G.3，逐阶段可勾选）

| 阶段 | 验收 |
| --- | --- |
| 4.1 | 三镜像可构建可推送（✅：`build-images.sh` 实跑，`localhost:5001` 推三镜像）；git 内含真 digest 的 helm 渲染全绿（✅：`deploy/prod/values.yaml` golden + check-k8s prod profile 断言）；生产一键部署可复现（✅：`helm upgrade --install -f deploy/prod/values.yaml` 单命令，helm README §8 记录） |
| 4.2 | SecretStore 运行且凭据注入生效；域名 + TLS 生效；L3 门在生产形态重跑全绿（✅ 2026-08-23：ESO+Vault 实接、注入断言、TLS/入口断言、L3 生产形态 2/2 全绿；gVisor 可选留 4.5，KEDA 留 4.4） |
| 4.3 | ✅ 后端埋点全链路（trace 上下文/四组业务指标/JSON 日志关联）+ 观测栈（collector/Tempo/Prometheus/Loki/Grafana/promtail）+ 告警 15 条 + 三看板，测试 21 新用例全绿 + 本地活体产证 PASS；kind 观察栈实跑待镜像网络恢复（`scripts/test-observability.sh`） |
| 4.4 | HPA 压力验证通过（消息积压→扩容→回缩）；数小时长 Run 无快照膨胀异常；出具 D/E 立项结论 |
| 4.5 | gVisor 验证结论或替代方案；OIDC 决策记录；备份恢复演练一次 PASS；发布/回滚演练一次 PASS |

## 4.5 阶段进度

### Phase 4.1 发布管道（G1）✅ 已交付（2026-08-23）

- **三镜像全仓库构建**：`deploy/images/control-plane.Dockerfile` / `runtime.Dockerfile` / 新增
  `frontend.Dockerfile`（node:22 构建 agent-ui 五 workspace → nginx:1.27 非 root uid101 托管 SPA，
  懒解析反代 `/api/agent-platform`，SSE 不缓冲；镜像自带 default.conf 可 standalone 冒烟）。
- **发布管道**：`scripts/build-images.sh`（build / --push / --update-prod-values；registry 与镜像源
  env 化；输出校验合同 `deploy/prod/image-refs.json`）；`scripts/update_image_refs.py`（仅改 golden
  values `images:` 块 6 行，保留注释与换行）。
- **git 内真 digest**：golden `deploy/prod/values.yaml`（frontend/ingress/autoscaling/secrets 生产
  合约全开）由管道每次发布写回真实 sha256；静态门 `scripts/check-k8s.sh` 新增 prod profile 断言——
  三个 Deployment 必须 digest 钉死真 sha256 且非示例 registry，占位 fail-closed（exit 78 + 指引）。
- **CI**：`image-gate` job——PR 只构建自测；main 且 `AGENT_PLATFORM_REGISTRY` 已配→推送+写回
  golden+自动 commit（digest 内容寻址，无空提交/无乒乓）。
- **实跑证据（本机）**：三镜像 build+push `localhost:5001`，golden 写回；前端镜像冒烟
  (uid 101，SPA index/asset 200/200)；`check-k8s.sh` 四 profile 全绿（prod 37 manifests 含
  Ingress/ExternalSecret/HPA/KEDA/RuntimeClass）；pytest 66+8。顺手修：agent-ui-react `chat()`
  调用点类型债（`ChatCommand` 输出态 resource_refs 必填 → 暴露 `ChatCommandInput`=z.input），
  前端全 workspace build 恢复绿。

### Phase 4.2 生产接线（G2/G3）✅ 已交付（2026-08-23）

- **G2 生产 Secret 实接**：`scripts/bootstrap-prod-wiring.sh` 幂等接线 —— 真实 External
  Secrets Operator v0.9.20 + ClusterSecretStore `enterprise-secret-store`（Vault
  provider） + dev Vault（受管 Vault/KMS 的等价占位，契约不变）+ 九键凭据 map
  （PG/NATS/S3/DeepSeek 模型 key）→ ExternalSecret（chart 契约）→ Secret
  `agent-platform-dependencies` → pod envFrom。round-trip 断言：9 键与 Vault
  值逐字节一致。实跑修复：① ExternalSecret `refreshInterval` 字符串而
  ClusterSecretStore 要整数秒（ESO CRD 校验不对等，初版写反）② 预建
  namespace/ExternalSecret 需打标准 Helm 所有权标签才能被 helm import
  ③ seed 后注解触发事件级 reconcile + 轮询。
- **G3 域名/TLS 生效**：cert-manager v1.15.3 + 演示两层 CA（`deploy/prod/tls/`）
  + Ingress 模板新增 `cert-manager.io/cluster-issuer` annotation 注入（golden
  默认 letsencrypt-prod 不变，演示门覆写 demo issuer）→ ingress-shim 签发 leaf
  （SAN=`agent-platform.e2e.local`）。验证：root/api 200 via https、leaf SAN、
  openssl 链可验（含 -verify_hostname）、SSE 反缓冲 off。
- **L3 生产形态重跑**：`scripts/test-prod-form.sh` —— golden helm 部署（host/issuer/
  沙箱运行时覆写，api 副本 2 因 kind 双 worker 上限，HA 斜度归 4.4）→ G2 注入断言
  → G3 TLS/入口断言 → 真实 Run→Attempt→SUCCEEDED（`test_attempt_job.py` 2/2）。
- **动门抓出 4 个静态门覆盖不到的缺口（已修）**：frontend 容器 PodSecurity restricted
  姿态缺失（enforce 拒 Pod）；非 root nginx bind<1024 拒绝（listen 8080 对齐
  Service/containerPort/probe）；`control-api-ingress` 未放行 control 平面内
  frontend→api 反代；`control-default-deny` 挡掉 ingress→frontend（根路径 504，
  新增 `control-frontend-ingress`）。另有：migrate hook 引用 chart 渲染 SA/
  PriorityClass 导致首次安装 hook FailCreate（hook 先于 main manifests）→
  改 default SA + 去 priority；kind 门用 kubectl apply 致 helm import 冲突 →
  接线脚本打所有权标签/先 purge；L3 客户端 `trust_env=False` 规避宿主环境对
  pf 通道干扰；pod 状态断言轮询消除 SUCCEEDED 事件早于 Pod 状态翻转的竞态。

## 5. 最短上线路径（建议顺序）

1. **G1 镜像管道**（其余项都依赖真镜像才能部署验证）
2. **G2/G3 生产接线 + L3 生产形态重跑**
3. **G5 可观测**
4. **G8 容量/长 Run**（触发 D/E 评估）
5. **G4/G6/G7 安全加固 + 备份文档**

## 6. 前端交付路线决策（2026-08-23 用户确认）

- **路线：前端 SDK/组件先行，产品页面最后做。**
- 当前交付物即**可嵌入 SDK + 组件库**（`agent-ui-protocol` 契约 / `agent-ui-client` 客户端 / `agent-ui-react` 组件），`examples/embedded-host-example` 仅是演示与联调宿主（demo 页面，非产品 UI）。
- **生产消费方式**：宿主业务系统自建页面嵌入组件（F-B 契约，经 HostBridgeCapabilities 适配）；独立托管形态（Helm frontend = nginx SPA 托管）也已就绪，供无宿主场景使用。
- **产品页面**（登录/工作台/运行列表/管理/结合业务）属独立控制台形态，**后置**：结合宿主业务与 `agent-ui-*` SDK 再做（见主 SDD Phase 4 远期清单）。
- 阶段边界：Phase 3.6 已交付「嵌入组件 + 演示宿主」；Phase 4 上线 = 三镜像（runtime/control/frontend）整体发布，不新增产品 UI 资产。