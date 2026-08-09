# Image Agent MVP 用户与开发者指南

本文同时面向终端使用者、Prompt 工程师和二次开发者。所有示例均以仓库根目录为当前目录，并与当前 `workspace_cli.py`、`configs/model_config.yaml` 和 `configs/runtime.yaml` 保持一致。

## 一、终端用户与 Prompt 工程师指南

### 1. 安装与凭证

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.lock
export ARK_API_KEY="your-api-key"
```

Python 最低版本为 3.10。默认路由使用 Ark，因此需要 `ARK_API_KEY`。其他 provider 可使用 `OPENAI_API_KEY` 或 `VLM_API_KEY`。密钥不得写入模型配置、任务 JSON 或工程文件；Prompt 审计也会按字段名对 key、token、authorization、secret 等敏感值脱敏。

### 2. CLI 语法约定

```text
python3 main.py [全局参数] <命令> <project_id> [命令参数]
```

全局参数：

- `--projects-root <path>`：工程根目录，默认 `projects`。
- `--debug`：显示内部 JSON 和原始异常等技术信息。普通模式只显示自然中文视图。

注意：全局参数放在命令前，例如 `python3 main.py --debug inspect demo`。

流转命令 `new`、`resume`、`retry`、`rewind` 支持以下选项：

| 参数 | 含义 |
|---|---|
| `--model-config <path>` | 指定状态级模型路由，默认 `configs/model_config.yaml` |
| `--offline` | 显式离线测试；模拟图不能最终交付 |
| `--selected-id <id>` | 从五张候选图中选择主图编号 |
| `--manual-action <action>` | 逐轮人工动作：`execute`、`edit_and_execute`、`skip`、`end`、`accept_current` |
| `--edited-delta <text>` | `edit_and_execute` 时替换 VLM 修改建议 |
| `--human-prompt <text>` | 质检后追加自然语言修改要求 |
| `--edited-task-markdown <path>` | 导入人工编辑后的任务书 Markdown，形成结构化新版本 |
| `--approve-final` | 明确确认最终交付 |
| `--clarification-answers <path>` | 读取“字段到答案”的澄清答案 JSON |

#### `new`：创建工程

```bash
python3 main.py new campaign-001 \
  --task examples/sample_image_task_card.json
```

`--task` 是必填的 `ImageTaskCard` JSON。`new` 创建 `projects/campaign-001/` 并开始执行；若目录已有内容会拒绝覆盖，请改用 `resume`、`retry` 或 `rewind`。

离线演练：

```bash
python3 main.py new demo-offline \
  --task examples/sample_image_task_card.json \
  --offline
```

#### `resume`：从断点或等待点继续

```bash
python3 main.py resume campaign-001
```

`resume` 加载 manifest 指向的最后成功 Checkpoint。若当时停在人工等待点，会恢复同一状态；否则进入合法的下一状态。已完成的成功节点不会重跑。

常见继续方式：

```bash
# 提交澄清答案
python3 main.py resume campaign-001 \
  --clarification-answers answers.json

# 导入编辑后的任务书
python3 main.py resume campaign-001 \
  --edited-task-markdown revised-task.md

# 五选一
python3 main.py resume campaign-001 \
  --selected-id <候选编号>

# 执行本轮 VLM 返工建议
python3 main.py resume campaign-001 \
  --manual-action execute

# 编辑建议后执行
python3 main.py resume campaign-001 \
  --manual-action edit_and_execute \
  --edited-delta "保留主体比例，只降低背景亮度"

# 质检后进行自然语言修改
python3 main.py resume campaign-001 \
  --human-prompt "主体向上移动约 5%，增加留白"

# 最终确认
python3 main.py resume campaign-001 --approve-final
```

人工动作的精确定义：

- `execute`：执行本轮建议并生成新图；新图必须重新接受 VLM 检查。
- `edit_and_execute`：使用 `--edited-delta` 修改建议后执行。
- `skip`：跳过本轮建议，按当前策略保存决策。
- `end`：终止且不交付，不会伪造“质检完成”。
- `accept_current`：人工接受当前已检查图片，并写入结构化审计事实。

#### `retry`：重试已失败状态

```bash
python3 main.py retry campaign-001

# 使用另一套路由重试
python3 main.py retry campaign-001 \
  --model-config configs/model_config.yaml
```

只有 manifest 中存在 `failed_step` 且失败前存在成功 Checkpoint 时才能执行。系统会先从上一成功点创建 `retry-...` 新分支，再调用真实失败状态的处理器；旧分支保持不变。

#### `rewind` / `branch`：从历史创建分支

先查看历史：

```bash
python3 main.py history campaign-001
```

从 Checkpoint 创建命名分支：

```bash
python3 main.py rewind campaign-001 \
  --from checkpoints/main/000003-initial_candidate_generation.json \
  --name warmer-style
```

创建后立即以指定模型配置继续：

```bash
python3 main.py rewind campaign-001 \
  --from checkpoints/main/000003-initial_candidate_generation.json \
  --name model-experiment \
  --model-config configs/model_config.yaml \
  --continue
```

`branch` 是 `rewind` 的别名。源 Checkpoint 会经过版本与 checksum 校验；新分支复制可恢复状态到自己的第一个 Checkpoint，不覆盖源文件。

#### `history` 与 `inspect`

```bash
python3 main.py history campaign-001
python3 main.py --debug inspect campaign-001
```

`history` 输出用户可理解的时间线。`inspect` 默认隐藏内部信息；加 `--debug` 后输出 manifest JSON，适合排查当前分支、成功指针和失败步骤。

### 3. 任务书交互与编辑

系统生成的任务书使用自然中文 Markdown，包括“本次目标”“画面重点”“交付要求”“必须遵守”“暂定处理”“仍需你决定”等栏目。内部 Pydantic 字段不会成为普通界面的标题。

推荐流程：

1. 将终端显示的任务书保存到 Markdown 文件。
2. 直接改写目标、限制条件或“暂定处理”条目；不要添加内部 JSON Key。
3. 用 `--edited-task-markdown` 提交。
4. 系统解析为新的结构化任务书版本并重新执行门禁。

```bash
python3 main.py resume campaign-001 \
  --edited-task-markdown ./revised-task.md
```

阻塞项未解决时不会进入付费生图阶段。机器状态保存在 Checkpoint 中，并不依赖从用户可见标题反向猜测全部契约。

### 4. 自检终止与放行策略

`configs/runtime.yaml` 中的默认配置：

```yaml
self_check:
  termination: fix
  fixed_rounds: 2
  max_rounds: 4
  stop_early_on_pass: false
  release: manual
```

两个维度相互独立：

| 维度 | 值 | 行为 |
|---|---|---|
| `termination` | `fix` | 按 `fixed_rounds` 次“VLM 实际检查”控制固定轮次；可由 `stop_early_on_pass` 允许提前通过 |
| `termination` | `solo` | VLM 返回通过时结束，最多检查 `max_rounds` 次 |
| `release` | `manual` | 每轮建议先等待人工执行、编辑、跳过、终止或接受当前图 |
| `release` | `auto` | 自动执行允许的 I2I 返工并进入下一轮检查 |

四种组合为 `fix/manual`、`fix/auto`、`solo/manual`、`solo/auto`。无论哪种组合，可交付资产都必须与 `latest_checked_asset_hash` 指向的最近实际检查资产一致。最后一次允许检查仍建议继续时，系统不会再生成一张无法复检的新图，也不会设置伪完成；它会保留已检查资产并进入人工决定等待态。

其他关键运行项：

- `clarification_total_budget: 10`：跨轮澄清总预算。
- `max_auto_questions: 3`：单轮自动问题上限。
- `max_clarify_rounds: 3`：最多澄清轮次。
- `max_render_retries: 2`、`max_calibration_retries: 3`：调用重试预算。
- `default_output_size: "1024x1024"`、`response_format: "url"`、`watermark: false`：图片请求默认值。

### 5. 常见故障与恢复

- **缺少凭证**：设置路由对应的环境变量，再运行 `resume` 或 `retry`。
- **工程正在处理**：同一工程由 `.lock` 保证进程互斥；等待另一个进程完成后重试。不要手工删除正在使用的锁。
- **模型调用失败**：查看 `history`；确认 manifest 已记录失败后使用 `retry`。
- **想换方案而非重试失败**：使用 `rewind` 从满意的历史节点创建新分支。
- **Checkpoint 完整性错误**：不要修改历史快照；回到更早的有效 Checkpoint 创建分支。
- **离线结果无法交付**：用真实模型配置和凭证从生成前的 Checkpoint `rewind`，再推进新分支。

## 二、系统架构与开发者指南

### 1. 工作区与持久化

`ProjectStore(projects_root, project_id)` 拥有单个工程的所有恢复和审计资源。当前实际布局如下：

```text
projects/<project_id>/
├── manifest.json
├── project.yaml
├── branches.json
├── .lock                         # 运行事务期间存在
├── events/
│   └── events.jsonl              # append-only 审计事件
├── runtime/
│   └── prompts.jsonl             # append-only Prompt 调用/结果链
├── checkpoints/
│   └── <branch>/
│       └── <seq>-<state>.json    # 原子、不可覆盖的恢复快照
└── artifacts/
    ├── images/                   # 以内容 hash 命名的稳定图片副本
    └── metadata.jsonl            # append-only 资产元数据
```

`manifest.json` 只指向当前分支、最后成功 Checkpoint 和可选失败步骤；JSONL 负责审计，不承担完整恢复。`branches.json` 保存父分支和来源 Checkpoint。

#### 原子写与完整性

`atomic_json()` 将 canonical JSON 写入同目录临时文件，依次 `flush`、`fsync`，最后用 `os.replace` 原子替换目标。Checkpoint 写入后带 `format_version` 与基于 canonical JSON 的 SHA-256 `checksum`；读取时两者都必须通过校验。成功快照不可覆盖，失败不会移动最后成功指针。

事件和 Prompt JSONL 每次追加后也会 `flush` 和 `fsync`。Prompt 调用前写 `started` 记录，结束后追加与父记录 hash 相连的 `completed` 或 `failed` 记录，因此原始输出、解析结果、模型参数和错误均可追踪。

#### 幂等键

`ProjectStore.idempotency_key()` 对以下值做稳定 hash：

```text
state + checkpoint_hash + prompt_hash + model_hash + reference_hash
```

相同状态、输入、Prompt、模型配置和参考图得到相同键，可在恢复/重试中识别已成功的付费调用；显式重做则通过新分支保留两条历史。

#### 进程锁

`.lock` 以 `O_CREAT | O_EXCL` 创建，同一进程内的同一 `ProjectStore` 实例支持可重入。CLI 与 `WorkflowRunner` 在读取指针、执行 handler、写 Checkpoint、推进 manifest 的完整事务外层持锁；另一个进程会收到自然中文的忙碌提示。

### 2. 显式状态机与门禁

生产迁移定义在 `agent_core/workflow.py` 的 `TRANSITIONS`：

| 当前状态 | 合法目标状态 | 关键前置条件 |
|---|---|---|
| `intake_clarify` | `confirmation_build` | 必要澄清完成且预算状态已保存 |
| `confirmation_build` | `initial_candidate_generation` | 任务书已确认，阻塞项已解决 |
| `initial_candidate_generation` | `master_candidate_selection` | 五个差异化候选资产已完整持久化 |
| `master_candidate_selection` | `self_check_iteration` | 已直选一张当前主图 |
| `self_check_iteration` | 自身或 `human_prompt_iteration` | 逐轮质检/返工；完成事实与最新检查 hash 一致 |
| `human_prompt_iteration` | 自身、`self_check_iteration` 或 `final_approval` | 新图会使旧质检失效并返回复检；无修改时才进入最终门禁 |
| `final_approval` | 无 | 人工批准、非 mock、终止策略满足、最终资产等于最近已检查资产 |

```text
intake_clarify
  → confirmation_build
  → initial_candidate_generation
  → master_candidate_selection
  → self_check_iteration ↺
  → human_prompt_iteration ↺ ──┐
           └→ self_check_iteration
           └→ final_approval
```

`validate_transition()` 拒绝任何表外跳转；生产 `WorkflowRunner` 在 handler 执行前调用迁移校验。等待点通过 snapshot 的 `phase` 恢复同一状态。最终门禁不会信任调用者硬编码的完成布尔值，而是核验持久化质检状态、策略、终止原因和资产 hash。

### 3. Presenter 视图隔离

领域层使用稳定的 Pydantic V2 英文契约，`interaction/presenter.py` 将其渲染为终端用户可理解的中文：

```text
领域模型 / Checkpoint ──→ Presenter ──→ 中文问题、候选图、质检结论、历史
                              │
                              └── debug=true → 技术 JSON
```

字段标签集中在 `LABELS` / `label_for()`，未知字段使用安全的“相关要求”标签，避免任意内部字段名进入问题。`confirmation_markdown()` 生成自然任务书；CLI 的 `_present_result()` 只调用 Presenter 或输出已生成的 Markdown。扩展 UI 时应复用 view-model 层，不要在领域模型或 handler 中散落打印逻辑。

### 4. 状态级模型路由

`ModelRouter.from_file()` 校验 `ModelConfig`，`validate_required_bindings()` 确保节点角色正确。`reload_at_boundary()` 仅在工作流状态或迭代边界重新读取配置；`RuntimeModelGateway` 同时把配置 hash 和实际 binding 写入事件。

新增 provider 时应：

1. 在 `model_router/router.py` 的 `PROVIDER_KEY_ENV` 声明凭证环境变量。
2. 在 `model_router/clients.py` 实现相应文本/VLM 客户端适配。
3. 保持 `ModelExecutor.audited_run()` 包装，不能绕过 Prompt 审计、超时、重试和错误分类。
4. 为角色不匹配、缺少凭证、超时和 provider 拒绝添加测试。

### 5. 多模态 I2I 与上下文预算

`ContextAssembler` 接收目标、规格、约束、当前输入、反馈、可选上下文和 `ReferenceImage` 列表：

1. 参考图按 `order` 稳定排序。
2. 超过 `max_images` 时截断。
3. 模型不支持多图且未允许单图降级时抛出 `CapabilityMismatchError`。
4. 文本超过 `max_text_chars` 时优先保留 required 内容，再裁剪 optional 内容。

因此每轮基于“当前任务 + 当前资产 + 当前反馈”装配最小充分上下文，不把历轮完整 Prompt 反复嵌套，避免线性累积演化为指数膨胀。

图片载荷由 `render_clients/payload_mapper.py` 统一生成：

```python
payload = build_render_payload(
    model="doubao-seedream-5-0-pro-260628",
    prompt="保留主体结构，降低背景亮度",
    size="1024x1024",
    metadata={"trace_id": "trace_example"},
    response_format="url",
    watermark=True,
    reference_images=["https://example.com/current.png"],
)
```

单图映射为：

```json
{"extra_body": {"image": "https://example.com/current.png", "watermark": true}}
```

多图则保持顺序映射为 `extra_body.image` 数组。生成结果必须经过资产归一化，形成稳定 URI、内容/引用 hash、provider、model 与 mock 标记；临时远程 URL 不能作为唯一可恢复资产。

### 6. 扩展工作流的约束

新增状态时至少同步修改并验证：

1. `agent_core/workflow.py` 的 `TRANSITIONS`。
2. `WorkflowRunner` 的顺序、handler 和等待/恢复语义。
3. `configs/model_config.yaml` 中需要模型的 state binding 与角色。
4. `workflows/image_mvp_v2_state_machine.yaml` 的声明状态和迁移。
5. Presenter 的自然中文进度标签。
6. Checkpoint 的完整恢复数据和 `format_version` 兼容策略。
7. 合法路径、非法迁移、失败重试、分支恢复和最终门禁测试。

不要用位置顺序替代显式迁移，不要让旧版“三选”和二级三图生成阶段重新进入活跃工作流。

### 7. 测试与贡献检查

安装锁定依赖后运行：

```bash
python3 main.py --help
python3 -m pytest --collect-only -q
python3 -m pytest -q
```

测试集中在：

- `tests/test_refactor.py`：强类型模型、原子 Checkpoint、Prompt 脱敏、路由、I2I 载荷、上下文预算、澄清去重等组件行为。
- `tests/test_workflow_e2e.py`：真实 Runner 的等待/恢复、retry、四种质检组合、资产 hash 门禁、非法迁移和双进程互斥。

测试通过 Fake Clients / 离线客户端注入可预测输出，不调用付费服务。新增模型集成时，单元和端到端测试仍应默认零成本、确定性运行；真实供应商冒烟测试应单独启用，且不得把 API Key 写入 fixture 或日志。

提交前检查：

- CLI 示例与 `python3 main.py --help` 一致。
- 用户界面没有泄漏内部英文 Key。
- 所有模型调用都在 Prompt 审计和重试包装内。
- 新资产会使旧质检事实失效并触发复检。
- 恢复、重试和 rewind 不覆盖旧 Checkpoint、Prompt 或资产。
- 全量测试通过，而非只运行单个组件测试。
