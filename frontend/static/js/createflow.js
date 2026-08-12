/* T10 新建工程交互控制器：表单校验通过后，同一点击调用栈内先切换到工作台
 * 等待态，再在后台等待创建接口。网络与 DOM 由 app.js 注入，本模块可在 Node
 * 中精确回归“接口未返回时弹窗已关闭/工作台已跳转”以及导航后的迟到响应。 */

export function createImmediateProjectFlow(deps, registry) {
  const {
    createProject,
    showPending = () => undefined,
    showCreated = () => {},
    showError = () => {},
  } = deps;

  return {
    async start(payload) {
      const op = registry.begin();
      // 必须发生在第一次 await 之前：点击后立即关闭弹窗并给出工作台等待反馈。
      const pending = showPending(payload);
      try {
        const view = await createProject(payload, { signal: op.controller.signal });
        if (!registry.isCurrent(op)) return null;
        showCreated(view, pending);
        return view;
      } catch (error) {
        // 用户已导航离开时，创建结果未知；迟到错误不得覆盖新视图或重开弹窗。
        if (!registry.isCurrent(op)) return null;
        showError(error, pending);
        return null;
      }
    },
  };
}
