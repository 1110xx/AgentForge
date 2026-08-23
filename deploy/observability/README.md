# Observability Stack（Phase 4.3 / G5）— OTLP 接线 + 告警 + 日志集中化

> 一次 Run 从提交到终态全程可追踪、平台整体可度量、告警→下钻→定位一条线走通
> （设计定稿见 SDD §G.4）。本文档说明接线方式、堆栈组件、三块看板、告警规则集
> 与验证方法。

## 1. 数据路径

```
API / Orchestrator / Runner Pod
   ├─ OTLP HTTP (4318) ───────────────► agent-platform-otel-collector
   │   （AGENT_PLATFORM_OTLP_ENDPOINT）     ├─ traces  → otlp/tempo ─► Tempo（4317）
   │                                       └─ metrics → prometheus:9464（+ 属性脱敏）
   └─ 结构化 JSON 日志（stdout）───► promtail DaemonSet ──► Loki（3100）
        （AGENT_PLATFORM_JSON_LOGS=1，含 trace_id/run_id/attempt_id）
Grafana（3000）统一面板：Prometheus（指标）/ Tempo（trace 瀑布）/ Loki（日志）
```

- **traces ↔ logs 一 key**：API 请求用 `traceparent` 派生 `trace_id`（`RequestContext.trace_id`
  与日志 `trace_id` 同一来源）；runner Pod 侧 `runtime.bootstrap`/`checkpoint.commit` 等
  span 带 `agent.platform.run.id`/`attempt.id` 属性，日志同一字段——按 `run_id` 检索即
  得到该 Run 的 span 集与日志行，Tempo 瀑布按共享 `trace_id` 还原 Attempt 生成周期。
- **敏感字段脱敏**：collector `attributes/redact` 处理器删除 authorization 头、
  agent.prompt、tool.payload、user.id、URL query（`deploy/observability/otel-collector.yaml`）。

## 2. 组件与文件

| 文件 | 组件 | 说明 |
|------|------|------|
| `otel-collector.yaml` | Collector（Deployment + Service） | OTLP grpc/http 接收、属性脱敏、batch、→ Tempo + Prometheus exporter |
| `prometheus-rules.yaml` | PrometheusRule CRD | prometheus-operator/kube-prometheus-stack 形态；correctness 8 条 + SLO 2 条 + 业务 6 条 |
| `prometheus-rules-classic.yaml` | rule_files 形态 | 独立 Prometheus 用的同一告警集（`test-observability.sh` 挂载） |
| `stack/00-namespace.yaml` | `agent-platform-observability` | restricted 姿态 |
| `stack/10-loki.yaml` | Loki 单实例 | tsdb v13、filesystem、保留 168h，structured metadata 开 |
| `stack/11-tempo.yaml` | Tempo 单实例 | OTLP 4317/4318、本地存储、`query_frontend.search.enabled` |
| `stack/12-prometheus.yaml` | Prometheus | scrape collector:9464（全部 OTLP 指标）+ 自身；挂 rules |
| `stack/13-grafana.yaml` | Grafana | 预置 3 数据源（Prom/Loki/Tempo）+ 文件型看板 provider |
| `stack/14-promtail.yaml` | promtail DaemonSet | k8s pod 日志 → Loki；json stage 提取 trace_id/run_id/attempt_id |
| `dashboards/*.json` | 3 块看板 | 总览 / 单次 Run 追踪 / 模型成本容量+依赖健康 |

## 3. 告警规则集（≥3 业务告警 + correctness 零容忍）

`prometheus-rules.yaml`（CRD）与 `prometheus-rules-classic.yaml`（rule_files）同集：

- **correctness（零容忍，page）**：unauthorized_write / duplicate_effect / audit_bypass /
  stale_fence_write / committed_checkpoint_lost / artifact_checksum_mismatch /
  cross_tenant_access / multiple_active_attempts（即 `agent_platform_correctness_violation_total`）
- **SLO burn（page）**：API 可用性误差预算 fast/slow burn（`agent_platform:http_error_ratio_*`）
- **业务（warning）**：Run 失败率 5m>10%（`agent_platform_run_lifecycle_total`）、队列积压
  >5（`agent_platform_queue_backlog`）、模型错误率 5m>20%（`agent_platform_model_errors_total`）、
  API p95>2s（`agent_platform_http_latency_seconds_bucket`）、租约续期失败 spike
  （`agent_platform_lease_failures_total`）

## 4. 看板

| 看板 | uid | 角色 | 内容 |
|------|-----|------|------|
| Agent Platform · Overview | `agent-platform-overview` | 值班/所有人 | Run 成功率、积压大数字、API QPS/错误率、attempt 终态表格、控制面日志 |
| Agent Platform · Single Run Trace | `agent-platform-run-trace` | 排障 | 输入 `run_id` 变量 → 该 Run 的日志流（Loki `run_id="${run_id}"`）+ 生命周期计数 + Tempo traceql 指引（Explore 内按 run.id 属性查询瀑布） |
| Agent Platform · Model Cost & Capacity | `agent-platform-model-capacity` | 负责人/运维 | 模型调用/延迟 p50/p95/p99、队列→running 延迟、job submit 延迟、租约失败、correctness 计数、sandbox 日志、`up` 探针表 |

## 5. 接入方式

### 5.1 最小栈（完整、可实测）

```bash
bash scripts/test-observability.sh --stack-only   # 只起观察栈（不做平台 Run 断言）
bash scripts/test-observability.sh                # 全量：栈 + 平台（golden）+ 真实 Run → 断言
```

脚本幂等：apply stack → `kubectl create configmap --from-file` 生成 rules/dashboards CM →
rollout status 等待 → （全量模式）helm 覆写 `observability.enabled=true` +
`observability.otlpEndpoint` + `jsonLogs` 部署平台 → 真实 Run → 断言
Prometheus 指标 / `/-/rules` 规则数 / Tempo /api/search 返回 span / Loki 日志行 / Grafana /api/search 看板数。

### 5.2 有 kube-prometheus-stack 的环境

Prometheus/Grafana/Alertmanager 已由 operator 管理时：只 apply
`otel-collector.yaml` + `prometheus-rules.yaml`（PrometheusRule CRD）+ `stack/14-promtail.yaml`
+ `stack/10-loki.yaml`，规则经 `ruleSelector` 匹配（CRD namespace 建议与平台同 ns）。

### 5.3 helm values 接线

`deploy/helm/values.yaml` 新增 `observability` 段（enabled / otlpEndpoint / jsonLogs /
prometheusExporter），api/orchestrator 注入 `AGENT_PLATFORM_OTLP_ENDPOINT` 等；Runner Pod
由 worker 透传同一批 env（`K8sJobDispatchRunner` → Job `extra_env`）。

## 6. 验证清单（G.3 验收条款 4.3）

- [ ] OTLP metrics 在 Prometheus 可查询（`agent_platform_run_lifecycle_total` 等，≥1 面板）
- [ ] 告警规则 ≥3 条生效（`/api/v1/rules` 列出 correctness + SLO + business 组）
- [ ] 日志集中可检索（Loki 按 `run_id` 过滤命中真实 JSON 日志行）
- [ ] Tempo 中一次 Run 的 span 集可按 `agent.platform.run.id` 检索（Explorer / API）

## 7. 保留策略与规模（SDD §G.4 ④）

metrics 15–30 天（Prometheus tsdb retention 15d）、traces 7–14 天（Tempo block_retention 168h）、
Loki 保留 168h。成本敏感时降 Tempo retention 或按 run_id 哈希采样（入口采样留给
宿主边网关；当前按 traceparent 全量入栈）。kind 单节点即可跑全部组件
（资源合计约 2.5 CPU / 4Gi）。生产建议把 Loki/Tempo/Prometheus 换受管或高可用形态
（本栈为单实例验收形态；HA 与容量属于 Phase 4.4）。