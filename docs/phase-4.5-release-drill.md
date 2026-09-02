# Phase 4.5 发布/回滚演练记录（G7）— 2026-09-02 PASS

依据：SDD G7（`deploy/helm/README.md` 有 upgrade/rollback 章节，未在集群演练）+ §G.3 4.5 验收「发布/回滚演练一次 PASS」。

## 1. 演练内容

本轮 upgrade 载荷 = Phase 4.5 的 4.3 源头同步修复（3 处源头 + 网格策略）：

- `helm/templates/frontend-configmap.yaml`：nginx 变量 proxy_pass 加 `resolver`（values `frontend.nginx.resolver`，schema 新增）+ FQDN upstream（`.svc.cluster.local`）+ SDK 前缀 rewrite
- `helm/templates/networkpolicies.yaml`：新增 `control-frontend-egress`（frontend→api:8080，原为 4.3 运行态手动 apply）
- `helm/values.yaml` + `values.schema.json`：`resolver: 10.96.0.10`（schema pattern 校验 IPv4）
- `deploy/images/frontend-default.conf`：镜像内置版同步（resolver/FQDN/rewrite）
- `scripts/check-k8s.sh`：profile 4 断言增强（cm 含 resolver/FQDN/rewrite、egress np 存在、镜像 conf 三要素）——升级前已 PASS

## 2. 基线（升级前故障态实证）

| 检查 | 结果 |
| --- | --- |
| 集群 cm（r2）含 resolver/FQDN/rewrite | 0 命中（旧模板） |
| `control-frontend-egress` np | 不存在 |
| frontend pod 内直连 api svc（pod-to-pod） | **wget timeout**（egress 被 control-default-deny 拦） |

## 3. Upgrade 方向 PASS（revision 3）

- `helm upgrade ... -f deploy/prod/values.yaml` → revision 3，`--wait` 成功，工作负载 2/2
- cm 更新（4 处 resolver/FQDN/rewrite 命中）、`control-frontend-egress` np 创建（25s）
- frontend pod 需 `rollout restart`（cm 变更不触发 deployment 滚动）
- 验证（pod exec 直连 + 反代全链路）：
  - pod-to-pod：`wget api svc /v1/runs` → **405 Method Not Allowed**（路由在线，基线超时→修复生效）
  - port-forward 全链路：root=**200**（SPA）、POST /api/agent-platform/v1/runs=**401**（鉴权生效）、GET=**405**
  - 零超时、零 502

## 4. Rollback 方向 PASS（revision 4 = r2 内容）

- `helm rollback agent-platform 2 --wait` → 成功（rollback 产生新 revision 4，内容=r2——非回跳旧号）
- 归位实证：cm resolver 命中 0、`control-frontend-egress` np **NotFound**
- 故障态复发实证：frontend pod 内直连 api svc → **wget timeout**（与基线一致，证明回滚精确还原旧版状态）

## 5. 复发布回到终态（revision 5）

- `helm upgrade ... -f deploy/prod/values.yaml` + frontend `rollout restart` → revision 5
- pod-to-pod 直连 → **405**（终态=修复版，与升级方向一致）

## 6. 结论

- **PASS**：升级一次 + 回滚一次 + 复发布一次，全程 helm `--wait`/rollout 无故障；migration hook 无告警；HPA/PDB/KEDA 未受影响（升级仅改 cm/np/values，工作负载镜像不变）。
- 验证通道注意：kind 下宿主机直连 ClusterIP 不可达（curl --resolve 超时），必须 pod exec 直连或 port-forward 隧道；故障/修复判据用 **pod-to-pod 直连结果**（405=通，timeout=断），勿仅依赖 port-forward。
- 遗留：`control-frontend-egress` 与 cm 的运行态手动份已被 helm 接管（同名同内容，last-applied 归 helm）——后续统一走 chart。