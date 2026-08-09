# Agent 风格库运行时结构

`agent-library/` 是唯一运行时入口；既有 DOCX、大型聚合 JSON 和历代资料放在 `legacy/`，生产代码不扫描它们。

```text
agent-library/
├── library.json
├── index.jsonl
├── styles/CW-HA-001/style.json
├── styles/CW-HA-001/image.jpg
├── extractions/CW-HA-001/<extraction_key>.json
├── schemas/{library,style,extraction}.schema.json
└── vocab/controlled-tags.json       # 可选
```

每张图片对应稳定 `style_id`。`style.json` 最小输入为 `style_id/image/title/describe`；导入工具补齐 MIME、尺寸和 SHA-256。`source/tags/task_fit` 可选。运行时只读 `library.json`、`index.jsonl` 和当前 extraction。

```json
{"style_id":"CW-HA-001","image":"image.jpg","title":"历史剧场","describe":"时间轴与空间装置结合","tags":["文化墙"],"task_fit":["品牌历史"]}
```

构建并校验：`python -m skills.style_library_cli art-lib/agent-library --build`。风格原图只进入 VLM 提取边界；生图网关只接收带 `style_id/extraction_key` 的结构化文字补充。
