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
| check-k8s.sh | 文件 | K8s 静态门：helm lint + kind/生产 values 渲染 + YAML 解析 |
| test-kind.sh | 文件 | 执行 Disposable Kind Sandbox Attempt L3 |

## 验证状态
| 门禁 | 脚本 | 状态 |
| --- | --- | --- |
| L1 wheel | wheel-smoke.py | 通过 |
| L1 contracts | check-generated.sh | 通过 |
| L2 compose | test-compose.sh | 通过（5 tests） |
| L3 静态 | check-k8s.sh | 通过 |
| L3 动态 | test-kind.sh | 待运行（需要 kind/helm/kubectl） |
