# Image Agent MVP

> 文件型、可恢复、支持无损分支回退与多模态图生图的图片创作 Agent 引擎。

Image Agent MVP 把图片创作组织成一条可审计、可暂停、可恢复的工程化工作流。每个成功节点都会形成原子 Checkpoint；失败不会覆盖最后一次成功状态；`rewind` 会从历史快照创建新分支，保留原分支的 Prompt、决策和图片资产。

核心能力：

- **5 选 1 直通管线**：五个差异化方向生成后，人工直接选定一张主图，不存在旧版“三选”中间阶段。
- **自然中文交互**：默认界面由 Presenter 渲染，不向使用者暴露内部英文 Key；`--debug` 才显示技术详情。
- **强类型契约**：Pydantic V2 模型负责机器契约，用户视图与领域数据解耦。
- **可恢复工作区**：事件、Prompt 链、原子 Checkpoint、资产和分支树均持久化到独立工程目录。
- **无损 Rewind**：从任意有效 Checkpoint 派生新分支，不修改旧历史。
- **状态级多模型路由**：推理、视觉理解、初稿生图、自检返工和人工返工可分别绑定模型；配置仅在状态或迭代边界热加载。
- **多模态 I2I**：上一轮图片作为单图或有序多图参考，通过 Ark 兼容载荷的 `extra_body.image` 传递。
- **可靠远程调用**：调用前记录完整 Prompt，提供超时、重试、错误分类、敏感字段脱敏和幂等复用基础。

## 核心工作流

```text
资料导入 / 必要澄清
          │
          ▼
    创作任务书确认
          │
          ▼
  生成 5 个差异化方向
          │
          ▼
    人工直选 1 张主图
          │
          ▼
 VLM 逐轮质检 / I2I 返工 ──┐
          │                 │
          └──── 下一轮复检 ◀┘
          │
          ▼
  人工自然语言修改（可多轮）
          │
          ▼
       最终人工交付
```

## 快速开始

### 1. 环境要求与安装

- Python 3.10+
- 推荐使用虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.lock
```

配置模型供应商凭证。默认 `configs/model_config.yaml` 使用 Ark：

```bash
export ARK_API_KEY="your-api-key"
```

凭证只从环境变量读取，不应写入 YAML、任务文件或工程目录。路由还支持按 provider 读取 `OPENAI_API_KEY` 或 `VLM_API_KEY`。没有真实凭证时，可显式使用 `--offline` 做流程测试；模拟图片不能最终交付。

### 2. 准备任务

`--task` 接收符合 `ImageTaskCard` 的 JSON。可复制示例开始：

```bash
cp examples/sample_image_task_card.json /tmp/my-task.json
```

### 3. 创建并推进工程

```bash
python main.py new demo --task examples\sample_image_task_card.json
```

流程遇到澄清、任务书确认、主图选择、逐轮放行或最终确认时会安全暂停。补充相应参数后运行 `resume`，不会重复已完成的付费步骤：

```bash
python3 main.py resume demo --selected-id <候选编号>
python3 main.py resume demo --manual-action execute
python3 main.py resume demo --human-prompt "增强主体金属质感，背景更克制"
python3 main.py resume demo --approve-final
```

### 常用命令

| 命令 | 用途 |
|---|---|
| `new <project_id> --task <path>` | 创建工程并启动工作流；同名工程不会被覆盖 |
| `resume <project_id>` | 从最后成功 Checkpoint 的下一状态或人工等待点继续 |
| `retry <project_id>` | 从上一成功点创建新分支，重试已记录的失败状态 |
| `rewind <project_id> --from <checkpoint> --name <branch>` | 从历史 Checkpoint 创建无损分支；加 `--continue` 立即推进 |
| `repair-project <project_id> --dry-run` | 只读检查 manifest、索引和 Checkpoint 文件的一致性 |
| `repair-project <project_id> --apply` | 备份控制文件后，按 checksum 修复唯一可确认的索引悬空引用 |
| `history <project_id>` | 查看自然中文审计时间线和分支信息 |
| `inspect <project_id>` | 查看工程技术信息；需配合全局 `--debug` 显示 JSON |

全局参数必须放在子命令之前，例如：

```bash
python3 main.py --projects-root ./projects --debug inspect demo
```

完整 CLI、人工动作和故障恢复示例见 [用户与开发者指南](docs/user_and_developer_guide.md)。

## 配置指引

### `configs/model_config.yaml`

`state_bindings` 为模型调用节点分别配置 `model_role`、`provider`、`model`、`parameters` 和可选 `fallback_model`：

| 节点 | 职责 | 角色 |
|---|---|---|
| `intake_clarify` | 需求澄清 | `reasoning_llm` |
| `confirmation_build` | 任务书生成 | `reasoning_llm` |
| `initial_candidate_generation` | 五张初稿 | `text_to_image_model` |
| `self_check_inspection` | VLM 画面质检 | `vision_language_model` |
| `self_check_rework` | 自检 I2I 返工 | `text_to_image_model` |
| `human_prompt_rework` | 人工 Prompt 返工 | `text_to_image_model` |

可用 `--model-config <path>` 为一次 `new`、`resume`、`retry` 或带 `--continue` 的 `rewind` 指定配置。路由在状态/迭代边界重读文件，单次模型调用中途不会切换配置。

### `configs/runtime.yaml`

该文件描述运行策略，包括澄清问题数量与总预算、渲染/质检重试次数、输出尺寸，以及 `self_check` 的终止和逐轮放行策略。四种组合及边界语义详见完整指南。

## 项目结构

```text
image_agent_mvp/
├── agent_core/       # Pydantic 契约、显式状态迁移、门禁与生产 Runner
├── storage/          # 工程、Checkpoint、事件、Prompt、资产和分支持久化
├── interaction/      # 澄清、任务书、人工审批与中文 Presenter
├── model_router/     # 状态级路由、能力校验、热加载和可靠调用执行器
├── calibrator/       # VLM 质检、终止/放行策略与 I2I 返工循环
├── prompt_engine/    # 模板、版本、组合与多模态上下文预算
├── render_clients/   # Ark 图片调用与单图/多图载荷映射
├── skills/           # 类目能力、风格卡和注册索引
├── configs/          # 模型路由与运行时策略
├── workflows/        # 声明式工作流状态与迁移说明
├── schemas/          # 对外 JSON Schema
├── tests/            # 单元测试和端到端恢复/并发测试
├── docs/             # 架构、迁移、用户及开发者文档
├── workspace_cli.py  # CLI 实现
└── main.py           # 命令行入口
```

## 质量验证

```bash
python3 main.py --help
python3 -m pytest -q
```

测试使用 Fake Clients / 离线客户端验证模型调用链，不消耗真实图片生成额度。
