#对象checksum异常处置
已提交Artifact或workspaceSnapshot读取后checksum不匹配属于committed dataloss,必须立即page。
1.隔离对应objectversion,停止向用户签发下载地址和从该Snapshot恢复。
2.以PostgresQL 中immutable object key、version、size、checksum 为期望事实;记录 S3version ID 和存储审计引用。
3.从objectversionhistory或已验证备份恢复到新objectversion:禁止覆盖/删除损坏版本来掩盖证据。
4.全量读取并重新计算checksum、扫描内容,再通过受控元数据事务切换可用版本。
5.查明Lifecycle、复制、客户端中断或存储故障范围,抽检同时间窗口对象。
恢复门禁:checksum/size/scan三者通过,旧损坏版本保留取证,Artifact元数据与审计原子提交。
