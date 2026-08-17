Enterprise Agent Platform
这是一个可以整体复制、独立安装和嵌入现有业务系统的企业Agent平台参考实现。它把稳定业务事实、Agent执行、Sandbox、审批、Artifact、A2UI、宿主权限和分布式基础设施拆成朗确边界;源码、前端包、Contracts、部署清单和验证入口全部位于本目录内。
当前实现已经不是只有Contracts 的骨架:L1 覆盖Run/Step/Attempt/Checkpoint/Lease、恢复、审批、Effect、Artifact、A2UI、FastAPI 公共 API、前 SDK、SQLadapter、消息与平台清单;L2/L3分别提供Compose 依赖集成与Kind Sandbox Attempt 的可选真实环境门禁,生产HA、gisor、外部 身份系统和企业依赖仍需要在目标环境完成L4验证,不能由本地测试代替。
##复制后验证
前置条件:Python3.12+、[uv](https://docs.astral.sh/uv/)、Node.js22+和npm
    bash
cdenterprise-agent-platform
./scripts/verify.sh11
                                        disas
                                        effect
11会执行frozen Python/npm安装,后端测试、复制到空父目录后的导入测试、Ruff、Contracts 重建对比、clean-wheel 安装 smoke、前端测试/typecheck/Lint/build、宿主import/绝对路径/高置信秘密扫描以及shell 语法检查,任一检查缺失或失败都不会被标成通过。 ￥lease
复制时不需要携带缓存和构建产物:.dockerignore nats-re
                                        gitignore和portabilitytest会保护这个边界.复制完成后,在新目录重新运行./scripts/verify.shl1即可证明没有依赖原来的父仓库 object-
#API-only本地演示 postgre
这个入口只用于观察独立HTTP合同。它使用固定的reference 身份、合成资源和进程内存储:进程退出即丢失数据,也没有scheduler/worker,因此创建的Run保持QUEUED。它不会被生产入口隐式加载, DOcS.md
    'bash
uv sync-project backend--frozen backend/.venv/bin/uvicorn\ docs
    enterprise_agent_platform.reference.local_stack:create_app -factory--port 808e
另一个终端创建Run:
    bash
curl-ihttp://127.0.0.1:808/v1/runs H'Authorization:Bearer reference-local-demo task-13-rep
    H'Idempotency-Key:local-create-1' frontend
    -H'Content-Type:application/json'\ examples/en
    -data'( npouapou
    "workflow_type":"synthetic-analysis", "intent":"Analyze a portable syntheticresource" packages )agent-ul-cat
    "resource_refs":["synthetic-case:case-42"], "parameters":{"analysis_mode":"summary","max_items":10), >agent-ui-clie
    "host_context_ref":"reference-context:demo" >agent-ui-pro
                                        ee-n-luebe(
                                        >test
                                        DESIGN.md
    包含真实checkpoint-approval-新Attempt/Lease-Effect-finaRunView的进程内完整参考链由以下测试证明: esint.configjs
    bash 11package.json f1package-lock.js
    backend/.venv/bin/pytest-qbackend/tests/test_public_smoke.py backend/tests/test_vertical_e2e.py ()tsconfig.basejs
                                        rs vitest.config.ts
                                        scripts
    嵌入现有系统 8bootstrap-kind-d
    宿主只实现稳定Ports,并从顶层包组装router:平台不会 1mport 宿主数据库、用户模型或前端源码: check-portabity 8check-generated
    python DOCS.md
    from enterprise_agent_platform import create_in_memory_container,create_router generate-contract
6e slest-compose.sh
    container=create_in_memory_container( stest-kind.sh
        auth_context_provider=auth_port, Sverifysh
        resource_resolver=resource_port, host_context_verifierecontext_port, dockerignorg wheel-smokepy
        policy_context_provider=policy_port, gitignore
    host_app.include_router(create_router(container),prefix="/agent-platform") DOCS.md
                                        README.md
    create_in_memory_container只适用于测试,生产 adapter 必须注入 durablePlatformStore、可信身份/资源/策略 Ports、A2UI action handler、Artifact authorizer,以及 scheduler/worker 组合,审批和外部 wRITE 分别通过publicApprovalDecisionService DurableEffectExecutor组装:浏览器不能调用内部 Effect 执行/对账路由,也不能自行选择 tenant、tool、scope、credential 或 canonical target,Effect 开始执行时,internal route 把经验证的effect-worker service subject 作为executor_id,与通增的executian_epoch和执 OUTUNE
    行租约一起持久化:Connector finish和watchdog 都必颁提交且命中该owner/epoch 对,否则以EFFECT_EXECUTOR_FENCE_MISMATCH拒绝。Audit 记录实际worker subject 与epoch.watchdog 还只能在租约到期后把遗留的EXECUTING 标记为UNKNONUNKNOwN 对账还必领注入独立 YTIMELINE
    FfterRarnnriliatinnbuthorier坦银定Fffar+证提的Ainnet对为FaTlFn环要证AverutarP信止日部确稳定 Ln1.Col1 UTF-8
Enterprise Agent Platform
API-only本地演示
嵌入现有系统
宿主只实现稳定Ports,并从顶层包组装router;平台不会import 宿主数据库、用户模型或前端源码:
    python
from enterprise_agent_platform import create_in_memory_container,create_router
container=create_in_memory_container( auth_context_provider=auth_port, runbe
    resource_resolver=resource_port, host_context_verifier=context_port, disast
    policy_context_provider=policy_port, effect-
                                        lease-8
host_app.include_router(create_router(container), ,prefix="/agent-platform") nats-re
                                        object-
                                        postgres
create_in_memory_container只适用于测试。生产adapter 必须注入 durablePlatformStore、可信身份/资源/策略Ports、AzUI action handler、Artifact authorizer,以及 scheduler/worker 组合.审批和外部 wRITE 分别通过 publicApprovalDecisionService Q.en.exam
DurableEffectExecutor组装:浏览器不能调用内部 Effect 执行/对账路由,也不能自行选择 tenant、tool、scope、credential或canonical target。Effect 开始执行时,internal route 把经验证的 effect-worker service subject 作为executor_id,与递增的execution_epoch和执
行租约一起持久化:Connectorfinish和watchdog都必须提交且命中该owner/epoch对,否则以EFFECT_EXECUTOR_FENCE_MISMATCH拒绝。Audit记录实际worker subject 与epoch.watchdog 还只能在租约到期后把遗留的EXECUTING标记为UNKNOwNUNKNDnN对账还必须注入独立
                                        docker-co
EffectReconciliationAuthorizer并提交绑定Effect和外部证据的 digest;对账为FAILED还要证明executor已停止且外部观测稳定。 DOCS.md
                                        operabons
已知失败的 Effect 不会被原地重放。持有effects:recoverscope 的操作者可通过publicrecovery command(前端 SDK为recoverFailedEffect')从已提交Checkpoint 进入恢复:原FAILEDEffect 保持不变,successor Attempt 必须重新规划、生成新提案并再次审批后才能产生新wRITE docs
                                        architectun
Compose的api和worker位于显式runtimeprofile.deploy/.env.exampTe故意不提供可运行的factory:目标镜像必须安装独立的宿主adapter 包,并配置AGENT_PLATFORM_cONTAINER_FACTORY与AGENT_PLATFORM_wORKER_FACTORY这通免reference demo变成生产权限回退 DOCS.md
验证层级 embedding
                                        implementa
|命令|实际证明|不证明| -
                                        security.md
    ./scripts/verify.sh11丨本机Contracts、领域/服务、公共API、SDK、copy/wheelportability、静态部署策略|PostgreSQL/NATS/S3 真实并发与容器网络| task-12-repo
    /scripts/verify.sh 12|Disposable PostgreSQL、NATS JetStream、MinIO、Alembic与 adapter 集成|Kubernetes、Sandbox 隔离、HA| task-13-repo
    目标环境L4|gVisor、0IDC、HA/failover/PITR、S3versionrestore、凭据轮换、容量与成本|不能由本仓库自动宣称1 /scripts/verify.sh13|DisposableKind 中真实Job/Pod、RBAC、NetworkPolicy与projected identity1gVisor、跨节点HA、生产依故 frontend
                                        examples/en
缺少Docker、Kind、kubect1或Helm时,请求的L2/L3会输出UNVERIFIED并以69退出,不会静默跳过。 npowepou<
#目录与文档 packages
    backend/:可构建Python wheel、公共FastAPI router、控制面与execution/tool/artifact Ports. agent-ul-pro agent-ul-c
    frontend/': deploy/: contracts/ ComposeL2、Helm/Kind L3、可观测性策略与 runbooks. agent-ui-protocol'、client、allowlisted catalog、React renderer 和 embedded-host example. 确定性 JSON Schema、OpenAPI 和 golden fixtures. >test >agent-ui-reac
    scripts/ 统一验证、生成物、portability、Compose和Kind入口。 DESIGN.md
    docs/architecture.md:统一执行模型、状态、数据流与分布式边界。 eslint.configs
    docs/security.md:身份、能力、Sandbox 与残余风险。 (1package-lockjs
    docs/embedding-guide.md:后端/前端嵌入合同 docs/migration-guide.md:从业务仓库内原型迁移到独立平台的方法。 (1tsconfig.base.js packagejson
    docs/imptementation.md:已实现能力和证据边界。 Tsvitest,config.ts
                                        Vscripts
                                        3bootstrap-kind
                                        Scheck-generated
                                        check-portabiity
                                        DocS.md
                                        s.test-compose.sh
                                        8test-kind.sh
                                        verify.sh
                                        wheel-smoke.py
                                        dockerignore
                                        gitignore
                                        DOCS.md
                                        READMEmd
                                        >OUTLINE
                                        >TIMELINE
                                        Ln1,Col1
