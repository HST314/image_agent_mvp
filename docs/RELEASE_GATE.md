# 一期自动化发布门

候审版本必须通过完整 `pytest`、静态编译和 `scripts/release_install_gate.py`。后者在源码树外构建 wheel、检查依赖元数据、安装 wheel、执行离线五候选最小流程，并使用锁定依赖再次运行全套测试。离线 fixture 必须可重复执行。

CI 的 Linux 安装态入口为 `python scripts/release_install_gate.py`。仓库托管工作流还必须在 `windows-latest` 上从 `requirements.lock` 全新安装依赖，并运行 `tests/test_windows_portalocker_backend.py`；该测试不 mock `portalocker.lock/unlock`，会实际调用 Windows 后端并验证 `GET /api/projects/{project_id}/branches` 返回 200。两个 job 都是发布阻塞门禁。

真实供应商 smoke 默认禁用，仅允许在受控凭据、明确费用确认和不会重放既有调用的环境中人工启用。CI 日志不得记录密钥或完整供应商载荷。

## 回滚

1. 停止接收新的后台 job，并等待运行中调用进入确定终态；结果未知的调用不得自动重试。
2. 将部署版本切回最近一个已验收 commit/wheel；不要覆盖项目目录。
3. 启动后调用 `/api/health`，确认全部探针为 `ok`，再恢复流量。
4. 保留新版本期间产生的 checkpoint、event、asset 和 job 审计记录；按分支恢复，不做原地历史覆盖。

## 已知限制

- 进程内在途 job 在服务重启后会标记为 `interrupted`，不会恢复执行或自动补调用。
- 旧版本遗留的临时 URL 可能失效；一期不迁移旧资产。
- Event/Prompt 暂无额外查询索引，长历史查询性能优化后置。
