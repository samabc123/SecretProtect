# 改造前备份

时间：2026-11-13。原提交：`9d23ebb2cc43c525617ac53de9c88d390cd10065`。

已先停止原本机服务，再打包源码、运行数据和原依赖目录；打包完成后进行了
7-Zip 全包完整性校验及 `git bundle verify`。两个检查均成功。

本地目录：`_backups/before-SecertProtect-20261113/`。此目录不提交 Git。

| 文件 | 字节数 | SHA-256 |
| --- | --- | --- |
| SecretRadar-complete.zip | 146618392 | AD15AC2C042472EB15D439CCF02E0C63EB38DA0C91EC78CD7D08769BA98AA1A8 |
| repository.bundle | 18554525 | 36D39F82904ADD2B8B8B5AB664BB87CF15D0EA795DDC7F51CEED70283530954E |

ZIP 包含 34872 个文件，原源码目录也留在本机，没有物理删除。
Git bundle 保留改造前完整 Git 历史。恢复步骤见 DEPLOYMENT.md。

备份包含私人运行数据和可能的配置秘密，应保持受限访问，不上传仓库或公开分享。
新旧管理员与准入数据库通过 SQLite `integrity_check`，迁移时记录数量一致；
没有将用户个人信息写入此报告。
