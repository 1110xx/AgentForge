#凭据轮换
平台只引用 ExternalSecret;凭据值不得写入 Git、Helmvalues、日志、trace 或 Artifact。
1.盘点PostgreSQL、NATS、S3、OIDCclient、OTLPexporter 与镜像仓库凭据的owner、TTL和使用者。
2.在secretmanager 创建新版本,保持l日版本短时有效;External Secrets 同步到目标Secret。
3.滚动重启control-plane/worker;SandboxAttempt 不获得这些长期依赖凭据,只使用短时runtime capability。
4.验证新连接、审计与最低权限,再撤销旧版本。
5.检查日志与 telemetry 未出现header、query、token 或user identifier
恢复门禁:所有replica使用新版本、旧版本已撒销、进行一次跨租户与未授权wRITE的负向验证。轮换失败时回滚secretversion,不放宽权限。
