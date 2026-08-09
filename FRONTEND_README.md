# Image Agent Studio 前端启动与验收

## 新增范围

本交付仅新增 `main_front.py`、`frontend/`、`frontend_tests/`、`design-system/`、
`requirements-front.txt` 与本文档。生产后端的 132 个原文件均保持不变。

`main_front.py` 是薄适配层：工程创建、恢复、重试和分支分别调用生产代码中的
`WorkflowRunner` 与 `ProjectStore`。它不复制状态机、模型路由、生成、校准或最终
交付门禁。浏览器侧不保存密钥，也不会把离线模拟结果标为最终成功。

## 启动

```bash
cd image_agent_mvp
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.lock -r requirements-front.txt
uvicorn main_front:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000`。生产模型凭证继续使用原后端支持的环境变量，
例如 `ARK_API_KEY`；本文档和前端代码不写入任何密钥。

可选环境变量：

- `IMAGE_AGENT_FRONT_PROJECTS_ROOT`：Web 工程数据根目录。默认写入
  `frontend/data/projects`，与附件内原生产工程隔离。
- `IMAGE_AGENT_MODEL_CONFIG`：生产模型配置路径。默认使用
  `configs/model_config.yaml`。

## 测试

```bash
# 原生产测试
python3 -m pytest -q tests

# 新增适配层测试
python3 -m pytest -q frontend_tests

# Python 语法检查
python3 -m py_compile main_front.py
```

## 安全边界

- 工程 ID 仅允许 2–64 位字母、数字、下划线与连字符，阻止路径穿越。
- JSON 请求上限 512 KiB；本地图片下载上限 25 MiB。
- 图片只允许从当前工程的 `artifacts/images` 目录读取，且 MIME 类型仅允许
  PNG、JPEG、WebP、GIF。
- 外部模型、密钥、配置或依赖不可用时，API 返回可恢复的真实错误；工作流已有
  检查点不会被覆盖。
- 同一工程并发推进沿用生产 `ProjectStore.lock()` 排他锁。

## 设计与状态覆盖

设计源文件位于 `design-system/image-agent-studio/MASTER.md`，工作台差异规则位于
`pages/workspace.md`。界面覆盖 loading、empty、error、disabled、success、离线提示、
长任务超时和断线恢复；支持 375、768、1024、1440px，键盘焦点、跳转链接、
语义表单、`aria-live`、44px 触控目标与 `prefers-reduced-motion`。

## 生产基线证据

- 指定附件：`image_agent_mvp_production.zip`
- 附件 ID：`019fd20a-3869-7cd2-aef2-b97f691526fe`
- ZIP SHA-256：`c9cb2760040da794789621241932424a2ed48bf14fe27b9d9a1b5d55b73509d3`
- 原文件数：132
- 交付前复核方式：对解压基线和当前树排除上述新增路径后分别执行
  `find ... -type f -print0 | sort -z | xargs -0 sha256sum`，再执行 `diff -u`；
  预期无输出且退出码为 0。

## 已知限制

- 当前适配层按一次请求推进一个生产检查点；真实图片生成可能持续较久，页面会显示
  忙碌状态并在 120 秒后提示刷新检查点。后端仍可能继续完成，不会伪造取消。
- 外部 HTTP 图片由原供应商 URL 展示；本地生成图片需已进入生产 artifact store。
- 没有真实模型凭证时只能显式选择离线测试模式，模拟图片受生产最终门禁限制。
- 四档截图在当前执行容器中未能生成：系统 Firefox 无头模式报告
  `RenderCompositorSWGL failed mapping default framebuffer`；随后尝试临时安装
  Playwright Chromium，但浏览器包下载连续出现 `ECONNRESET`。因此本交付不虚报
  截图完成。页面已实现并静态核查 375/768/1024/1440px 断点，需在具备可用浏览器
  渲染环境的验收阶段补录四档视觉证据。
