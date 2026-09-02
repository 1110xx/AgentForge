# 生产部署拓扑与 On-Prem 迁移计划（Step 4，纯文档）

> 2026-09-02 · 对应 MEMORY 计划 Step 4：在 Step 1-3（鉴权 / 数据面 / 供应链）已实跑的
> 证据之上，把「离生产上线还差什么、按什么顺序接、每步怎么验收」写成单一权威文档。
> **本文件零代码变更**：引用的全部资产、门与 runbook 均已存在并实跑（证据锚点见各节）。

## 1. 目标与边界

- 目标读者：接手把平台部署到**自建/私有（on-prem）集群**或云集群的工程师。
- 平台已冻结的组件（**不要**因部署而改）：
  - 三主镜像：`ghcr.io/1110xx/agentforge/enterprise-agent-platform/{control-plane,runtime,frontend}`
    （golden digest 见 `deploy/prod/image-refs.json`；内容寻址，registry 无关）。
  - 控制面机制：Run/Attempt/Lease/Checkpoint/EffectLedger/RunEvent（facts 在 PostgreSQL）、
    NATS outbox→wakeup 投递、HMAC runtime tokens + OIDC 可选（Step 1）。
  - 数据面机制：WAL 归档 PITR 恢复、定时 basebackup、新鲜度 watchdog、TTL 受管 SQL（Step 2）。
  - 发布机制：main 推送 → CI `image-gate` 用 `GITHUB_TOKEN` 推 GHCR + GitOps 回写 golden（Step 3）。
- 本文件不做的事：不在生产环境改运行时代码；不给具体发行版逐条清单；不代替各 runbook。

## 2. 现状快照（2026-09-02，kind 全链 + CI 实跑）

| 面 | 现状 | 证据锚点 |
| --- | --- | --- |
| 鉴权 | HMAC `rt.v1.*` capability tokens（5 键契约）+ OIDC opt-in（RS256 手工验证） | `docs/security.md`、`docs/phase-4.5-security-decisions.md`；集群 e2e：伪造 token 401 负向 + 真实 Run SUCCEEDED |
| 供应链 | 三镜像 GHCR **public**（CI 实发 run 33619097309；GitOps commit `a4e0352`；匿名 manifest inspect 三包 OK） | `docs/phase-5-supply-chain.md`、`deploy/prod/image-refs.json` |
| Secret | External Secrets Operator + ClusterSecretStore（Vault provider）九键 round-trip 断言 | `deploy/prod/eso/`、`scripts/bootstrap-prod-wiring.sh`（kind Vault = 受管 Vault/KMS 的等价占位） |
| TLS | 集群内 demo 两层 CA 全链已验；**LE staging/prod ClusterIssuer 就绪待域名** | `deploy/prod/tls/letsencrypt-issuer.yaml`、`deploy/helm/README.md` |
| 数据面 | WAL archive+PITR 双 target 精度实证（113/93 窗口）；每日 basebackup→MinIO；26h 新鲜度 watchdog；周日 TTL | `docs/phase-5-data-plane.md`、`deploy/runbooks/backup-and-ttl.md`、`deploy/data-plane/ttl_maintenance.sql` |
| 观测 | OTLP collector→Tempo/Prometheus + promtail→Loki + 三看板 + 15 告警 | `deploy/observability/README.md` |
| 执行安全 | runtime uid 65532 非 root；netpol default-deny + 定向放行；RuntimeClass 模板 `agent-platform-gvisor`（handler runsc）；PDB/HPA/PriorityClass 模板齐 | `deploy/helm/templates/{networkpolicies,runtimeclass,pdb,hpa,priorityclass}.yaml` |

## 3. 生产拓扑（on-prem，3 个依赖节点 ×2 工作节点起）

```text
┌───────────────────────── 交付面 ─────────────────────────┐
│  GitHub main ─CI image-gate─▶ GHCR(public) ◀─离线镜像 Harbor/mirror  │
│  GitOps（bot 提交 golden digest）  helm upgrade --values deploy/prod/values.yaml │
└──────────────────────────────────────────────────────────┘
┌───────────────────────── 控制面（≥2 副本，PDB）────────────┐
│ api / orchestrator / frontend / workers / migration-job    │
│  ← ESO 注入 Secret（Vault） · TLS 到 ingress-nginx(cert-manager) │
└──────────────────────────────────────────────────────────┘
┌───────────────────────── 数据与依赖面（HA）────────────────┐
│ PostgreSQL 3 节点(Patroni)  ←WAL archive→  MinIO(erasure, agent-backups) │
│ NATS 3 节点 JetStream(R>1)     MinIO(agent-artifacts 版本化+lifecycle)   │
│ Vault 3 节点 Raft（或受管 KMS）   observability 栈持久化卷              │
└──────────────────────────────────────────────────────────┘
┌───────────────────────── 执行面（sandbox ns）──────────────┐
│ Attempt→K8s Job → RuntimeClass agent-platform-gvisor(runsc)   │
│ netpol default-deny · egress 白名单(模型 API 出口) · uid 65532  │
└──────────────────────────────────────────────────────────┘
```

把 `kind` 形态的差异只保留在「依赖实例数 / 持久化 / 域名」三处，其余资产直接复用。

## 4. 依赖组件生产形态（逐项：现状 → 建议 → 关键点 → runbook）

### 4.1 PostgreSQL（最优先）
| 项 | 值 |
| --- | --- |
| 现状 | 外部 PG（kind 用官方镜像单实例 PVC）；Step 2 已实证 archive+PITR 全链路 |
| 建议 | **3 节点 Patroni**（或 streaming+repmgr）；独立数据盘；WAL 与数据分盘 |
| 关键点 | `archive_mode=on` + archive_command 直写 MinIO（复用 Step 2 机制，改配置不改镜像）；pg_hba 只放专用备份角色 replication；TLS（verify-full）；`max_wal_senders`/`archive_timeout` 按写入率 |
| 验收 | `scripts/apply-data-plane.sh` 在 prod profile 的等价体 + 季度 PITR 演练（锚点、数值算法见 `docs/phase-5-data-plane.md` §钻取） |
| runbook | `deploy/runbooks/postgresql-failover.md`、`disaster-recovery.md`、`backup-and-ttl.md` |

### 4.2 备份/DR 参数建议（把 kind 常量提到生产档）
| 项 | kind（已实跑） | 生产建议 | 说明 |
| --- | --- | --- | --- |
| basebackup | 每日 02:00 | 每 6h 或按 WAL 量 | cron `platform-base-backup` 参数化 |
| watchdog 新鲜度 | 26h（小时级告警） | 按 RPO（建议 ≤ base 间隔×2+1h） | 超时无新 base → **Job Failed=告警**（机制已在告警路实测触发） |
| 备份保留 | 14d `mc rm --older-than` | 按合规（PITR 窗口 = 保留期） | 版本化 + lifecycle（`deploy/config/minio-lifecycle.json`） |
| TTL | 每周日 30d（dry-run 先行） | 事件/过期 outbox/幂等按 retention_days；**事实面永不 TTL** | 单源 SQL `deploy/data-plane/ttl_maintenance.sql` |
| 红线段 | — | 手工恢复演练前先 `-v dry_run`；恢复目录 `chmod 700`+`touch recovery.signal` 顺序 | `docs/phase-5-data-plane.md` |

### 4.3 NATS
- 现状：JetStream 单节点（conf：`deploy/config/nats.conf`，2Gi file store）。
- 生产：**3 节点 cluster，JetStream R>1**（stream/consumer 副本 ≥2），TLS，store 按积压容量扩。
- 关键：outbox 是事实源，NATS 通知丢失不丢业务事实（设计已保证）→ 重建即可，不必追求强一致。
- runbook：`deploy/runbooks/nats-rebuild.md`。

### 4.4 MinIO（对象存储）
- 现状：单实例 + 版本化 `agent-artifacts` + lifecycle（30d noncurrent）+ Step 2 的 `agent-backups`。
- 生产：**分布式 erasure（≥4 节点，EC≥4:2）**；两 bucket 策略分开（工件版本化；备份 bucket 保留期匹配 PITR 窗口）；kms/sse。
- 验收：写坏一块盘无感知读（erasure 自愈）；备份/工件读写走同一 S3 契约（botocore 已验）。

### 4.5 Vault + ESO（Secret 面）
- 现状：kind dev Vault + ClusterSecretStore + 九键 round-trip（含 DeepSeek 模型 key 生产注入路径）。
- 生产：Vault **3 节点 Raft + 自动 unseal**（或受管 KMS）；ESO `refreshInterval` 用整数秒（历史坑已记）；凭据轮换见 `deploy/runbooks/credential-rotation.md`。
- 红线：平台无「内置管理员」回退（架构边界）；capability key 由 Vault 提供，**代码只信 env 注入**。

### 4.6 cert-manager / TLS
- 现状：demo 两层 CA 全链已验（root/api 200、leaf SAN、链可验、SSE off）；`deploy/prod/values.yaml` golden 已是 `clusterIssuer: letsencrypt-prod`。
- 生产：应用 `deploy/prod/tls/letsencrypt-issuer.yaml`（填 ACME email；HTTP-01 需公网可达端点，或 DNS-01 用私有集群）→ 等域名（**外部付费项，待用户决策**）。
- 内部流量：ingress-nginx 双向 TLS / 服务网格（可选后续，不阻塞上线）。

### 4.7 观测
- 现状：collector/Tempo/Prometheus/Loki/Grafana/promtail + 15 告警 + 三看板（kind 栈）。
- 生产：全部持久化卷 + 保留策略（Tempo/Loki 按容量、Prometheus 2w-1m）；告警集 correctness/SLO/业务三族已分（`deploy/observability/prometheus-rules*.yaml`）。

## 5. 节点与集群加固（on-prem 清单，发行版无关）

1. **节点镜像最小化**：只装 kubelet/containerd/所需驱动；关闭非必要服务与 root 远程登录；SSH key-only。
2. **系统层**：自动安全更新（含 kernel livepatch 策略）；防火墙仅开 API 端口与 egress 白名单；
   审计开启（登录/进程/文件敏感目录）；磁盘 LUKS 加密 + k8s secret 静态加密（etcd 加密开）。
3. **集群层**：RBAC 最小化（运行账号零 cluster-admin）；命名空间 PSA `baseline`→sandbox 力争 `restricted`
   （frontend 曾因 restricted 姿态缺失被拒——教训见 `docs/go-live-plan.md` Phase 4.2）；PodSecurity
   admission 强制；`NetworkPolicy` default-deny（chart 已带，kind 门在跑）；control 与 sandbox 分 namespace。
4. **eDress/出口**：模型 API 等外部调用走 NAT/代理白名单，sandbox egress 默认关闭（chart netpol 定向放行）。
5. **控制面**：etcd 独立盘 + 备份；apiserver 审计日志归档。

## 6. gVisor RuntimeClass 落地计划（G4 后置项）

- 现状：`sandbox.runtimeClassName: agent-platform-gvisor` 已是 golden 值；chart 渲染 RuntimeClass
  `handler: runsc`（`deploy/helm/templates/runtimeclass.yaml`）；kind 节点**不能**跑 runsc → 从未在集群验证。
- 计划（上线后灰度，不阻断 MVP）：
  1. 在 ≥1 工作节点安装 runsc + containerd 注册（gVisor 官方 rpm/deb 或自建）。
  2. 先导一个 sandbox 灰名单：Attempt Job 打 `runtimeClassName`（chart 已参数化）跑通
     Step1 真实 Run e2e（`test_attempt_job.py` 同款）+ 网络策略 + 非 root uid 65532 兼容。
  3. 验收结论 = 性能/兼容性数据 + 是否全量切换决策；不满足则退回 runc + 更强 netpol（替代方案已备）。
- 红线：RuntimeClass 默认不切（golden 保持），用 worker 侧注入控制灰度，回滚只删 RuntimeClass 注解。

## 7. 供应链在 on-prem/隔离环境的落地

- **可联网**：GHCR public → 节点/集群直接拉（registry 名已在 golden，`imagePullPolicy` 用 digest 钉死）。
- **隔离内网**：`GHCR → 自建 registry( Harbor/nexus ) mirror 代理或拉取缓存`；golden 的 repository
  前缀换成内网 registry 域名（digest 不变 = 同物），`scripts/build-images.sh` 的 env 支持该切换；
  GitOps 回写逻辑不变（Step 3 已把「registry 从环境变量注入」改成 workflow 内联 GHCR，内网改一行
  `AGENT_PLATFORM_REGISTRY` 即回退旧路径）。
- **更新流程**：改代码 push main → CI 构建推 GHCR + 回写 digest（自动提交，无乒乓）→
  `helm upgrade --install agent-platform deploy/helm -f deploy/prod/values.yaml`（或 ArgoCD/Flux 指向该 commit）。
- **升级次序**：migration Job（自动 hook）→ api/orchestrator（滚动+PDB）→ workers → frontend；
  回滚 = 切回上一 digest commit（内容寻址保证可复现）。

## 8. 接线顺序与验收（每步一条命令/门）

| 步骤 | 动作 | 验收 |
| --- | --- | --- |
| 0 前置 | 域名（外部购买项）、≥2 依赖机×2 工作机、块盘与加密、DNS | 节点 ready；etcd/audit 检查过 |
| 1 依赖 | PG 3 节点 Patroni + archive 通 MinIO；NATS 3 节点；MinIO erasure；Vault Raft | 三组件健康端点绿 |
| 2 Secret | ESO + ClusterSecretStore + 九键 | `scripts/bootstrap-prod-wiring.sh` 同款 round-trip 断言通过 |
| 3 发布 | helm upgrade golden（GHCR digest + LE issuer 切 prod） | `scripts/check-k8s.sh` prod profile 全绿 + `helm template` 静态门 |
| 4 动门 | 生产形态 L3（真实域名 host） | `scripts/test-prod-form.sh`（host/issuer 覆写为真域名）全绿 |
| 5 数据面 | 定时备份/watchdog/TTL 上线 + 参数化 | `scripts/apply-data-plane.sh` 等价体 + 首次真实 PITR 演练 PASS |
| 6 观测 | observability 栈持久化 | `scripts/test-observability.sh` + 告警触达演练 |
| 7 gVisor | 灰名单 → 全量决策 | 见 §6 |
| 8 DR | 季度 PITR + 故障切换 + 凭据轮换演练 | 三 runbook 各一次 PASS，记录时间线 |

## 9. 剩余外部依赖与成本（唯一决策项）

| 项 | 说明 |
| --- | --- |
| **域名 + LE 实出证** | ~$1-3/年（Step 3/4 唯一付费项）；接线物全部就绪，等用户购买后按 §4.6/§8 接线 |
| gVisor 兼容性评估 | 需真实节点装 runsc 跑灰度（§6），属验收项非成本项 |
| 依赖机数量 | 3 依赖节点 × ≥2 工作节点（可小规模起步再横向扩） |

## 10. 与云托管的可移植性

同一份 golden + chart：把 PG/NATS/MinIO/Vault 换成云托管（RDS/托管 JetStream/S3/KMS）时，
只改 SecretStore provider 与 endpoint 配置，容器面零改动——文档通用于 on-prem 与云。
