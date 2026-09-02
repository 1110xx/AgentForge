## 文档索引

| 文档 | 说明 |
| --- | --- |
| architecture.md | 统一执行模型、控制面/执行面、状态恢复、数据流与未来多 Agent 扩展。 |
| security.md | Sandbox、身份、网络、Tool、WRITE Effect、A2UI、多租户与剩余风险边界。 |
| implementation.md | 当前代码模块、公开 API、持久化、部署资产、已知缺口和 L1-L4 证据边界。 |
| embedding-guide.md | 通过 Host Ports、public HTTP、SSE、前端 SDK 和 adapter 接入现有业务系统。 |
| migration-guide.md | 从业务系统内嵌 Agent 迁移到独立平台的分阶段、cutover 和 rollback 方案。 |
| task-12-report.md | MessageBus、Telemetry、部署与平台运维任务的带日期交付证据快照。 |
| task-13-report.md | 独立交付、公共装配、portability、wheel 与统一 L1/L2/L3 验证证据。
| sdd-followup-mode.md | 追问链专属 SDD：FollowupPanel/useFollowupHistory/追问端点与公共完成器。
| sdd-frontend-dual-mode.md | 前端双模式设计（live 真后端 / demo 内嵌 mock）与 SDK 分层。
| sdd-frontend-launcher.md | Phase 3.6 前端入口 SDD：自由对话端点、AgentLauncher/浮窗、示例重建、CI+Helm 应用层（F-A..F-E）。 |
| go-live-plan.md | 生产上线 Go-Live 差距与分阶段计划（Phase 4 立项）：G1-G8 差距清单、分阶段验收、最短上线路径。 |
| phase-4.5-security-decisions.md | Step 1 鉴权层决策与证据：HMAC capability tokens（rt.v1 五键契约）+ OIDC opt-in（RS256 手工验证）、伪造明文 401 负向、集群 e2e。 |
| phase-5-data-plane.md | Step 2 数据面证据：WAL 归档/PITR 双 target 精度（113/93 窗口）、定时 basebackup、26h 新鲜度 watchdog（告警路实测）、TTL 受管 SQL。 |
| phase-5-supply-chain.md | Step 3 供应链证据：GHCR CI 原生发布（GITHUB_TOKEN）、GitOps golden 回写、三包 public 匿名可验、LE issuer 就绪物。 |
| production-onprem-topology.md | Step 4 生产/on-prem 迁移拓扑与加固：依赖组件 HA 形态（Patroni/NATS cluster/MinIO erasure/Vault Raft）、gVisor 落地计划、隔离网络镜像方案、接线顺序与验收。 |

## 目录内容

无（本目录只包含上表长期文档和交付报告）。
