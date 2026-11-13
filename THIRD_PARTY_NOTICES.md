# 第三方组件

SecertProtect 使用 CrowdSec 作为独立安全引擎。

- 上游：`https://github.com/crowdsecurity/crowdsec`
- 固定版本：`v1.8.2`
- 固定提交：`3d369d64eb3e9ba770ed39aedc83ffd3170aef4c`
- 源码位置：`engine/crowdsec` Git 子模块。
- CrowdSec 版权及 MIT 许可：见 `engine/crowdsec/LICENSE`，不得删除。

该仓库不是 CrowdSec 官方产品，也不包含 CrowdSec Console 商业服务源码。
管理平台沿用原项目的 FastAPI、Vue、Element Plus 和已有准入、凭证检测模块。
各依赖及规则集分别遵循其原始许可证；CrowdSec MIT 许可不自动覆盖全部依赖。
新管理界面默认简体中文，上游引擎和规则标识保持原始形式。
