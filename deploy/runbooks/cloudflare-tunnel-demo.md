# Cloudflare Tunnel 面试演示（域名 tyx-lab.online → 本机 kind）

> 2026-09-03。目标：让面试官访问 `https://agent-platform.tyx-lab.online` 直达本机 kind 集群的
> ingress-nginx。**不用迁移集群、不用云服务器**；隧道免费；唯一常驻条件=本机开机（Pod 形态只
> 要 kind 在跑；终端形态要两个终端开着）。
>
> ⚠️ **证书口径（诚实）**：Cloudflare 代理（橙色云）下，浏览器看到的是 **Cloudflare 的
> Universal SSL 证书**（仍是绿色锁、HTTPS 合法）。我们之前 cert-manager 签的真实 LE 证书只在
> origin 直连（https 443）或 CF SSL 模式=Full(strict) 时才被使用。演示层面两者都「安全」，
> 但别在面试里说「浏览器看到的就是我们自己签的证书」——CF 边到边默认是它自己的。
>
> origin 现状（已实证）：ingress 已切真 host `agent-platform.tyx-lab.online`；因隧道走 CF
> Flexible 默认（CF→origin 用 http），已给 ingress 加 `nginx.ingress.kubernetes.io/ssl-redirect=false`
> （kubectl annotate，未入库）；HTTP origin 探测 root/live/ready 全 200。
> **正式生产**想保留 https origin + Full(strict) 时：删掉该 annotation 并把 tunnel service 改
> `https://ingress-nginx-controller.ingress-nginx.svc.cluster.local:443`。

## 0. 材料
- cloudflared.exe 已下载：`C:\Users\唐雨欣\cloudflared\cloudflared.exe`（v2026.8.3）
- CF zone `tyx-lab.online` active；API token 仅 Zone/DNS 权限（**不能**建隧道 → 需下面登录授权）
- 演示站点=kind 里已部署的 prod-form 平台（GHCR digest 镜像 + ESO + 真实 LE issuer）

## 0b. 中国网络实测修正（2026-09-03，宿主形态）
- CF 边缘 **QUIC/UDP 7844 被干扰**（`CRYPTO_ERROR 0x178 tls: no application protocol`），
  必须强制 http2：config 顶层加 `protocol: http2`（已写入 `~/.cloudflared/config.yml`）。
- kubectl port-forward 用 `80:80` 写法（`127.0.0.1:80` 简写在本机 kubectl 报解析错）。
- 上线成功证据：`Registered tunnel connection … location=hkg10 protocol=http2`；公网
  ready/live/root 全 200；证书为 CF Universal SSL（subject CN=tyx-lab.online，
  issuer Google Trust Services WE1），绿锁。
- 启动器已就绪：`C:\Users\唐雨欣\cloudflared\01-ingress-forward.bat` +
  `02-tunnel-run.bat`（双击两个窗口，关闭即下线）。

## 1. 用户交互三步（只有你能做，浏览器授权）
在 `C:\Users\唐雨欣\cloudflared` 打开终端：
```bash
.\cloudflared.exe tunnel login          # 浏览器登录 CF → 选 tyx-lab.online 授权 → 生成 cert.pem
.\cloudflared.exe tunnel create demo-tunnel   # 记下 Tunnel-UUID；生成 ~/.cloudflared/<UUID>.json
.\cloudflared.exe tunnel route dns demo-tunnel agent-platform.tyx-lab.online   # 自动建 CNAME
```

## 2. 形态 A：主机终端形态（最简单）
两个终端必须常开：
```bash
# 终端1：本机转发到 ingress（80）
kubectl -n ingress-nginx port-forward svc/ingress-nginx-controller 127.0.0.1:80
# 终端2：隧道（注意：run 不用 --url；用 config 或 --url quick tunnel 二选一）
cloudflared.exe tunnel run demo-tunnel    # 读取 ~/.cloudflared/config.yml
```
~/.cloudflared/config.yml 形如：
```yaml
tunnel: demo-tunnel
credentials-file: C:\Users\唐雨欣\.cloudflared\<UUID>.json
ingress:
  - hostname: agent-platform.tyx-lab.online
    service: http://127.0.0.1:80
  - service: http_status:404
```

## 3. 形态 B：Pod 常驻集群（原推荐，暂时受阻）
> ⚠️ 2026-09-03 实况：本机 kind **Pod 出网 IPv4 当前不通**（探测证据：Pod 内 DNS 正常，但
> 显式 IP 连 1.1.1.1:443 / CF 边缘 7844/443 全部超时，docker hub 也不通；节点镜像拉取正常；宿主
> 直连全通）。疑 Docker Desktop/宿主网络层异常，非本项目代码。**当前演示走形态 A（宿主）**；
> 网络修复后随时可切回（部署已备好，`kubectl -n agent-platform-control scale deploy
> cloudflared-tunnel --replicas=1`，且 args 已带 `--protocol http2`）。
用户做完第 1 步后，把凭据喂给脚本即可（脚本幂等，token/凭据只进 Secret 不入库）：
```bash
# 凭据文件在 ~/.cloudflared/<UUID>.json
bash scripts/apply-cloudflare-tunnel.sh demo-tunnel "C:\Users\唐雨欣\.cloudflared\<UUID>.json"
```
脚本做：生成 config（origin=`http://ingress-nginx-controller.ingress-nginx.svc.cluster.local:80`）→
`cloudflared-config` ConfigMap + `cloudflared-credentials` Secret（--from-file）→ 应用
`deploy/kind/cloudflared-tunnel.yaml`（Deployment，镜像 cloudflare/cloudflared:2026.8.3）→
rollout 等待 + 日志尾部。看到 `Connection registered to cloudflare edge` = 上线。
下线：`kubectl -n agent-platform-control delete deploy cloudflared-tunnel`。

## 4. 验证
```bash
# DNS CNAME（CF 侧）：应为 demo-tunnel.cfargotunnel.com（代理开）
curl -s -H "Authorization: Bearer $CFTOK" "https://api.cloudflare.com/client/v4/zones/<ZONE>/dns_records?name=agent-platform.tyx-lab.online"
# 浏览器 https://agent-platform.tyx-lab.online（绿锁）
# 或本机：curl -s https://agent-platform.tyx-lab.online/api/agent-platform/v1/health/ready
```

## 5. 排错
1. **8080 坑**：本机 8080 可能被旧的本地 `python run.py` 占着——探测 ingress 用别的端口
   （如 18080），别把 run.py 当 ingress。
2. CF DNS 页确认 CNAME 存在且云图标**橙色**（代理开启）。
3. Ingress host 已切真域名：`kubectl get ingress -n agent-platform-control agent-platform-api`。
4. 后端 Pod Running；隧道日志无报错。
5. 站点只在本机开机 +（形态 A）两终端 /（形态 B）kind+Pod 在跑时在线。

## 6. 收尾
- 面试完删隧道 DNS 记录（`cloudflared tunnel route dns --overwrite-dns` 或 CF API 删 CNAME）
  或留着续用（免费）。`cloudflared tunnel cleanup demo-tunnel` 后可 `tunnel delete demo-tunnel`。
- 形态 B 的 Pod/Secret/ConfigMap 删除命令见 §3；恢复 http→https origin 直连时删掉
  ssl-redirect=false 注解（`kubectl annotate ingress ... nginx.ingress.kubernetes.io/ssl-redirect-`）。
