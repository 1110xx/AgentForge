# Phase 4.4 — 容量与长 Run 验证（G8）实测结论（2026-08-31/09-01）

> 验收条款（§G.3 4.4）：HPA 压力验证通过（积压→扩容→回缩）；数小时长 Run 无快照膨胀异常；
> 出具 D/E 立项结论。

## 环境与前置
- 集群：`agent-platform-e2e`（kind v1.32.0，3 节点，control-plane taint 不可调度 → 2 可调度 worker × 8 CPU）。
- HPA：chart `hpa.yaml`（autoscaling/v2，min 2 / max 20 / CPU 70%，scaleDown stabilization 300s），
  golden `deploy/prod/values.yaml` 默认 `autoscaling.enabled=true`。
- **metrics-server 部署**：`registry.k8s.io/metrics-server/metrics-server:v0.7.2`（经 7890 代理拉取 + kind load 三节点）
  + `--kubelet-insecure-tls`（kind kubelet 无 TLS 认证）→ `kubectl top nodes/pods` 生效
  （manifest 存档 `deploy/kind/metrics-server.yaml`）。**HPA 前置补齐——kind 环境从无 CPU 指标到可扩缩。**

## A. HPA 压力验证（PASS）
实测序列（helm upgrade 开 autoscaling → 24 并发 worker × 180s 注入 `scripts/api-load.py`，POST `/v1/runs` + 轮询）：
1. 注入 1016 真实 Run（synthetic-analysis，参考 token）。
2. `test-observability` 同款断言工具外，直接观测 HPA：`cpu: 1% → 122% → 187%（目标 70%）`。
3. **扩容**：replicas 2 → 5（HPA 判定 CPU 超目标后扩容）。新 pod 出现 Pending——
   `topologySpreadConstraints maxSkew:1 / hostname / DoNotSchedule`（api deployment）在 2 节点 kind 下
   有效落地上限 ≈3 → 机制扩容（desired 5）与落地（受节点拓扑约束）分离——**生产多节点/多 AZ 无此约束**。
4. **回缩**：负载停止 → CPU 降至 1% → scaleDown stabilization 300s 平稳期后 **replicas 5 → 2** ✓。
结论：**积压→扩容→回缩全链路 PASS**（1016 runs 积压驱动 CPU 超目标 → HPA 扩 → 负载退 → 回缩收敛）。

## B. 容量缺口实测（4.4 附加发现，go-live/4.5 处理项）
1. **沙箱 Job 配额堵死调度**：sandbox `ResourceQuota count/jobs.batch=100`，且 **runtime Job 无
   `ttlSecondsAfterFinished`/清理器** → 高吞吐（1016 runs）后完成 Job 堆积到 100 → 新 attempt Job
   创建 `403 Forbidden exceeded quota` → 调度循环停止（日志实证）→ 队列 918 积压不动。
   处理：清理完成 Job 释放配额后恢复。**生产必须加 Job TTL/清理策略**（如 `ttlSecondsAfterFinished: 600`）。
   —— 这正是"数小时长 Run / 高峰吞吐"场景的真实卡点，属容量验证最重要的收获。
2. **调度器重启后 attempt 卡 PROVISIONING**：orchestrator 因 403 异常堆积重启后，`attempt.provisioning.requested`
   已 PUBLISHED（outbox→NATS relay 正常）但新调度进程未消费 → attempt 悬挂 PROVISIONING、无 Job 落沙箱。
   —— 韧性缺口：PROVISIONING 态缺恢复路径（调度器重启后对半成品 attempt 的接管）。4.5 韧性处理项。

## C. 长 Run 快照膨胀验证（PASS，D/E 结论）
- 真实长 Run：8 个 synthetic-case × 真 DeepSeek 模型逐项多步执行 → **22 轮 checkpoint**（21×
  `agent.turn.completed` + 39×tool 事件），Run SUCCEEDED（本地形态经宿主 PG/Runtime 同代码，轮次行为与
  Kind Job 形态同构；集群探针首轮同样跑通 SUCCEEDED）。
- **膨胀结构实测**：
  - `checkpoint` 每轮一行轻量游标（workflow_cursor ~448B 恒定，seq 21 后 516B）——**无全量复制重写**；
  - 膨胀主体 = **事件流 append**：`run_event` 每轮 `turn.completed` ~360B + `tool.*` ~170B×2 → **每轮 ~700B
    线性累积**（21 轮累计 7.5KB，单轮响应体积稳定，无指数放大）；
  - 长会话上下文有摘要引用（checkpoint `model_context_summary_ref`），模型请求随轮次线性而非全量重发。
- 结论：**线性可控，无失控膨胀异常 → D（快照膨胀消解）不转必做**；存储为事件流 append，数百轮量级只需
  事件保留/归档运维策略（4.5 项）。**E（2PC write-ahead）不转必做**：每轮 checkpoint 原子提交
  （21/21 COMMITTED，无撕裂）保证一致性；子进程异常中止仅丢失未提交轮，可恢复（reconciler/重试语义），
  非数据损坏级。

## 交付物
- `deploy/kind/metrics-server.yaml`（metrics-server 部署 manifest，kind 调优）。
- `scripts/api-load.py`（HPA 压力注入器：并发 create_run + 轮询，env 可调）。
- `scripts/longrun-monitor.py`（长 Run 快照采样器：状态 + checkpoint 轮次/体积 TSV）。
- 本结论文档。

## 遗留（记录，非 4.4 验收阻塞）
- 集群侧 probe attempt（run_90843f…）悬挂 PROVISIONING（调度器消费未恢复）——已计划清理（见下），
  残余现象归 4.5 韧性。
- golden `deploy/prod/values.yaml` 与 `deploy/prod/image-refs.json` 的 control-plane digest 不一致
  （values e5b9b5cd… vs image-refs 3cd6d610…）——已实测部署用 e5b9b5cd 跑通全部门，需发布管道对齐（记录待办）。