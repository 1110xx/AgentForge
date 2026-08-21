#Enterprise Agent Platform Backend
#定位
这是可独立安装的Python backend package,包含企业Agent 的contracts、领域状态机、Control Plane services、持久化,Protocol/adapter、Runtime/Tool/Ar
包不导入任何入业务系统的源码。业务系统通过公开HostPorts、Connector、CredentialBroker和factory注入身份、资源、策略及外部系统能力。

## 🤖 DeepSeek 模型提供商集成

平台现已支持 DeepSeek v4 Flash 作为默认模型提供商。配置简单，开箱即用。

### 快速开始

1. **设置 API Key**：
   ```bash
   export DEEPSEEK_API_KEY='your-deepseek-api-key-here'
   ```

2. **运行测试脚本**：
   ```bash
   uv run -project backend python scripts/deepseek_setup.py
   ```

3. **在生产中使用**：
   ```python
   from enterprise_agent_platform.platform.config_loader import ConfigLoader
   from enterprise_agent_platform.platform.model_provider_config import create_model_provider
   
   # 自动加载 DeepSeek 配置
   settings, model_provider = ConfigLoader.configure_with_deepseek()
   ```

### 配置选项

| 环境变量 | 描述 | 默认值 |
|---------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | 必填 |
| `DEEPSEEK_BASE_URL` | DeepSeek API 基础 URL | `https://api.deepseek.com/v1` |
| `DEFAULT_MODEL` | 默认模型 | `deepseek-chat` |

### 回退机制

- 如果未设置 `DEEPSEEK_API_KEY`，平台自动回退到 Reference 模型提供商（demo 模式）
- 支持多种 API key 名称兼容（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`）
- 提供完整的错误提示和配置指导

### 特性

✅ **Session 管理**：每个 Run 对应一个模型 session，支持追问
✅ **只读护栏**：追问时自动确保只读操作
✅ **幂等处理**：支持重复请求去重
✅ **错误处理**：完善的异常处理和错误信息
✅ **异步支持**：完全异步实现，性能优异

### 文档

- [DeepSeek API 文档](https://platform.deepseek.com/api-docs)
- [Session 架构设计](../docs/sdd-followup-mode.md)
- [模型提供商接口](../execution/session.py)
环境与安装
    Python >=3.12
    uv
从平台根目录执行:
bash
uvsync --project backend--frozen
构建wheel:
    bash
uvrun -project backend python -m build -wheel backend
pyproject.toml' 使用srclayout,wheel只包含enterprise_agent_platformpackage,不包含测试或宿主adapter。
##稳定公开入口
接入代码优先从包根导入:
    python
fromenterprise_agent_platform import
    AgentPlatformContainer,
    ApprovalDecisionService,
    AuthContextProvider,
    Connector,
    CredentiaLBroker,
    DurableEffectExecutor,
    EffectCapabilityAuthorizer,
    EffectPayloadResolver,
    HostContextVerifier,
    PlatformStore,
    PlatformTransaction,
    PolicyContextProvider,
    ResourceResolver,
    create_app,
    create_in_memory_container,
    create_router,
###Factory
    create_app(container) 创建独立publicFastAPI app;
    create_router(container) 仅创建 /v1pubticrouten.供独立adapter ASGIapp挂载:
    create in_memory_container(.,):测试/演示helper,仍强制提供 Auth、Resaurce、HostContext、Policy Ports:不提供permissive identity
    ###PubLiccomposition services
    ApprovalDecisionService以 PostgreSQL/PlatformStore 事实为准,次性消费审批t,写入Approvat、ActionProposal、Event、Audit、Outbox 和可远PREPARED
    DurableEffectExecutor 校验tenant/effect/approval/request/tool/connector/target-boundcapability,执行持久化Effect,并把不明确的外部结果置为UNk
    这两个service是接入方应复用的公开边界。不要在adapter中另建一套审批overlay或仅存内存的wRITEretry表。
    ##最小测试组合
    python
    fromenterprise_agent_platformimport create_app,create_in_memory_container
IA
# Enterprise Agent Platform Backend
#最小测试组合
container=create_in_memory_container(
    auth_context_provider=test_auth,
    resource_resolveretest_resources,
    host_context_verifier=test_host_context,
    policy_context_provider=test_policy,
app =create_app(container)
该helper 验证publiccontract 和领域语义,不证明多进程并发、durability、NATs、S3或Kubernetes。
##API-onlylocal demo
显式localstack提供copy-and-run 的publicAPI 演示:
    bash
uvrun-project backend uvicorn\
    enterprise_agent_platform.reference.local_stack:create_app
    -factory-host127.0.0.1-port 8080
创建syntheticRun:
    bash
curl--fail-with-body\
    -H'Authorization:Bearer reference-local-demo'
    -H'Idempotency-Key:local-create-1'
    -H'Content-Type:application/json'\
    data
    "workflow_type":"synthetic-analysis",
    "intent:"analyze synthetic evidence"
    "resource_refs":["synthetic-case:case-0o1"],
    "parameters":{}
    http://127.0.0.1:8080/v1/runs
localstack 的事实边界:
    API-only;
    InMemoryPLatformStore进程重启后数据消失:
    固定synthetic tenant/actor/token:
    只接受syntheticresource/werkflow;
    只提供create/read/cancelscopes;
    没有scheduler、worker、durableEffect或真实Connector;
    新Run停留在QUEUED
它不会被productionentrypoint隐式加载,也不是认证失败时的fallback。
##生产进程组合
镜像入口从环境变量加载接入方factory
    text
    AGENT_PLATFORM_wORKER FACTORY=business_agent_adapter.worker:run_worker
    运行模式:
    bash
    python-menterprise_agent_platform.platform.entrypoint api
    python-m enterprise_agent_platform.platform.entrypoint worker
    要求:
    containerfactory 返回FastAPI或AgentPlatformContainer
    worker factory返回awaitable;
    productioncontainer 使用 durable PostgreSQLStore;
.
#Enterprise Agent Platform Backend
##生产进程组合
python -m enterprise_agent_platform.platform.entrypoint api
python-menterprise_agent_platform.platform.entrypointworker
要求:
container factory 返回FastAPI或AgentPlatformContainer
workerfactory返回awaitable;
    production container使用 durablePostgresQLStore;
    Auth/Resource/Context/Policy Ports fail closed;
UI Action、Artifact download、NATSnotifier、Tool Gateway、CredentialBroker、Connectors 和 Effect worker 按需要显式组装:
未配置factory时进程启动失败。
不要把localstackfactory填入生产workload,也不要实现"缺配置就管理员"的分支。
##Public与 Internal API
PublicAPI 位于/v1,面向经过用户认证的业务客户端:Run create/get/cancel/rerun、events/SSE、Surface-bound actions 和Artifact download authariza
Internal API 位于/internal/vi,面向 Sandbox Runtime 与 Effect worker:bootstrap、restore、heartbeat.Toot、Artifact、ActionProposal、Checkpo
capability,必须使用独立内部网络和访问策略,不能与publicrouter一起无差别暴露到浏览器。
##持久化原则
    PostgreSOL 是Run、Step、ExecutionUnit、Attempt、Lease、Checkpoint、Approval、EffectLedger、Artifact metadata、RunEvent、Outbox/Inbox 和 Audi
    NATS只传通知;消费者在数据库Inbox去重,消息不能改写领域真相:
    Artifact/workspaceSnapshot bytes 在不可变objectStore,READYmetadata/checksum/lineage 在数据库;
    Pod/workspace、Runner内存、前端Session、SSEconnection和 telemetry都不是恢复事实。
##Contracts
生成并比较公开合同:
    'bash
    -output-root contracts
/scripts/check-generated.sh
从平台根执行脚本。生成物位于contracts/openapi.json contracts/schemas/:fixtures 是跨 Python/TypeScript 的 golden corpus
##测试与验证
Backend focused:
    bash
    -ignore=backend/tests/integration
    -ignore=backend/tests/kind
uvrun-project backendruff check backend/srcbackend/testsscripts
    完整验证从平台根使用:
    'bash
    /scripts/verify.sh11
    ./scripts/verify.sh12
    L1:本地contracts/domain/E2E、portability、cleanwheel、frontend 与静态安全边界:
    L2:真实Compose PostgreSQL/NATS/MinIO integration;
    L3:真实Kind Job/RBAC/projected identity/NetworkPolicy;
    L4:生产等价gVisor、HA/PITR、对象恢复、凭据轮换、真实Connector和容量/攻击演练、不由本地脚本自动宣称。
    缺少所需环境时gate必须返回UNVERIFIED,不能把skip当作通过。
    #进一步阅读
    [总体架构】( /dnre/arrhitecture md)
# Enterprise Agent Platform Backend
##测试与验证
    'bash
uvrun--project backend pytest-q backend/tests
    -ignore=backend/tests/integration\
    -ignore=backend/tests/kind
uv run -project backend ruff check backend/src backend/tests scripts
完整验证从平台根使用:
'bash
./scripts/verify.shl1
./scripts/verify.sh12
./scripts/verify.sh13
L1:本地contracts/domain/E2E、portability、clean wheel、frontend与静态安全边界;
L2:真实Compose PostgreSQL/NATS/MinIO integration;
    L3:真实Kind Job/RBAC/projected identity/NetworkPolicy;
L4:生产等价gVisor、HA/PITR、对象恢复、凭据轮换、真实Connector和容量/攻击演练,不由本地脚本自动宣称。
缺少所需环境时gate必须返回UNVERIFIED,不能把skip当作通过。
#进一步阅读
    [总体架构](./docs/architecture.md)
    [安全模型](../docs/security.md)
    [实现说明](../docs/implementation.md)
    [嵌入指南](../docs/embedding-guide.md)
    [迁移指南](../docs/migration-guide.md)
    [Reference Vertical](src/enterprise_agent_platform/reference/README.md)
