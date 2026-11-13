# SecertProtect

面向单实验室平台的中文安全管理工作台，以 CrowdSec 为独立防护引擎，
复用原项目的管理员鉴权、人员白名单与凭证检测模块。

**当前为 0.1.0 本地开发版本，不是 Samlab 生产上线声明。**
没有引擎连接时展示真实未配置状态，不使用模拟告警或虚假防护统计。

## 目录

| 目录 | 内容 |
| --- | --- |
| `engine/crowdsec` | CrowdSec v1.8.2 源码子模块，固定提交并保留 MIT 许可 |
| `platform/app` | FastAPI 管理服务、准入、凭证检测、CrowdSec 适配器 |
| `platform/frontend` | Vue 3 + Element Plus 简体中文界面 |
| `platform/tests` | 后端回归与隔离浏览器验收 |
| `scripts` | Windows 启停及跨平台服务入口 |
| `deploy` | 引擎 Compose、只读采集、systemd 和 HTTPS 代理模板 |
| `docs/DEPLOYMENT.md` | 接入步骤、生产边界、备份与回滚 |

原项目不再作为当前分支的工作目录发布。完整 ZIP、运行数据和 Git bundle
仅保留在本机 `_backups` 中，旧代码提交历史保留，不强制覆盖远端历史。

## 已实现

- 中文安全总览、攻击事件、封禁决策、引擎状态、防护审计和邮件投递记录。
- 独立机器 JWT 与只读决策 API Key；凭据只在服务器受保护文件中配置。
- 事件结构字段入库、去重、人工处置、筛选和脱敏 JSON 导出；不存储原始请求体或告警上下文。
- 手工同步与独立 worker，SQLite 持久化状态、同步租约、失败退避和过期租约恢复。
- 默认只读决策；服务器显式开启后支持单条解封、管理员确认、CSRF 和审计。
- SMTP TLS 通知、持久化 outbox、重试、发送结果；未配置或失败不显示送达成功。
- 原人员准入功能：永久白名单、灵活 CSV 表头、删除后重新导入、批量删除、手动添加、数值 ID 倒序和单次审批。
- 安全平台只有管理员可登录，只有超级管理员能创建其他管理员；首次安装无默认密码，现有账号保留。
- 保留原凭证检测模块和兼容接口，凭证检测不是 CrowdSec 本身提供的功能。

## 本机运行

依赖：Python 3.12、Node.js 与 npm。首次克隆需递归拉取子模块：

```powershell
git submodule update --init --recursive
python -m venv platform/.venv
.\platform\.venv\Scripts\python.exe -m pip install -r platform/requirements.txt
cd platform/frontend
npm ci
npm run build
cd ../..
```

全新数据库需用安全编辑器创建私有初始密码文件，并设置
`SECERTPROTECT_BOOTSTRAP_PASSWORD_FILE` 指向该文件。不要把密码放入 Git、
命令参数或聊天。已有本地 `admin` 数据库无需重新设置初始密码。

```powershell
.\scripts\start.ps1 -Python .\platform\.venv\Scripts\python.exe
.\scripts\stop.ps1
```

访问 `http://127.0.0.1:8001/`。端口已占用时脚本拒绝接管，
可以使用 `-Port 8002`。后台 worker 会启动，但没有真实引擎凭据时不发起连接。
运行日志在 `platform/data/runtime`，不入 Git。

## 测试

```powershell
.\platform\.venv\Scripts\python.exe -m pip install -r platform/requirements-dev.txt
cd platform
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.\.venv\Scripts\python.exe tests/verify_protection_browser.py
.\.venv\Scripts\python.exe tests/verify_admission_browser.py
```

浏览器测试使用安装的 Microsoft Edge 和临时数据库；合成事件不代表真实接入。
测试结果默认写入 `platform/.test-results`，不发布私人运行记录。

## 尚未完成的生产验收

- 本机 Docker 守护进程尚未启用，CrowdSec 真实引擎和 Bouncer 未实机联调。
- Samlab 注册、账号映射、撤销会话及 API 授权仍需要业务后端接入；名单存在不等于业务权限已生效。
- 历史账号不自动迁移或停用；现有需要平台 ID 的名单流程不能直接代替新账号预授权设计。
- SMTP 实际送达、日志格式、代理真实 IP、WAF、SSE 和误封恢复待授权环境测试。
- 引擎告警同步窗口为最近 100 条，不宣称无损全量采集；事件归属、检测准确率及拦截效果需真实证据。
- 凭证规则沿用旧版本边界，不能声称已经覆盖全部秘密或科研资料。
- 正式上线前还需管理员 MFA、生产密码策略、留存策略与备份恢复演练。

详见 [部署说明](docs/DEPLOYMENT.md) 和 [第三方声明](THIRD_PARTY_NOTICES.md)。
