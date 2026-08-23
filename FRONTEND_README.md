# Image Agent 受管前端

该前端是主系统启动的 Image Agent 实例工作区，不是独立配置控制台。

用户可在这里查看工程进度、回答澄清问题、确认任务书、选择主图、处理质检与下载交付。页面不展示或修改 Provider、凭据、模型路由、运行文件、测试模式或部署诊断。

受管模式下：

- 工程只能由主系统 Adapter 创建；
- 直接创建请求会被拒绝；
- 任务身份与实例身份必须一致；
- 受保护请求同时校验本机来源和 Adapter 密钥；
- 运行策略来自任务创建时固定的只读快照。

前端开发验证：

```bash
python3 -m pytest -q frontend_tests
node --test frontend_tests/js/*.test.mjs
```

测试通过显式注册的 fake clients 覆盖交互链路，不会调用真实供应商。
