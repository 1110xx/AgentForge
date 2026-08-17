嵌入现有业务系统指南
##1.推荐集成形态
"可入"不等于"把平台源码烤进业务后端并直接导入内部表"、推荐把平台保持为独立部署和独立发布单元,只在三条稳定边界与业务系统融合
1.业务后端或独立adapter service实现Host Ports 与业务Connectors
2.业务网关把/api/agent-platform/反向代理到平台publicAPI,并传递用户token;
3.业务前端加载独立SDKpackages、把AgentPanel嵌入既有页面
    mermaid
flowchart LR
    Page[现有业务页面]->SDK[AgentUI SDK]
    SDK->Gw[业务网关/same-originproxy]
    GW-→API[立Agent Platform PublicAPI]
    API->PORT[Host Adapter Service]
    PORT->IAN[企业身份/策略]
    PORT->BIZ[业务资源API]
    APIEXEC独立Scheduler/Sandbox/Tool Gateway]
    EXEC->CoN{Business Connectors]
这样可以整体带走enterprise-agent-platform/,也可以独立升级平台,而不会让平台依赖当前业务工程的model、ORM、路由或部署目录。
2.接入方必须提供的后端能力
2.1AuthContextProvider
验证业务系统或企业IdP 发出的 token,返回:
    tenant_ia:由可信claim/自录派生,不能读取客户端自报header;
    actor_id:稳定用户或serviceidentity;
    scopes' 当前public操作范围;
    repuest_id trace_id:必须与平台传入值一致。
常用public scopes:
LE
    runs:create
    runs:read
    runs:cancel"
    runs:act
    effects:recover' (仅授予可确认已知失败并启动重新规划的操作者)
    artifacts:down load
    approvals:decide(Surface-bound approval handler 最终仍校验)
##2.2ResourceResolver
把览器提交的opaque
                resource_ref转换为服务端权威事实:canonical ID、tenant、owner、classification、version 和 digest。建议ref 形式是resource-type:opaque-id不包含 hostname、path、query、credential 或可执行表达式
跨租户、已到除或无权院资源统一按不可见处理,Resolver不返回数据库对象或业务源码类型,只返回平台公开的ResoLvedResource
###Z.3 HostContextVerifier
###2.4PolicyContextProvider
根据actor、workflowtype.resolvedresources和verified context 返回:
    allow/deny:
policy version和digest;
Agent可用scopes;
max runtime、Tool calls.Artifact bytes.型预算等 budget.
Policy budget不能携带endpoint,token.secret,password、credential或任意uRL,Run 创建时这些权威结果会被快照,便于审计当时为什么允许"
###2.5CredentialBroker与Connector
Credential Broker只在Tool Gateway/Effect Worker 务端获频时凭据:Connector 接收canonicalresource ref、operation、validated arguments 和CredentialMaterial,Connector 要:
一对READ调用设timeout、结果schema 和大小限制: Doyouw
    对wRITE支持稳定effect_key幂等或可查询reconciliation; extension
    在外部系统真正以effect_key去重;平台消息是at-Least-once,只有该承诺才能形成effectively-once 结果; 始确仪分"已知牛"我"外部结里去知:
                                        Ln1,Col1Soaces:2 AR
嵌入现有业务系统指南
#2.接入方必须提供的后端能力
###2.5CredentialBroker与Connector
在外部系统真正以effect_key去重:平台消息是at-least-once,只有该承诺才能形成effectively-once 结果; 明确区分"已知失败"和"外部结果末知"
不把凭据、原始响应header或敏感URL写入结果、事件或trace.
#3.后端组合方式
#3.1立adapter peckage(推荐)
在平台目录之外创建一个很小的adapter package.它可以依赖业务 SDK/API client 和enterprise-agent-platformwheel,但平台package 不能反向依赖它,adapter factory 返回AgentPlatformContainer
    python
from enterprise_agent_platform import AgentPlatformContainer
defcreate_container()-→AgentPlatformContainer:
    return AgentPlatformContainer(
        store=build_postgres_store(),
        control=build_control_service(),
        auth_context_provider=build_auth_port(),
        resource_resolver=build_resource_port(),
        host_context_verifier=build_context_port(),
        policy_context_provider=build_policy_port(),
        notifier=build_event_notifier(),
        ui_actions=build_surface_action_handler(),
        artifact_downloads=build_artifact_download_service(),
lee
上例中的build_是接入方实现,不属于平台默认代码。生产容器必须使用durable Store:不要调用create_in_memory_containen
进程配置:
    text
AGENT_PLATFoRK_woRKER_FACTORY=business_agent_adapter.worker:run_worker
1ie APIfactory可以独立部署,workerfactory 必须返回awaitable。缺少任何factory时进程应启动失败。
##3.2挂量Router(适合独立adapterASGIservice)
如果adapter 自己管理health、midleware、CoRS 或reverse-proxy prefix,可只挂公共router
    python
fron fastapl nport FastAPI
fronenterprise_agent_platformimport create_router
app=FastAPI()
app.include_router(
    create_router(create_container()),
    prefix="/api/agent-platfor
即使采用routermount,也建议该ASGIapp是可单独打包/带走的adapter service,而不是把平台内部 Store和 domainimports散落到主业务后端。
##3.3 API-onlylocal demo
从平台目录运行:
'bash
uv sync-project backend--frozen
uvrun-project backenduvicorn
    enterprise_agent_platform.reference.local_stack:create_app\
    -factory-host127.0.0.1-port 8080
该模块必须被显式点名才会启用。它使用固定syntheticidentity和进程内Store,仅允许创建,读取和取消syntheticRun: Doyou w extension
    "bash
                                        a Ln 1,Col1Sace2
嵌入现有业务系统指南
#3.后端组合方式
###3.3API-only local demo
    bash
curl--fail-with-body\
-H'Authorization:Bearer reference-local-demo'
-H*Idempotency-Key:demo-create-]'
H'Content-Type:application/json'\
    -data'{
    "workflow_type":"synthetic-analysis",
    "intent":"summarize synthetic signals",
    "resource_refs":I"synthetic-case:case-De1"],
    "paraneters":}
http://127.e.e.1:808e/v1/runs
返回QueuED只证明createpath 和 publiccontract 工作.localstack没有 scheduler/worker,不会执行这个 Run;需要参考完整状态流时运行reference vertical tests,而不是对 demo 增加宽松生产fallback。
4.PubLicAPI使用约定
16e 公营###
PosTvi/runs必须携带Idempotency-Key相同actor、tenant、operation、key 和相同 canonical request 返回原结果:同key 不同请求被拒绝。业务系统应在用户确认创建时生成稳定请求ID,不要在 HTTP retry 时重新生成。
Create body 只包含:
    workf low_type
    intent'
    个减多个opaque resource_refs
JSON parameters
可选host_context_ref
不要加入tenant_id、user role、Toollist、credential、Runner/Pod ID、callback URL或object-store path
#4.2乐观并发
壹询Run会返回ETag。cancel、rerun等会改变业务语义的操作应使用If-Match,避免用户基于I旧页面覆盖新状态。409VERSION_coNFLICT后重新取RunView并让用户确认,不要客户端循环强行覆盖。
#4.3SBpshot、事件分页与 SSE
推荐读取流程:
2.watermark建立 SSE:Last-Event-ID:watermark> 1.GET/v/runs/(run_id)取得完整RunViewSnapshot 和watermark;
3.SSE断开后用周一cursor 重连;
4.收到序列间时调用event repLay;
5.服务返回"RESYNC_REQUIRED时重新获取完整snapshot。
不要把NATS或SSE内存消息当UI状态.SDKRunProjectionStore 会丢弃重复 seq、缓冲乱序、用REST replay 修补 gap,并在 retention fLoor 超出时从 snapshot resync。
#4.4稳定课合同
前端按ApirrorEnvelope.code判断认证、权限、冲突、validation、resync或,retryabledependency error。不要解析英文message;message 是安全显示文本,不是程序协议。
4.5已知失败Effect的复命令
Effect已收效为FAILEDRun/ExecutionUnit/Step 处于NEEDS_ATTENTION时,有权操作者可调用
POST/v1/runs/(run_id)/effects/feffect_id}/recover
Authorization:Bearer ctokenwitheffects:recover>
If-Match:"<current-run-version"
Idempotency-Key:<stable-recovery-request-id>
该路由强制要求effects:recover
                        scope.强If-atchRun version 和Idempotency-Key:缺少 precondition 会拒绝,version 冲突后应重新读取 RunView,不得用循环覆盖新状态.相同 acter/tenant/operation/key 只能对应相同 canonicalrequest
成功鸣应为更新后的RunViewSnapshot和ETag,博FAILEDEffectLedger 保特不变:Run/ExecutionUnit 转为RECOVERING、Step 转为ACTIVEscheduler 从原 coNMITTEDCheckpoint 创建新 Attempt.successor Agent 必须重新规划、生成新 ActionProposal 并获得新审批;该命令 绝不会重新dispatch I旧Effect. Doyouw
##5.前端SDK接入 extension
                                        Ln1,CoiSpace
#嵌入现有业务系统指南
4.PubLicAPI 使用约定
##5.前端SDK接入
###5.1 Packages
    text
@platform/agent-ui-protocol wire types+Zod validators
@platform/agent-ui-client @platform/agent-ui-catalog REST/SSEclient+failed-Effect recovery +projection store allowlisted component renderer
                        Provider+AgentPanel
5.2最小React组合
    tsx
import{AgentPlatformClient)from"@platform/agent-ui-client"; inport{
AgentPanel,
AgentplatformProvider,
}from"@platform/agent-ui-react";
importtype(HostBridgeCapabilities}from"@platform/agent-ui-protocol/host";
const client=new AgentPlatformClient({
baseurl:"/api/agent-platform/",
getAccessToken:()=>identityClient.getShortLivedToken(),
const hostBridge:HostBridgeCapabilities={
schema_version:"host-bridge-capabilities/v1",
navigate:async((destination_ref })=>{
    hostRouter.openStableDestination(destination_ref);
dowmloadAuthorizedArtifact:async({authorization })=>
    await down loadManager.download(authorization);
export function EmbeddedAnalysisPanel(){
    returm(
    <AgentPlatfonmProvider client={client} hostBridge={hostBridge}>
    gentPanelrun={runSummary} surface={currentSurface)/>
    </AgentPlatformProvider>
    :
identityClient hostRouter" 和down loadManager 是宿主自己的对象,SDK不需要知道业务ORM、页面全局store或身份token的长期保存位置。
对已知FAILEDEffect,宿主可以把经授权的操作员意图交给client:
    ts
constrecovered=awaitclient.recoverFailedEffect(run.run_id,effect,effect_id,( expectedRunVersion:run,version,
idempotencyKey:recoveryRequestId,
recoverFailedEffect
                会URL-encodePun/EffectID,发送POST、强If-Match和Idempotency-Key,严格解析RunViewSnapshot并校验返回的run_id与请求一致,SDk不会自动在冲突后重试,也不会替操作者获得effects:recover权限。
###5.3 Host Bridge 边界
Host Bridge只允许三类能力
获取audience=enterprise-agent-platfors的短期access token;
按stabledestination_ref请求宿主导航; 处理服务端已经签发的ArtifactDownloadAuthorization。 Do youw
                                        extension
Bridge 不向 Surface 露任意fetch、eval.DOM、router object.credentialstore 或业务API client
                                        QLn1.Coi1Spac
嵌入现有业务系统指南
#非5.前端SDK接入
###5.3 Host Bridge 边界
Bridge不向 Surface需任意fetch、eval、DoM、routerobject、credential store 或业务API client。
##6.A2UI Surface与动作
###6.1染
服务端只允许固定catalog和protocolversion,SDKswitch 渲染ProgressCardEvidenceSummary
Surfacerevision 包含 ApprovalCard与ArtifactCard;未知component 显示Unsupportedcomponent不会动态下载代码
stablesurface_idrun_idrevision;
sourceAttempt和 source event seq
canonicaldocument 和checksum
业务页面不得自行把型文本拼成 HTML,也不要绕过validator 直接调用动态组件registry。
#6.2ApprovalAction
ApprovalCard显示服务端canonical target 与request digest,点击后 SDK 发送:
    json
    "run_ie":"run_stable_id",
    "surface_id":"surface_stable_id",
"surface_revision":2,
    "action_ref":"approval:stable-id:approve"
3e5 "cLient_action_id":"action_unique_id", "displayed_digest":"sha256:server-request"
Action 不包含approval_id、decision scope、Tool、Connector、target、payload 或credential,服务端从 imutable Surface 反查 Approval,并通过ApprovaLDecisionService再次验证权限、摘要、状态、版本和过期时间,
60E
6.3Artifact 下载
浏览器直接访问短期HTTPSURL;
经过宿主downloadproxy做额外审计/DLP;
在受限桌面环境调用企业下载管理器。
不要把S3objectkey、长期URL或S3credential放进Surface。
7.反向代理与浏宽器策略
推荐同通路径/api/agent-platform/,由网关完成:
TLS terination和upstream mTLS;
-public/intermal route ;
-request body、header 和连接时长限制;
    SSE禁用代理buffering,并配置合理idle/Lifetime;
Authorization.Cookie.query 和 signed URL 日志脱敏: CSRF 策略与业务认证模式一致;
ratelinit按tenant/actor/operation,而不是仅按IP
若跨域部署,只allowlist明origin/method/header,不使用credentialedwildcard coRs,Internal Runtime/Effect API 必须使用独立 hostname/network poLicy,不能因反向代理prefix 配错而暴露到浏览器
##8,版本与发布兼容
OpenAPI、JSON Schema.event payload 和AzUI protocol 都有显式 后端新增optional字段前先确认前端strict Zodschema 的兼容策略; tschema_version
breakingchange 发布新version和迁移蜜口,不原地改变旧字段含义; SDK解析失败要显示安全错误并停止动作,不"尽力测";
    宿主adapter与平台wheel独立版本,通过contract fixtures做CI compatibility test;
    先部署能同时读取old/new的consumer,再切producer,最后清理i旧合同,
##9,生产接入验收清单 nooa
【】平台目录可在空父目录独立复制、构建、安装和执行L1 extension
-【】Adapter 只依赖根包公开边界,没有平台对宿主源码的反向import
                                        Ln1,Col1Spce:2 UTF
##7.反向代理与浏览器策略
CSRF策略与业务认证模式一致:
rate Limit 按tenant/actor/operation,而不是仅按IP
若跨域部署,只allowlist 明醇origin/method/header,不使用credentialedwildcard coRsInternal Runtime/Effect API 必须使用独立hostname/networkpolicy,不能因反向代理prefix配错而暴露到浏览器。
#8、版本与发布兼容
OpenAPI、JSON Schema、event payload 和 AzuI protocol 都有显式schema_version
-后端新增optional字民前先确认前端strict Zodschema 的兼容策略:
breakingchange发布新version 和迁移窗口,不原地改变旧字段含义;
SDK解析失败要显示安全错误并停止动作,不"尽力猜测":
宿主adapter与平台wheel独立版本,通过contractfixtures 做CIcompatibilitytest;
先部著能同时读取eld/new的consumer,再切producer,最后清理旧合同。
#9.生产接入验收清单
】平台目录可在空父目录独立复制、构建、安装和执行L1。
[]Adapter只依赖根包公开边界,没有平台对宿主源码的反向import.
    Authcontext 的tenant/actor/scopes 由可信token派生
    Resource/Context/PolicyPorts对跨租户和timeout failclosed.
    ]PostgresOL schema、migration、backup/PITR 与 connection limits 已评审 []NATS只做通知,Outbox/Inbox在数据库中启用。
    []S3versioning、checksum、retention、scan 和 download policy已验证。
    READ Tool scopes、grant、resource prefix、timeout 和 result size 已登记。
    []wRITEConnector支持effect-key 等和 UNKNDwNreconciliation
]Effectwatchdog只在executorease按数据库时间到期后标记UNKNOwN: effects:recover只授予可发起重新规划的操作者,宿主正确传递强If-Match与幂等键。 FAILED reconciliation 校验executor inactive 和 stable observation.
    ]Internal API与publicAPI 网络、DNS、identity和证书分离。
    Sandbox无KubernetesRBAC、无Secretmount、无任意egress,强化runtime 完成L4
    []前只用allowlisted A2uI catalog,Host Bridge 无任意 fetch/eval 能力。
】L1、L2、L3均有当前版本证据,L4在目标生产等价环境单独签署。
                                        Do you w
                                        extension
                                        QLn1,Col1Space:2
