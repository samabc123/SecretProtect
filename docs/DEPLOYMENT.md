# 部署与验收

本仓库是单实验室平台试点版本。部署模板不代表已在 Samlab 生产环境验收。

## 结构与依赖

管理服务使用 Python 3.12、FastAPI、SQLite；前端使用 Vue 3、Element Plus、Vite。
CrowdSec 固定在 v1.8.2，源码以 Git 子模块保留，可独立编译修改，不与管理后台耦合。
版本固定不等于镜像摘要固定；生产发布前还需将通过验收的镜像锁定到 digest。

```sh
git clone --recurse-submodules https://github.com/samabc123/secretradar.git secertprotect
cd secertprotect
python3.12 -m venv platform/.venv
platform/.venv/bin/pip install -r platform/requirements.txt
cd platform/frontend
npm ci
npm run build
```

Python 直接依赖已固定版本，前端有 package-lock.json；Python 传递依赖尚未做跨平台哈希锁定。
完整离线、字节级可复现构建不是本版本的验收结论。

## 管理服务

1. 创建专用低权限用户 `secertprotect`，目录按 systemd 模板放置在 `/opt/secertprotect`。
2. 仅给该用户 `platform/data` 写权限；配置与秘密文件放在 `/etc/secertprotect`，目录 0700、文件 0600。
3. 使用 `deploy/secertprotect.env.example` 配置 `runtime.env`，不把真实值提交 Git。
4. 新数据库首次启动需要 `SECERTPROTECT_BOOTSTRAP_PASSWORD_FILE`。通过服务器安全编辑器创建文件，不在命令参数、聊天或日志中输出密码；完成首次修改后移除 bootstrap 配置及文件。已有 admin 保留原密码。
5. 配置路径、用户权限后安装两个 systemd 单元。凭证检测仍使用原独立调度器 `python -m app.run_backend_scheduler`，必须先确认授权目标，不能误以为 CrowdSec worker 会启动凭证扫描。
6. 本机开发保持回环访问。公网部署先配置唯一 `SECERTPROTECT_PUBLIC_ORIGIN=https://实际域名`，再配置同机 Nginx HTTPS。应用仍只接收回环来源；禁止直接暴露 8001，禁止删除 localhost 安全边界，禁止启用不受控的代理头信任。
7. 管理登录与 CSRF 校验始终启用。反向代理模式 Cookie 为 Secure；当前登录限速按代理地址聚合，会影响同一代理后的管理员，需纳入试点容量测试。

```sh
sudo systemctl start secertprotect-web secertprotect-worker
sudo systemctl status secertprotect-web secertprotect-worker
sudo systemctl stop secertprotect-web secertprotect-worker
```

当前兼容密码长度仍为 6–128。公网正式上线前必须确定更强的密码规则及管理员 MFA；
本版本没有实现 MFA，不能据此宣称达到生产身份安全要求。

## CrowdSec

在明确授权的测试服务器设置 `SAMLAB_NGINX_LOG_DIR` 后：

```sh
docker compose -f deploy/crowdsec.compose.yml config
docker compose -f deploy/crowdsec.compose.yml up -d
docker compose -f deploy/crowdsec.compose.yml exec crowdsec cscli lapi status
```

默认关闭在线社区 API，默认 simulation 模式，只观察不实施封禁。
授权 Nginx 日志通过只读挂载输入；无需挂载整个宿主机，也不挂载 Docker socket。
确认日志格式、真实客户端 IP、代理可信范围和日志采集正常后，再单独验收 Bouncer。
不得将实验室共享出口 IP 直接等同于人员身份。

需要两种独立身份，不能混用：

- `CROWDSEC_CREDENTIALS_FILE`：官方 `cscli machines` 创建并批准的机器身份 YAML（url/login/password），用于 JWT 读取事件和受控删除决策。
- `CROWDSEC_BOUNCER_KEY_FILE`：专用只读决策查询密钥，由 `cscli bouncers` 创建，以受保护文件提供。它不是管理后台登录凭据，也不是实际执行拦截的 Bouncer。

用受限文件重定向或服务器密钥管理工具保存创建结果，避免终端历史、日志或聊天泄露。
默认不允许浏览器更改引擎地址、不允许 arbitrary API 代理、不允许运行 shell 命令。
远程引擎只允许 HTTPS，支持自定义 CA，不允许关闭证书校验。
解封需服务器显式设置 `CROWDSEC_ALLOW_DELETE=true`、管理员会话、CSRF、二次确认和原因；
引擎本身的 trusted_ips 规则仍生效。只允许单条删除，没有批量全网解封入口。

## 邮件

配置 SMTP over TLS 的主机、端口、发件地址、收件地址及密码文件。建议复用现有邮箱。
默认不发送；未配置时保留待发送记录，不冒充实际送达。
发送失败持久化退避重试，最多五次；页面显示实际发送结果。
SMTP 接受邮件不代表收件人已阅读。发送成功后进程崩溃仍可能重复投递，
因此这是带稳定 Message-ID 的至少一次投递，不是 exactly-once。

## 当前边界与上线清单

- API 适配器按上游 v1.8.2 路由实现，单次最多接收最近 100 条告警；本地列表最多展示 1000 条，防护审计和投递记录展示最近 200 条。
- 超过同步窗口的停机积压可能丢失，需要后续增量游标/补偿回补，不能承诺全量日志归档。
- 来源名称、原始事件、上下文、请求体不入库；IP、规则标识等结构字段仍属于需要保护的安全数据。
- 连接成功、存在封禁决策、真实请求被阻挡，是三个不同验收条件。
- 不安装真实执行组件、不修改 Samlab 源码时，不能宣称完成网站拦截或账号访问控制。
- 现有人员名单和准入接口保留；当前 CSV/手动添加仍要求已有平台 ID。新用户无平台 ID 的准入模型、历史账号范围隔离与 Samlab 生产联动尚待实施。
- 凭证检测沿用旧实现及测试边界，不宣称 CrowdSec 覆盖凭证泄露，也不宣称凭证检测覆盖所有科研资料。
- WAF 与 Bouncer 必须先验证登录、API 客户端、流式 SSE、下载以及共享出口误封；保留独立受控紧急运维入口。
- 生产需另行完成资源限制、通知失败告警、数据留存策略、MFA、备份恢复演练及真实授权环境验收。

## 回滚与备份

改造前本机备份：`_backups/before-SecertProtect-20261113/SecretRadar-complete.zip` 和 `repository.bundle`。
ZIP 含源码、原运行数据和依赖文件；bundle 含完整 Git 历史。均仅本地保存，不上传。
备份含私人运行信息，不能作为普通发布包分发。

停止新服务后，将 ZIP 解压到新的恢复目录，核对数据库与源码，不直接覆盖运行数据库。
改造前 Git 提交：`9d23ebb2cc43c525617ac53de9c88d390cd10065`。
远端保留正常提交历史；无需强推回滚。可在单独目录检出旧提交，并从私有备份恢复数据。
新服务后产生的变更需先备份，避免回滚时丢失新增审批与审计。
