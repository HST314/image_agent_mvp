"""User-facing CLI for the fully assembled recoverable workflow."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from agent_core.workflow_runner import RunnerOptions, WorkflowRunner
from calibrator.calibration_loop import ManualAction
from interaction.presenter import Presenter
from storage.project_store import ProjectStore
from agent_core.models import QuestionCard
from configs.runtime_policy import RuntimePolicy


def _flow_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--model-config", type=Path, default=Path(__file__).parent / "configs/model_config.yaml")
    command.add_argument("--offline", action="store_true", help="显式离线测试模式（模拟图不可最终交付）")
    command.add_argument("--selected-id", help="从五张候选图中选择的编号")
    command.add_argument("--manual-action", choices=("execute", "edit_and_execute", "skip", "end", "accept_current", "add_rounds", "human_tune_best"),
                         help="上限处置：accept_current/end/add_rounds/human_tune_best")
    command.add_argument("--edited-delta", help="选择编辑建议后执行时的新建议")
    command.add_argument("--additional-rounds", type=int, default=0, help="add_rounds 增加的质检轮数")
    command.add_argument("--confirm-cost", action="store_true", help="确认 add_rounds 产生的额外费用")
    command.add_argument("--human-prompt", help="质检后人工自然语言修改要求")
    command.add_argument("--edited-task-markdown", type=Path, help="编辑后的任务书；保存为结构化新版本")
    command.add_argument("--approve-final", action="store_true")
    command.add_argument("--clarification-answers", type=Path, help="澄清答案 JSON（字段到答案）")
    command.add_argument("--approve-task", action="store_true", help="确认当前任务书并允许进入付费步骤")
    command.add_argument("--actor", help="执行任务书确认的人员标识")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="图片创作工程：可恢复、可分支、状态级模型路由")
    root.add_argument("--projects-root", type=Path, default=Path("projects"))
    root.add_argument("--debug", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new", help="创建工程并启动真实工作流")
    new.add_argument("project_id"); new.add_argument("--task", type=Path, required=True, help="ImageTaskCard JSON")
    _flow_options(new)
    for name in ("resume", "retry"):
        item = commands.add_parser(name, help="从检查点继续" if name == "resume" else "在新分支重跑真实失败状态")
        item.add_argument("project_id"); _flow_options(item)
    for name in ("history", "inspect"):
        item = commands.add_parser(name); item.add_argument("project_id")
    rewind = commands.add_parser("rewind", aliases=["branch"])
    rewind.add_argument("project_id"); rewind.add_argument("--from", dest="checkpoint", required=True); rewind.add_argument("--name")
    rewind.add_argument("--continue", dest="continue_run", action="store_true"); _flow_options(rewind)
    policy = commands.add_parser("revise-policy", help="人工确认后在新分支修订运行策略")
    policy.add_argument("project_id"); policy.add_argument("--policy", type=Path, required=True)
    policy.add_argument("--actor", required=True); policy.add_argument("--confirm", action="store_true")
    unknown = commands.add_parser("unknown", help="查询或人工处置付费调用未知态")
    unknown.add_argument("project_id"); unknown.add_argument("--idempotency-key")
    unknown.add_argument("--action", choices=("retry_after_confirmation", "abandon")); unknown.add_argument("--actor")
    return root


def _options(args: argparse.Namespace) -> RunnerOptions:
    action = ManualAction(action=args.manual_action, edited_delta=args.edited_delta,
                          additional_rounds=getattr(args, "additional_rounds", 0),
                          cost_confirmed=bool(getattr(args, "confirm_cost", False))) if getattr(args, "manual_action", None) else None
    markdown = args.edited_task_markdown.read_text(encoding="utf-8") if getattr(args, "edited_task_markdown", None) else None
    answers = json.loads(args.clarification_answers.read_text(encoding="utf-8")) if getattr(args, "clarification_answers", None) else None
    return RunnerOptions(selected_id=getattr(args, "selected_id", None), manual_action=action,
                         human_prompt=getattr(args, "human_prompt", None), edited_markdown=markdown,
                         final_approved=bool(getattr(args, "approve_final", False)), clarification_answers=answers,
                         task_approved=bool(getattr(args, "approve_task", False)), actor=getattr(args, "actor", None))


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store = ProjectStore(args.projects_root, args.project_id)
    view = Presenter(args.debug)
    try:
        if args.command == "new":
            task = json.loads(args.task.read_text(encoding="utf-8"))
            policy = RuntimePolicy.from_file(Path(__file__).parent / "configs/runtime.yaml").model_copy(update={"offline_mode": args.offline})
            store.create(policy.snapshot())
            with store.lock():
                runner = WorkflowRunner(store, args.model_config, offline_mode=args.offline, output=print)
                result = runner.run({"task_card": task}, _options(args))
            _present_result(view, result)
            if result.get("waiting"): print("流程已安全暂停；补充所需选择后运行 resume。")
        elif args.command == "resume":
            with store.lock():
                snapshot = store.resume()
                if snapshot is None: raise ValueError("工程还没有可恢复节点。")
                result = WorkflowRunner(store, args.model_config, offline_mode=args.offline, output=print).run(snapshot, _options(args))
            _present_result(view, result)
            print("已从检查点的下一状态继续执行。" if not result.get("waiting") else "已恢复到人工等待点，未重复已完成的付费调用。")
        elif args.command == "retry":
            with store.lock():
                runner = WorkflowRunner(store, args.model_config, offline_mode=args.offline)
                result_box: list[dict[str, object]] = []
                store.retry(lambda state, snapshot: result_box.append(runner.run(snapshot, _options(args), only_state=state)))
            print("已从上一成功点创建新分支，并调用真实失败状态处理器。")
        elif args.command in {"rewind", "branch"}:
            with store.lock():
                branch = store.branch_from(args.checkpoint, name=args.name)
                print(f"已创建新分支 {branch}，原历史未修改。")
                if args.continue_run:
                    WorkflowRunner(store, args.model_config, offline_mode=args.offline).run(store.resume(), _options(args))
        elif args.command == "revise-policy":
            policy = RuntimePolicy.model_validate(json.loads(args.policy.read_text(encoding="utf-8")))
            print(json.dumps({"branch": store.revise_policy(policy.snapshot(), confirmed=args.confirm, actor=args.actor)}, ensure_ascii=False))
        elif args.command == "unknown":
            gateway = WorkflowRunner(store, Path(__file__).parent / "configs/model_config.yaml",
                                     offline_mode=RuntimePolicy.model_validate(json.loads((store.root / "runtime_policy.json").read_text())["policy"]).offline_mode).gateway
            if args.action:
                if not args.idempotency_key or not args.actor: raise ValueError("处置未知态必须提供 key 与 actor。")
                gateway.resolve_unknown(args.idempotency_key, args.action, args.actor)
            print(json.dumps({"items": gateway.unknown_actions()}, ensure_ascii=False))
        elif args.command == "history": print(view.history(store.history()))
        elif args.command == "inspect": print(view.technical(store.manifest()))
        return 0
    except Exception as exc:
        message = str(exc) if args.debug else "流程未完成；详细错误已写入工程事件。"
        print(f"{message}\n已有进度保存在工程目录，可修正后使用 resume 或 retry。")
        return 2

def _present_result(view: Presenter, result: dict[str, object]) -> None:
    if result.get("question_card"):
        print(view.questions(QuestionCard.model_validate(result["question_card"])))
    if result.get("task_markdown"):
        print(str(result["task_markdown"]))
    if result.get("candidates"):
        print(view.candidates(result["candidates"]))  # type: ignore[arg-type]
    if result.get("inspection"):
        from agent_core.models import VisualCheckResult
        print(view.inspection(VisualCheckResult.model_validate(result["inspection"])))


if __name__ == "__main__": raise SystemExit(main())
