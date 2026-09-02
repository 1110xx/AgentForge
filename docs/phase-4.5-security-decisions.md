# Phase 4.5 安全决策记录（G4）：gVisor 替代方案 + OIDC 决策

- 日期：2026-09-02
- 依据：SDD G4 差距行（`gVisor RuntimeClass 模板/SAname 已备，需集群具备该 RuntimeClass 并验证 egress 行为；OIDC/多租户身份未做`）与 `docs/go-live-plan.md` Phase 4.5 清单
- 验收目标：`gVisor 验证结论或替代方案；OIDC 决策记录`（SDD §G.3 4.5）

## 1. gVisor：集群实证与替代方案决策

### 1.1 集群实证（2026-09-02）

| 检查项 | 结果 |
| --- | --- |
| `kubectl get runtimeclass`（agent-platform-e2e，3 节点） | **No resources found** —— 集群无任何 RuntimeClass，含无 `agent-platform-gvisor` |
| 节点容器运行时 | containerd 1.24（kind 内置，无 runsc handler） |
| 节点内核 | 5.15 WSL2 宿主机内核（非独立 VM/裸机，gVisor 的 Kata/microVM 前提不成立） |
| `deploy/helm/templates/runtimeclass.yaml` | 仅在有 RuntimeClass 的集群渲染（条件模板），kind/prod 当前均不渲染 |
| `deploy/prod/values.yaml` sandbox 段 | `runtimeClassName: agent-platform-gvisor` 为**声明式契约**：指向尚不存在的类时只影响声明了 runtimeClassName 的负载 | — |

结论：当前环境**不具备** gVisor（或任何沙箱运行时）的部署与验证前提。按 SDD 4.5 验收要求，本阶段给出**替代方案结论**而非强行引入。

### 1.2 决策：不引入 gVisor —— 采用「分层纵深防御替代栈」（已实现，2026-09-02 复核）

Sandbox 运行时 Pod（`job_spec.py` 生成的 Job Pod，Phase 3/4 已实装并历经 L3 门验证）：

1. **命名空间隔离**：Sandbox 专用命名空间 `agent-platform-sandbox`，PSA `restricted`（tenant 平面），与 control 平面（`privileged`，供基础设施）分离。
2. **Pod 安全基线**（`job_spec.py:93-146`）：`runAsNonRoot: true`（uid 101）、`seccompProfile: RuntimeDefault`、`readOnlyRootFilesystem: true`、`capabilities.drop: ["ALL"]`、非特权容器。
3. **Kubernetes API 最小面**：Sandbox SA 无 RoleBinding；运行时能力仅经 control 平面发放的短时 capability token（`security/capabilities.py` CapabilityIssuer/Verifier 协议）。
4. **网络微分段**（`networkpolicies.yaml`）：`sandbox-default-deny` 双向全拒 + `sandbox-dns-egress`（仅 CoreDNS）+ `sandbox-approved-proxies-egress`（仅 control 平面 API/model-proxy/tool-gateway/artifact-proxy/otel 白名单端口）。无 gVisor 时，**egress 行为由 NetworkPolicy 白名单控制**（替代 gVisor 的网络隔离面）。
5. **资源/生命周期限制**：CPU/内存显式限额、`activeDeadlineSeconds` 300、Job `backoffLimit: 0`、**Job TTL `ttlSecondsAfterFinished: 600`**（4.5 新增，防完成 Job 堆积吃配额）。
6. **凭据隔离**：`automountServiceAccountToken=false`、`enableServiceLinks=false`、/workspace 与 /tmp 有限额 volume、不挂载 docker socket/宿主机目录/Secret volume。

**替代结论**：在当前验证环境，上述 1-6 构成完整的 hostile-workload 分隔栈；gVisor 降级为**真实生产节点 L4 验证项**（`docs/security.md` §3.3、§L4）。Helm 契约保持 gVisor-ready（runtimeclass 模板与 `runtimeClassName` 声明保留，生产节点装好 runsc 后仅需在 values 中原位启用 + L4 验证即可切换，零代码改动）。

## 2. OIDC 决策：MVP 不引入 IdP，保留参考鉴权 + 明确接线路径

### 2.1 现状实证（2026-09-02 复核）

- **用户侧（外部 /v1 API）鉴权**：`fastapi/dependencies.py:279 authenticate_request` → `AuthContextProvider.authenticate(authorization 头)`。参考形态 `ReferenceLocalAuth`（`reference/local_stack.py:29-43`）比对**固定静态 bearer** 并返回固定 `RequestContext(tenant=reference-local, actor=reference-local-analyst, scopes=runs:create/read/cancel)`；生产 K8s 容器（`reference/k8s_container.py:90`）当前同样注入 `ReferenceLocalAuth`。
- **运行时侧（internal API，Sandbox→Control）鉴权**：`internal_adapter.py:58-59` `_runtime_token(attempt_id) = "runtime-token:{attempt_id}"` —— **明文确定性派生、无签名、无时效**；验证仅比对派生串 + attempt 事实核验（run_id/generation）。Bootstrap 端点向运行时发放该 token（`internal_adapter.py:123-125`）。
- **授权模型已具备**：`RequestContext(tenant_id, actor_id, scopes, request_id, trace_id)` + `require_scope`（`dependencies.py:313`）+ 端点级 scopes（runs:create/read/cancel 等）+ capability token（`capabilities.py` 协议带 expires_at/scopes）。

### 2.2 决策（A/B 评价）

| 选项 | 评价 | 决策 |
| --- | --- | --- |
| A. MVP 引入 IdP（Dex/Keycloak + OIDC discovery + JWKS 校验） | 接线成本高（IdP 部署、证书/Discovery 链、E2E 测试），MVP 内网/单租户验收场景无外部身份源；且会同时放大运行面 | **不采用（本阶段）** |
| B. 保留参考鉴权，明确 OIDC 集成路径 | `AuthContextProvider` 是**唯一接头**：实现 `OIDCAuthContextProvider`（JWKS 远端校验 Bearer JWT → claims 映射 tenant_id/actor_id/scopes → 返回 RequestContext）即全栈生效，**控制面业务零改动**；协议、授权模型、capability 体系全部复用 | **采用** |

**决策**：MVP 维持参考鉴权（静态 bearer 最小鉴权），**OIDC 作为生产前置清单项**：上线真实多租户/公网场景前，仅需新增一个 `AuthContextProvider` 实现 + 容器接线替换（`/v1` 路由与鉴权中间件不动），映射关系与 scopes 授权表在 `docs/security.md` §4 扩展。

### 2.3 附带落地（本阶段已执行/决策的轻量加固）

1. **运行时 capability 签名化 ✅ 已落地（生产前置 Step 1，2026-09-02 执行）**：
   旧实现 `runtime-token:{attempt_id}` 明文派生（`fastapi/internal_adapter.py:58-59`，知 attempt_id 即得 token、无签名无时效）——已替换为 **HMAC-SHA256 签名短时能力令牌**：
   - 新模块 `security/runtime_tokens.py`：`rt.v1.<b64url(payload)>.<hexsig>`，payload 绑定 `sub=attempt_id` + `iat/exp`，验证先验签名/时效/subject 再查库；密钥源 `AGENT_PLATFORM_CAPABILITY_KEY` env（SecretStore 注入），无 env 时文档化 demo key 回退（本地/kind 一致）。
   - **三形态切换**：生产 HTTP 内部 API（`internal_adapter.py` bootstrap 签发 / verifier 验证 / heartbeat 上下文**滚动重签**）；本地子进程形态（`subprocess_orchestrator.py` `_op_bootstrap` 签发）；`http_runtime`/pipe 消费端不透明携带（零改动，注释同步）。
   - **接线**：Vault 种子（bootstrap-prod-wiring.sh）+ kind secret（bootstrap-kind-dependencies.sh）注入密钥；helm README Secret 契约 四键 → 五键；独立 internal API `_status` 401 映射补 `AUTH_FAILED/AUTH_EXPIRED/AUTH_INVALID`。
   - **测试**：新 `tests/test_runtime_capabilities.py`（签名/篡改/过期/未来/错误 subject/key/畸形/旧明文拒绝/环境选择）11 条 + `test_http_runner_lifecycle`/`test_internal_api_mount` 断言改为 `rt.v1.*`。
   - **集群级证据（同日）**：重建镜像（control-plane `6a32eba1`、runtime `b1bb33fe`、frontend `cf64fee3`，digest 已入 golden）→ kind/prod-form L3 门 **PASS**：G2 新增 `AGENT_PLATFORM_CAPABILITY_KEY` Vault→ESO→Secret→pod env 注入断言通过（生产形态无 demo 回退）；L3 真实 Run→Attempt Job→SUCCEEDED（签名 bootstrap/heartbeat/commit 全链路在集群 HTTP 形态验证）；负向探测「伪造明文 `runtime-token:*` → /internal/v1/runtime/bootstrap 401」通过；ESO round-trip **10 keys** 两轮一致。
2. **OIDC `AuthContextProvider` ✅ 已实现（生产前置 Step 1，2026-09-02 执行）**：
   新模块 `security/oidc.py` `OIDCAuthContextProvider`——OIDC discovery + JWKS 缓存（TTL 300s）、RS256 验签（cryptography，无新依赖）、iss/aud/exp/nbf(leeway) 校验、claims → tenant_id/actor_id/scopes 映射；参考鉴权保留为默认（dev 回退），`AGENT_PLATFORM_AUTH_PROVIDER=oidc` + `AGENT_PLATFORM_OIDC_{ISSUER,AUDIENCE,JWKS_URI,TENANT_CLAIM,ACTOR_CLAIM,SCOPE_CLAIM}` 开启；K8s 容器工厂（`reference/k8s_container.create_container`）经 `create_auth_provider_from_env` 选择。离线全链路测试 `tests/test_oidc_auth.py` 13 条（本地 RSA + httpx.MockTransport 假 IdP：映射/kid/过期/nbf/aud/iss/alg/tamper/缓存/不可用 503/from_env/选择）。真实 Auth0/Keycloak 租户接线留 IdP 接入挂起项（凭证/发现 URL 只差环境变量）。
3. **HA（G4 第三项）**：多副本 + PDB + 优雅关停已实装（chart hpa.yaml/pdb.yaml + worker 优雅退出）；4.4 已实证 HPA 扩容/回缩——HA 视为已覆盖，不再单列演练。

## 3. 验收自检

- [x] gVisor：集群实证（0 RuntimeClass）→ 替代方案结论（分层防御栈 1-6，已实装并复核）
- [x] OIDC：决策记录（A 不采用 / B 采用，含接线点与前置条件）
- [x] 附带加固：运行时 capability HMAC 签名 **决策**（升级路径已定，留生产前置清单）→ **✅ 生产前置 Step 1 已落地**（rt.v1.* HMAC 三形态 + SecretStore 注入 + 测试，见 §2.3）
- [ ] L4 真实节点 gVisor 验证（挂起项，移交生产前置，`docs/security.md` §L4）
- [x] OIDC provider 实现（`security/oidc.py`，离线 JWKS 全链路测试通过）
- [ ] IdP 真实租户接入（Auth0/Keycloak 接线=填 AGENT_PLATFORM_OIDC_* env，provider 已就绪，多租户/公网上线前置）