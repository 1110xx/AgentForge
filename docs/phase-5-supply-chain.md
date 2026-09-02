# Phase 5 Step 3 — 供应链：GHCR 发布（零凭据 CI 原生）+ golden 记录化 — 2026-09-02 实跑

目标：三镜像发布到 GitHub Container Registry、CI 发布目标改写、golden 指到 GHCR，
为生产拉取提供公开内容寻址工件。**零云成本、零主镜像重建**（digest 内容寻址与 registry
无关；Step 2 硬约束兑现：全流程只有 Step 1 那一次主镜像重建）。

## 0. 为什么发布是 CI 原生的（而非本地 PAT）

- 本机 git token = classic PAT（scopes `repo, workflow`），**无 `write:packages`**：
  `docker login ghcr.io` 成功、push 返回
  `denied: permission_denied: The token provided does not match expected scopes.`
- GitHub Actions runner 的 `GITHUB_TOKEN` 天然带 `packages: write`（workflow 显式声明）——
  **零外部凭据**。故 GHCR 发布由 main 推送触发的 `image-gate` 完成，本机只做
  验证与记录。仓库 public → 包 public（匿名可拉）。

## 1. 改动清单（commit `9492836` / `27a7e31` / `8c47241` / `a4e0352`）

| 文件 | 变更 |
| --- | --- |
| `.github/workflows/ci.yml` | `image-gate`：PR 只构建；main 登录 ghcr（GITHUB_TOKEN）→ `build-images.sh --push --update-prod-values`（`AGENT_PLATFORM_REGISTRY=ghcr.io/<owner>/agentforge`）→ digest 变化则 GitOps 提交。`permissions: packages: write + contents: write`。顺带修复既有 YAML 隐患（未加引号的 `gate: lint` 冒号使整个 workflow 解析失败——此前历次 main CI 全 failure 的根因之一） |
| `scripts/build-images.sh` | 两个 CI 必现修复：① `{{if index .RepoDigests 0}}` 在全新 daemon（首次 build-only）上 index 越界 → 改 `{{if .RepoDigests}}` 走 `.Id` 兜底；② golden 回写用 Windows-only `.venv/Scripts/python.exe` → 解释器交叉探测（venv → python3/python） |
| `deploy/prod/values.yaml` + `image-refs.json` | repository 从 kind-only `localhost:5001` 切到 `ghcr.io/1110xx/agentforge/enterprise-agent-platform/{control-plane,runtime,frontend}`（digest 保持真值，由 CI 二轮回写） |
| `scripts/test-prod-form.sh` | kind L3 门保持自洽：golden digest 按 digest 匹配本地镜像 → 以**本地 registry 身份**重打 tag 灌节点 → helm 安装用 `--set` 把 repository 覆写为 localhost（digest 不变）。原因见 §3 |
| `deploy/helm/README.md` | 发布说明改 GHCR + GITHUB_TOKEN 语义 |

## 2. 证据链（全部实跑）

1. **kind L3 门（GHCR golden 形态）**：`scripts/test-prod-form.sh --keep` rc=0 —— G2 注入断言 +
   ESO 10 keys round-trip + G3 TLS 链 + L3 真实 Run→Attempt Job→SUCCEEDED + 伪造明文 401 负向，
   部署的是 prod golden 的同一组 digest（6a32e/b1bb/cf64，后经 CI 重定标）。
2. **CI 实发**：push `8c47241` → run `33619097309` **completed success**（l1 / frontend / helm-static /
   image-gate 四作业全绿）→ image-gate 构建三镜像并 push 到
   `ghcr.io/1110xx/agentforge/enterprise-agent-platform/*`，回写 digest 并 GitOps 提交
   **`a4e0352`**（origin/main 上 image-refs 已含 CI digest）。
3. **发布物匿名可拉（public 证据）**：无登录状态下
   `docker manifest inspect ghcr.io/1110xx/agentforge/enterprise-agent-platform/<name>@<digest>`
   三个全部 OK。
4. **最终 golden（origin/main `a4e0352`）**：
   - control-plane `ghcr.io/1110xx/agentforge/enterprise-agent-platform/control-plane@sha256:9a71bd77…`
   - runtime `…@sha256:386164d2…`
   - frontend `…@sha256:b7b4b15f…`
   （CI 重建 digest ≠ 本机旧构建：基础镜像 tag 漂移所致 → 管道自动重定标，属设计行为；
   bot 用 GITHUB_TOKEN 提交不会递归触发 CI，无乒乓。）

> **已知行为（非缺陷）**：当前 CI 构建在全新 runner 上**不可位复现**（COPY 层 mtime、依赖
> 下载等随时间戳变化）→ **每次 main push 会带来一次 bake commit**（如 487a200 → `10d70ac`），
> 但 bot 提交不递归触发 CI，链必然收敛；digest 随 commit 钉死 = 内容寻址仍正确。
> 后续可选优化：引入 `SOURCE_DATE_EPOCH`/buildx `--source-date-epoch` 使构建可复现、
> bake 只在源码真变化时发生——属管道优化，不阻塞上线。

## 3. kind 门为何用本地 registry 身份（探针实证）

探针：`ghcr.io/…/control-plane@sha256:6a32…`（imagePullPolicy IfNotPresent）在 kind 节点 →
`ErrImagePull: failed to authorize: GET https://ghcr.io/token?scope=repository:…:pull 403`。
**containerd 对 `image@digest` 按名字解析**（先要匿名 token），不因本地已有同 digest 内容而跳过
——ghcr 包存在前不可能以 ghcr 名解析。故门在 helm 安装时把 repository 覆写为本地 registry
身份（digest 不变，内容同物），保持离线可重跑；GHCR 名是生产/CI 记录。

## 4. 生产 HTTPS/域名（就绪物 vs 外部前置）

- 集群内 TLS 全链已演示（G3：demo CA → root/api 200 / leaf SAN / ingress-nginx）；cert-manager
  签发机制在 demo issuer 上实证。
- 真实 Let's Encrypt 签发需要**公有域名 + 可被 ACME 访问的端点**（HTTP-01）或 DNS API（DNS-01），
  kind 本地集群无公有端点 → **零成本无法实测 LE 出证**。
- 就绪物：`deploy/prod/tls/letsencrypt-issuer.yaml`（staging/prod 两个 ClusterIssuer，填
  `email`/`solver` 即可；应用后把 values `ingress.tls.clusterIssuer` 切 `letsencrypt-prod` 并
  helm upgrade）。域名是**唯一的付费项**（~$1-3/年首年），需用户决策后再接线。

## 5. 后续发布流程（人话）

```
改代码 → commit → push main
  └─ CI: l1/frontend/helm 门 + image-gate（构建；main 再 push GHCR + 回写 golden 自动提交）
本地想用新 golden 跑 kind 门：scripts/build-images.sh --push --update-prod-values 后
  bash scripts/test-prod-form.sh --keep
```
