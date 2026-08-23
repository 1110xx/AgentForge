## 文档索引
无

## 目录内容
| 名称 | 类型 | 说明 |
| --- | --- | --- |
| verify.sh | 文件 | 执行 L1/L2/L3/all 统一门禁 |
| generate-contracts.py | 文件 | 从当前 Pydantic/FastAPI 实现生成 JSON Schema 与 OpenAPI |
| check-generated.sh | 文件 | 临时重建并逐字节比较 checked-in Contracts |
| check-portability.py | 文件 | 扫描 forbidden import、逃逸路径、宿主绝对路径、内部 URL 和高置信秘密 |
| wheel-smoke.py | 文件 | 在 clean venv 中只用 public surface 验证 wheel |
| test-compose.sh | 文件 | 执行 Disposable PostgreSQL/NATS/MinIO L2 |
| bootstrap-kind-dependencies.sh | 文件 | 在 Kind 中准备 L3 依赖与凭据 |
| check-k8s.sh | 文件 | K8s 静态门：helm lint（默认/kind/golden 三 profile）+ kind/生产/扩展/frontend 四路渲染 + YAML 解析 + 生产黄金 digest 断言 |
| build-images.sh | 文件 | Phase 4.1 镜像发布管道：构建 control-plane/runtime/frontend 三镜像，可选推送与写回 `deploy/prod/values.yaml` 真 digest |
| update_image_refs.py | 文件 | 仅替换 golden values `images:` 块的 repository/digest 行（保留注释与换行） |
| bootstrap-prod-wiring.sh | 文件 | Phase 4.2 G2/G3 接线：ingress-nginx + ESO + cert-manager + 演示 CA + dev Vault + seed + ClusterSecretStore + ExternalSecret 预置 + round-trip 断言；`--eso-only` 重挂 Secret |
| test-prod-form.sh | 文件 | Phase 4.2 生产形态动门：golden helm 部署 + G2 注入断言 + G3 TLS/入口断言 + L3 真实 Attempt 测试（2/2） |
| test-kind.sh | 文件 | 执行 Disposable Kind Sandbox Attempt L3 |

## 验证状态
| 门禁 | 脚本 | 状态 |
| --- | --- | --- |
| L1 wheel | wheel-smoke.py | 通过 |
| L1 contracts | check-generated.sh | 通过 |
| L2 compose | test-compose.sh | 通过（5 tests） |
| L3 静态 | check-k8s.sh | 通过（四 profile，prod profile 真 digest 钉死断言） |
| 镜像发布 | build-images.sh | 通过（三镜像本地构建/推送 + golden digest 写回） |
| 生产接线门 | test-prod-form.sh | 通过（Phase 4.2 G2/G3 实跑全绿：ESO→Vault 9 键注入 + TLS/入口 + L3 生产形态 2/2；本机 kind 环境） |
| L3 动态 | test-kind.sh | 待运行（需要 kind/helm/kubectl） |
