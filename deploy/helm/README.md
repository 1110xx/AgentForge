# enterprise-agent-platform Helm Chart（部署配置细则）

> 本文档按**部署清单**逐项说明本 Chart 的配置（values 逐项、resources/limits、
> 探针、HPA/KEDA、PVC、Ingress/TLS）与**无集群静态校验**方法（Phase 3.5-C 交付）。
> 架构与数据路径见 `deploy/operations.md`；故障处理见 `deploy/runbooks/`。

## 1. Chart 概览

| 项 | 值 |
|---|---|
| Chart 名称 / 类型 | `enterprise-agent-platform` / v2 application |
| 版本 | 0.2.0（appVersion 0.1.0，v0.2.0 起：resources/probes 参数化、可选中 ingress/TLS、可选 PVC、schema 全量同步、静态门加 helm lint 双 profile） |
| 目标命名空间 | `agent-platform-control`（控制面）/ `agent-platform-sandbox`（Attempt 沙箱） |
| 安装的组件 | API Deployment、Orchestrator(worker) Deployment、migrate Job(hook)、Service、HPA、KEDA ScaledObject、Namespace/SA/RBAC、NetworkPolicy、PDB、PriorityClass、ResourceQuota、可选 RuntimeClass / Ingress / PVC |
| **不安装**（外部托管依赖） | PostgreSQL、NATS JetStream、S3/MinIO、OIDC、External Secrets Operator、KEDA、OTel 后端 |

一次生产渲染生成 **33 个 manifest**（含 ExternalSecret + RuntimeClass + HPA + ScaledObject）；
kind 开发渲染 **29 个**；开启 Ingress+PVC 的扩展渲染 **31 个**。

加载顺序：`values.yaml`（默认）→ `-f` 覆盖文件 → `--set/--set-string` 命令行（最高优先）。
所有键受 `values.schema.json` 约束（`additionalProperties: false`，多余/缺必填键会直接校验失败）。

## 2. values 逐项说明

### 2.1 `images` — 镜像（强制 digest 钉死）

| 键 | 默认 | 说明 |
|---|---|---|
| `images.controlPlane.repository` | `registry.example.invalid/.../control-plane` | 控制面镜像仓库（API/worker/migrate 共用） |
| `images.controlPlane.digest` | `sha256:0000…0`（占位） | 必须替换为真实 digest；schema 强制 `^sha256:[0-9a-f]{64}$` |
| `images.runtime.repository` | `registry.example.invalid/.../runtime` | Attempt 沙箱运行时镜像仓库 |
| `images.runtime.digest` | `sha256:0000…0`（占位） | 同上；由 orchestrator 注入 `AGENT_PLATFORM_RUNTIME_IMAGE` |
| `images.frontend.repository` | `registry.example.invalid/.../frontend` | Phase 3.6 前端镜像（nginx + Vite 构建产物），仅 `frontend.enabled=true` 时使用 |
| `images.frontend.digest` | `sha256:0000…0`（占位） | 同上 |

> 生产镜像仓库与 digest 由 CI/CD 构建并注入（见 §8.5 镜像发布管道）。占位 digest
> 部署会 pull 失败 —— 这是有意的 fail-closed。仓库级 golden 值在
> `deploy/prod/values.yaml`（每次发布由 `scripts/build-images.sh --update-prod-values`
> 重写真 digest，静态门断言已钉真，占位会直接 fail）。

### 2.2 `controlPlane` — 控制面工作负载

| 键 | 默认 | 说明 |
|---|---|---|
| `controlPlane.replicas` | `3` | API Deployment 副本数（PDB 最低可用 2） |
| `controlPlane.workersReplicas` | `2` | Orchestrator Deployment 副本数（KEDA 自动扩缩目标） |
| `controlPlane.apiPort` | `8080` | API 容器端口（schema const，勿改） |
| `controlPlane.kubernetesApiCidr` | `10.96.0.1/32` | kube-apiserver 网段；tokenreview（API）与 Job 提交（worker）的 egress 白名单 |
| `controlPlane.dependencyEgressCidr` | `10.0.0.0/8` | 外部依赖网段；PG(5432)/NATS(4222)/S3(443)/OTel(4318) 的 egress 白名单 |
| `controlPlane.apiResources` | requests 250m/256Mi → limits 1/1Gi | API 容器资源（见 §3） |
| `controlPlane.workerResources` | requests 250m/256Mi → limits 1/1Gi | Orchestrator 容器资源（见 §3） |
| `controlPlane.migrationResources` | requests 100m/128Mi → limits 500m/512Mi | migrate hook Job 资源（见 §3） |
| `controlPlane.apiProbes` | httpGet `/health/ready` 与 `/health/live` | API 探针（path 与 timing，见 §4） |
| `controlPlane.workerProbes` | exec `kill -0 1` | worker 探针（见 §4） |

### 2.3 `dependencies` — 外部依赖端点

| 键 | 默认 | 说明 |
|---|---|---|
| `dependencies.postgresql.endpoint` | `postgresql+asyncpg://…` | asyncpg DSN（写进 `AGENT_PLATFORM_DATABASE_URL` secret，或由外部 secret 提供） |
| `dependencies.nats.endpoint` | `tls://nats.example.invalid:4222` | NATS 连接串（`AGENT_PLATFORM_NATS_URL`） |
| `dependencies.nats.monitoringEndpoint` | `https://…:8222` | NATS 监控端点（KEDA JetStream lag 触发器用） |
| `dependencies.nats.stream` | `AGENT_PLATFORM` | JetStream stream 名（`AGENT_PLATFORM_NATS_STREAM`） |
| `dependencies.nats.streamReplicas` | `3` | stream 副本数（schema const 3；单节点 kind 用 secret 覆盖为 1） |
| `dependencies.s3.endpoint` | `https://objects.example.invalid` | 工件存储 S3 端点 |
| `dependencies.s3.bucket` | `agent-artifacts` | 工件 bucket |
| `dependencies.oidc.issuer` | `https://identity.example.invalid` | OIDC 签发方（生产鉴权接入点） |

### 2.4 `secrets` — 凭据注入（ExternalSecret 合约）

| 键 | 默认 | 说明 |
|---|---|---|
| `secrets.externalSecretContract` | `true` | 是否渲染 `ExternalSecret` 契约对象（kind 关闭，由 bootstrap 脚本直接建 Secret） |
| `secrets.externalSecretName` | `agent-platform-dependencies` | 目标 Secret 名（envFrom/secretKeyRef 引用） |
| `secrets.storeRefName` | `enterprise-secret-store` | ExternalSecret 引用的 store 名 |
| `secrets.storeRefKind` | `ClusterSecretStore` | store 种类（`SecretStore`/`ClusterSecretStore`） |
| `secrets.remoteKey` | `.enterprise-agent-platform/production` | 远端密钥管理器中的整段键（dataFrom.extract） |

**Secret 必须包含的键**（worker/envFrom 与镜像内代码读取）：

| Secret 键 | 用途 | 消费方 |
|---|---|---|
| `AGENT_PLATFORM_DATABASE_URL` | 共享 durable store（API worker 必须同库，否则静默分叉） | api / worker / migrate |
| `AGENT_PLATFORM_NATS_URL` | 消息总线 | worker（relay/调度） |
| `AGENT_PLATFORM_NATS_STREAM` | JetStream stream 名 | worker |
| `AGENT_PLATFORM_NATS_STREAM_REPLICAS` | stream 副本数（生产 3 / kind 单节点 1） | worker |

> 宿主业务侧的 adapter（如 `reference.k8s_container:create_container`）可能还需
> 模型密钥、OIDC 发现等环境变量；本 Chart 只保证自带四键，其余以 host factory
> 契约为准。`AGENT_PLATFORM_STORE=memory` 是仅本地演示逃生门，生产不允许。

### 2.5 `integration` — 宿主适配器工厂

| 键 | 默认 | 说明 |
|---|---|---|
| `integration.containerFactory` | `enterprise_agent_platform.reference.k8s_container:create_container` | API 进程的容器工厂（`module:callable`，schema 校验格式） |
| `integration.workerFactory` | `enterprise_agent_platform.execution.k8s_worker:run_worker` | worker 进程工厂（同上） |

对应代码 env：`AGENT_PLATFORM_CONTAINER_FACTORY` / `AGENT_PLATFORM_WORKER_FACTORY`。

### 2.6 `sandbox` — Attempt 沙箱

| 键 | 默认 | 说明 |
|---|---|---|
| `sandbox.namespace` | `agent-platform-sandbox` | Attempt Job/Pod 命名空间（schema const） |
| `sandbox.serviceAccountName` | `agent-platform-sandbox` | 沙箱 SA（无令牌挂载，schema const） |
| `sandbox.runtimeClassName` | `agent-platform-gvisor` | 沙箱 RuntimeClass（空串 = 不渲染 RuntimeClass 且 Job 不带 runtimeClass；kind 用空串） |
| `sandbox.maxActiveAttempts` | `100` | ResourceQuota：`pods` 与 `count/jobs.batch` 上限 |

### 2.7 `autoscaling` — 弹性

| 键 | 默认 | 说明 |
|---|---|---|
| `autoscaling.enabled` | `true` | 总开关（关闭则不渲染 HPA 与 ScaledObject） |
| `autoscaling.minReplicas` | `2` | HPA/ScaledObject 最小副本 |
| `autoscaling.maxReplicas` | `20` | 最大副本 |
| `autoscaling.pendingMessagesPerReplica` | `"5"` | KEDA `lagThreshold`（NATS 待消费条数/副本） |
| `autoscaling.apiCpuAverageUtilization` | `70` | API HPA 的 CPU 目标利用率（%） |

### 2.8 `ingress` — 对外暴露（可选，默认关）

| 键 | 默认 | 说明 |
|---|---|---|
| `ingress.enabled` | `false` | 是否渲染 Ingress（默认仅 ClusterIP + port-forward） |
| `ingress.className` | `nginx` | IngressClass 名 |
| `ingress.host` | `agent-platform.example.invalid` | 主机名（必填，schema minLength） |
| `ingress.annotations` | `{}` | 自定义注解（如限流、WAF、OIDC 注入） |
| `ingress.tls.enabled` | `false` | 是否挂 TLS 段 |
| `ingress.tls.secretName` | `agent-platform-api-tls` | TLS Secret 名（现成 secret，或 cert-manager 签发目标） |
| `ingress.tls.clusterIssuer` | `""` | 非空时建议配合 annotation 自动签发（示例见 §6） |

路由：`/api/agent-platform`（Prefix）→ `agent-platform-api` Service http 端口。
NetworkPolicy `control-api-ingress` 已放行 `ingress-nginx` 命名空间访问 8080。

### 2.9 `persistence` — 本地暂存（可选，默认关）

| 键 | 默认 | 说明 |
|---|---|---|
| `persistence.localScratch.enabled` | `false` | 是否渲染 PVC 并让 API 的 `/tmp` 挂载该 PVC |
| `persistence.localScratch.storageClassName` | `""` | 存储类（空 = 集群默认） |
| `persistence.localScratch.size` | `10Gi` | 容量（schema 校验 K8s 数量格式） |

## 3. resources / limits 部署细则

**设计原则**：控制面无本地持久状态（全部状态在外部 PostgreSQL），因此资源是线性小水位；
沙箱 Attempt 的算力由 `ResourceQuota`（100 Pods / 200Gi limits）在命名空间级封顶。

| 工作负载 | requests | limits | 说明 |
|---|---|---|---|
| API（每副本） | 250m CPU / 256Mi | 1 CPU / 1Gi | FastAPI + 调度器相邻进程；SSE 长连按连接数线性增长 |
| Orchestrator（每副本） | 250m CPU / 256Mi | 1 CPU / 1Gi | 轮询式调度 + NATS relay；**纯等待时 CPU 极低**，KEDA 按队列 lag 扩缩，资源可以给小 |
| migrate Job | 100m CPU / 128Mi | 500m CPU / 512Mi | 一次性迁移；`activeDeadlineSeconds 600`，`backoffLimit 2` |
| 沙箱 Job（Attempt） | 不在此 Chart 内（由 orchestrator 构造） | — | 受 `agent-platform-sandbox` ResourceQuota 约束（pods/sandbox.maxActiveAttempts） |

调整方法：改 `controlPlane.apiResources / workerResources / migrationResources`
（requests/limits 的 `cpu`/`memory` 四个子键），schema 自动校验。容量与成本规划见
`deploy/operations.md`。

## 4. readiness / liveness 探针

| 工作负载 | readiness | liveness | 说明 |
|---|---|---|---|
| API | HTTP GET `api/agent-platform/v1/health/ready`，延迟 3s，周期 5s | HTTP GET `.../health/live`，延迟 10s，周期 10s | 端点由 `platform/entrypoint.py` 提供（`ready`/`live`），路径可用 `controlPlane.apiProbes.*.path` 覆盖 |
| Orchestrator | **exec** `/bin/sh -c "kill -0 1"`，延迟 3s，周期 10s | 同命令，延迟 10s，周期 10s | worker 无 HTTP 服务，用 exec 探针确认进程存活；可用 `controlPlane.workerProbes.*.command` 替换为自定义检查（例如探测器写 readiness 文件） |
| migrate Job | — | — | Job 型任务不做探针；由 hook 失败/`backoffLimit` 控制 |

探针全部参数（initialDelay/period/timeout/failureThreshold）在
`controlPlane.apiProbes` / `controlPlane.workerProbes` 下逐项可配，schema 校验
`timeout≥1, period≥1, failureThreshold≥1`。

## 5. HPA / KEDA 扩缩细则

- **API：HPA（autoscaling/v2）**，CPU 利用率目标 `apiCpuAverageUtilization`（默认 70%），
  `scaleDown.stabilizationWindowSeconds: 300` 防抖动；min/max 由 `autoscaling.*` 控制。
- **Orchestrator：KEDA ScaledObject**，触发器 `nats-jetstream`：
  `natsServerMonitoringEndpoint=dependencies.nats.monitoringEndpoint`、
  `stream=dependencies.nats.stream`、consumer `agent-platform-orchestrator`、
  `lagThreshold=pendingMessagesPerReplica`（默认每副本 5 条待消费即扩 1 副本）、
  `cooldownPeriod 300`。**依赖 KEDA 已安装且 NATS 监控端点可达**，与
  `workersReplicas` 为互斥关系（KEDA 接管后 `workersReplicas` 作为初始值）。

## 6. Ingress / TLS

开启：`--set ingress.enabled=true --set ingress.host=agent.example.com`。

TLS 两种接入方式：
1. **cert-manager 自动签发**：`--set ingress.tls.enabled=true --set ingress.tls.clusterIssuer=letsencrypt-prod`
   并加 annotation（chart 不强制注入，常见组合示例）：
   ```yaml
   ingress:
     annotations:
       cert-manager.io/cluster-issuer: letsencrypt-prod
       nginx.ingress.kubernetes.io/backend-protocol: HTTP
   ```
2. **现成 TLS Secret**：`--set ingress.tls.secretName=my-tls-secret`（secret 需含
   `tls.crt`/`tls.key`，与 host 匹配）。

默认关闭的原因：控制面 API 是**内部网关**（Sandbox Pod 经 Internal API 反向代理），
对外只暴露公网 API 路径；生产按网络拓扑（入口控制器 / WAF / mTLS 网格）选择暴露方式。

Phase 3.6：`frontend.enabled=true` 时（也要求 `ingress.enabled=true`）Ingress 规则
增加第二条 path —— `path: /`（Prefix）→ `agent-platform-frontend:http`；同一规则
`/api/agent-platform`（Prefix）仍指向 `agent-platform-api:http`，nginx-ingress 按最长
路径前缀分流，API 与静态站点互不干扰。SSE 反缓冲：该 profile 下 Ingress
annotations 注入 `nginx.ingress.kubernetes.io/proxy-buffering: "off"`（用户 annotations
会被保留并叠加）。

### 2.10 `frontend` — 前端工作负载（Phase 3.6，可选，默认关）

| 键 | 默认 | 说明 |
|---|---|---|
| `frontend.enabled` | `false` | 开启后额外渲染 Deployment/Service/ConfigMap，并在 Ingress 加根路径 |
| `frontend.replicas` | `2` | 静态 nginx 副本数 |
| `frontend.resources` | `requests 50m/64Mi`，`limits 250m/256Mi` | 静态站点 + 反代，nginx 极轻量 |
| `frontend.probes` | `path=/`（ready/live） | 探针打在 index 上；`http://svc/` 必须 200 才 Ready |
| `frontend.nginx.staticRoot` | `/usr/share/nginx/html` | 镜像内 Vite 构建产物目录 |
| `frontend.nginx.apiServiceName` / `apiPort` | `agent-platform-api` / `8080` | 内嵌反代的 API Service 目标 |
| `frontend.nginx.proxyReadTimeoutSeconds` | `3600` | SSE 长连接读超时 |

工作方式：浏览器只连前端站点；SDK 的 `/api/agent-platform/*` 请求由内嵌 nginx
反代到 API Service（`proxy_buffering off` 保 SSE 流）。Ingress 上 `/api/agent-platform`
仍是**最长路径优先**直通 API Service，`/` 根路径进前端；Ingress 层同步注入
`nginx.ingress.kubernetes.io/proxy-buffering: "off"`。

前端镜像（nginx + `dist`）不在本仓库构建，由宿主 CI/CD 产出后注入
`images.frontend.*`（与迁移 Job 相同的镜像注入流程）。

## 7. PVC

默认**不创建** PVC：控制面状态全部持久化在外部 PostgreSQL，`/tmp` 仅是
`readOnlyRootFilesystem: true` 容器需要的可写暂存（默认 emptyDir `sizeLimit 128Mi`）。

可选 `persistence.localScratch.enabled=true` 时：
- 渲染 `PersistentVolumeClaim agent-platform-local-scratch`（RWO，`size`，可指定 storageClass）；
- API 的 `/tmp` 改为挂载该 PVC（worker 仍用 emptyDir —— dispatch 是短暂的）。

**边界（务必阅读）**：RWO PVC 同一时刻只能被**一个节点**挂载，`replicas>1` 时多副本
会挂起等待。适用场景：单副本 + 需要跨 Pod 重启保留 `/tmp` 调试产物/暂存工件；
多副本需要 RWX 存储类或保持 emptyDir。**不要用 PVC 承载业务状态** —— 唯一事实源是 2.3 的外部 PostgreSQL。

## 8. 安装 / 升级 / 回滚 / 卸载

```bash
# 前置：External Secrets Operator + 目标 Secret 就绪（或 secrets.externalSecretContract=false 自行建 Secret）；KEDA（如需 autoscaling.enabled=true）

# 安装（migrate Job 在 pre-install hook 自动执行 alembic upgrade head）
helm install enterprise-agent-platform deploy/helm \
  --namespace agent-platform-control --create-namespace \
  --values my-prod-values.yaml

# 升级（pre-upgrade 再跑 migrate hook，Job 幂等）
helm upgrade enterprise-agent-platform deploy/helm \
  --namespace agent-platform-control --values my-prod-values.yaml

# 回滚
helm rollback enterprise-agent-platform <revision> --namespace agent-platform-control

# 卸载（RBAC/NetworkPolicy/PriorityClass 等随 chart 删除；外部依赖不动）
helm uninstall enterprise-agent-platform --namespace agent-platform-control
```

安装后 `helm status / helm get values` 可查生效配置；`helm list -A` 查看版本。
注意 migrate hook 是 `before-hook-creation,hook-succeeded` 删除策略 —— 升级时旧 Job
自动重建，**不要**在集群里手工创建同名 Job。

**生产一键部署（Phase 4.1 G1，镜像已发布后）**：

```bash
# deploy/prod/values.yaml 是本仓库的 golden 生产值：镜像按真 sha256 digest 钉死，
# frontend/ingress/autoscaling/secrets 合约全开。改名 host 域名后直接安装：
helm upgrade --install agent-platform deploy/helm --namespace agent-platform-control \
  --create-namespace -f deploy/prod/values.yaml
```

## 8.5 镜像发布管道（Phase 4.1 G1）

三个生产镜像全部由本仓库构建（不依赖宿主编译）：

| 镜像 | Dockerfile | 内容 |
|---|---|---|
| control-plane | `deploy/images/control-plane.Dockerfile` | API + orchestrator + migrate（uv 同步，非 root 65532） |
| runtime | `deploy/images/runtime.Dockerfile` | Attempt 沙箱 Runner（pi-agent-core 运行时） |
| frontend | `deploy/images/frontend.Dockerfile` | nginx 非 root SPA（agent-ui 五 workspace 构建 + 反代 /api/agent-platform，SSE 不缓冲） |

```bash
# 构建（不推送）
scripts/build-images.sh
# 构建 + 推送到 AGENT_PLATFORM_REGISTRY（默认 localhost:5001；带用户名/密码自动 docker login）
scripts/build-images.sh --push
# 构建 + 推送 + 把真 digest 写回 deploy/prod/values.yaml（golden 文件，可提交）
scripts/build-images.sh --push --update-prod-values
```

环境变量：`AGENT_PLATFORM_REGISTRY` / `AGENT_PLATFORM_REGISTRY_USERNAME` /
`_PASSWORD`（docker login）/ `AGENT_PLATFORM_IMAGE_TAG` / `UV_INDEX_URL`、
`NPM_REGISTRY_URL`（镜像源覆盖）。产物 `deploy/prod/image-refs.json`（GitOps 工具可读）。

CI（`.github/workflows/ci.yml` `image-gate`）：PR 只构建自测；main 且配置了
`vars.AGENT_PLATFORM_REGISTRY` 时构建+推送+更新 golden 值并自动提交
（digest 内容寻址：内容不变 digest 不变 → 不产生空提交，不会乒乓）。

## 9. 无集群静态校验（helm template / lint）

本 Chart 的静态门**不需要真实集群**：

```bash
scripts/check-k8s.sh
```

执行内容（无集群、无网络）：

| 步骤 | 命令 | 校验点 |
|---|---|---|
| 1 | `helm lint deploy/helm` | 结构 + **values 对 `values.schema.json` 校验**（多余键/缺必填/格式 pattern 会 fail） |
| 2 | `helm lint deploy/helm --values deploy/kind/values.yaml` | kind 覆盖 profile 同样过 schema |
| 3 | `helm template … --values deploy/kind/values.yaml …` | kind 渲染（29 manifest） |
| 4 | `helm template … --values deploy/prod/values.yaml` | 生产渲染（默认值 + golden 覆盖 = 37 manifest；**断言三 Deployment 均为真 sha256 digest 钉死**，占位/示例 registry 会 fail） |
| 5 | `helm template … --set ingress.enabled=true … --set persistence.localScratch.enabled=true` | 扩展渲染（31 manifest，覆盖 Ingress/PVC 分支） |
| 6 | Python `yaml.safe_load_all` | 全部渲染 manifest 可解析、kind 统计 |

手动快速验证（本机无 helm 也可读 schema 对拍）：
```bash
helm lint deploy/helm --set bogusKey=true        # 应失败（additionalProperties）
helm lint deploy/helm --set persistence.localScratch.size=HUGE   # 应失败（pattern）
helm template enterprise-agent-platform deploy/helm | kubectl --dry-run=client -f -    # 可选：kubectl client-side 校验
```

## 10. 与 kind 开发环境的关系

`deploy/kind/values.yaml` 是**开发 profile**（非生产值）：digest 占位由
`scripts/test-kind.sh` 注入真实镜像 digest；`externalSecretContract: false`
（bootstrap 脚本直建 Secret）；`runtimeClassName: ""`（kind 无 gVisor）；
`autoscaling.enabled: false`；`replicas 1/1`。生产部署**必须**使用完整的
生产 values（含真实 digest、R3 NATS、`agent-platform-gvisor` RuntimeClass —— 安装前
确认节点存在 `runsc` handler）。

---

> 相关文档：`deploy/DOCS.md`（索引）、`deploy/operations.md`（数据路径与容量）、
> `deploy/runbooks/`（故障处理）、SDD.md §14 Phase 3 / 3.5。