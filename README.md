# Image Agent

Image Agent 是企业级多智能体平面设计系统中的受管图片创作执行组件。主系统负责创建任务、固定任务配置并启动实例；Image Agent 负责把已确认的任务卡推进为可审计、可暂停、可恢复的图片创作工作流。

正式运行只有一个入口：主系统为实例生成只读运行文件，通过受管进程环境传入文件位置，并使用本机受保护的 Adapter 接口创建工程。仓库不携带可人工维护的模型路由或运行策略，Web 与 CLI 也不提供切换模型配置或测试模式的选项。

## 核心能力

- 五个差异化方向生成后人工选择主图。
- 澄清、任务书确认、候选选择、逐轮质检和最终交付均有明确等待点。
- 事件、Prompt、原子 Checkpoint、资产和分支树持久化到独立工程目录。
- 从有效 Checkpoint 创建新分支，不覆盖原历史。
- 推理、视觉理解、生图与返工按主系统固定的任务快照路由。
- 调用前记录审计信息，提供超时、错误分类、敏感字段脱敏和幂等边界。

## 受管工作流

```text
主系统确认计划与 TaskCard
          │
          ▼
受管 Adapter 创建 Image Agent 工程
          │
          ▼
资料导入 / 必要澄清 → 任务书确认
          │
          ▼
生成候选 → 选择主图 → VLM 质检 / 返工
          │
          ▼
人工确认并冻结最终交付
```

任务创建、继续推进、审批和交付下载均从主系统工作台完成。Image Agent 自带页面仅作为受管实例的工作区投影，不维护部署配置，也不创建第二份配置事实源。

## 开发与验证

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.lock
python3 -m pytest -q
```

测试配置与 fake clients 只位于测试夹具中，用于验证模型调用链而不产生真实费用；它们不会被正式入口加载。

## 项目结构

```text
image_agent_mvp/
├── agent_core/       # 契约、状态迁移、门禁与生产 Runner
├── storage/          # 工程、Checkpoint、事件、资产和分支
├── interaction/      # 澄清、任务书与人工审批
├── model_router/     # 状态级路由与调用边界
├── calibrator/       # VLM 质检与返工循环
├── render_clients/   # 图片调用载荷与客户端
├── frontend/         # 受管实例工作区
├── tests/            # 单元、集成、恢复与安全回归
├── docs/             # 架构和开发文档
├── workspace_cli.py  # 工程维护 CLI
└── main_front.py     # 受管 Web 适配层
```

面向任务使用者的完整流程见 [用户与开发者指南](docs/user_and_developer_guide.md)。
