# SecertProtect 0.1.0 本地验收

日期：2026-11-13，北京时间。此报告不代表生产验收。

## 已执行检查

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 原项目 ZIP 完整性 | 通过 | 7-Zip 全包检查 |
| Git 历史备份 | 通过 | git bundle verify |
| 原/新管理员与名单数据库 | 通过 | integrity_check、记录数量核对，不输出个人资料 |
| 独立 Python 3.12 虚拟环境 | 通过 | requirements 安装与 pip check |
| 后端 unittest | 117 项通过 | 原有 96 项及新防护 21 项 |
| 前端 Node 测试 | 4 项通过 | CSV 安全、排序、64 位平台 ID |
| Vue 生产构建 | 通过 | npm ci、npm run build |
| ESLint | 0 错误 | 两个旧页面有 13 个缩进警告；新页面无警告 |
| 准入浏览器回归 | 14 个流程通过 | 登录、审批、导入、恢复、批量删除、权限与响应式布局 |
| 新防护浏览器回归 | 通过 | 真实空状态、详情、处置持久化、筛选、导出、只读解封和待发送状态 |
| Windows 启停 | 通过 | 系统 Python 与 venv 均测试；停止后确认监听端口释放 |
| 引擎 Compose | 静态配置校验通过 | 没有运行容器 |

后端新增覆盖：机器 JWT 与 Bouncer Key 分离、身份与 CSRF、TLS 代理 Cookie、
拒绝伪造转发头、响应大小限制、拒绝重定向、拒绝远程明文引擎地址、
删除权限开关、失败不抹掉快照、失败审计、过期租约恢复、同步互斥、
通知去重与重试、未配置不假送达、去除上下文与非 IP 来源信息。

新页面视口：1440×1000、1024×768、768×1024、390×844、320×720。
准入页面另覆盖 1904×930、320×568、844×390。浏览器未记录 pageerror。
已人工查看桌面总览、320px 手机总览和登录页截图。

本地自动化报告与截图：

- `platform/.test-results/secertprotect/browser-results.json`
- `platform/.test-results/secertprotect/overview-1440.png`
- `platform/.test-results/secertprotect/overview-320.png`
- `platform/.test-results/samlab/browser-results.json`

报告字段明确为 `synthetic_only=true`、`production_changed=false`；
浏览器测试没有触发真实网站扫描，没有使用真实有效凭据。
SMTP 单元测试使用隔离替身，不是实际送达证据。
CrowdSec 适配器按固定上游源码契约测试，不是实际引擎联调证据。

## 未完成

本机 Docker 守护进程不可用，因此没有启动 CrowdSec、运行规则检测或执行 Bouncer。
真实 SMTP、Samlab 账号准入和生产反向代理尚未部署验收。
没有修改 DNS、服务器业务源码、生产账号或任何生产运行数据。

新管理服务与独立同步 worker 在本机运行；没有凭据时保持未配置状态，
不主动连接生产平台。凭证检测调度器没有自动启动。
安全生产门槛及回滚方法见 DEPLOYMENT.md。
