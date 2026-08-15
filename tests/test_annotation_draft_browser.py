"""Real-DOM regression for Q1-A annotation draft navigation persistence."""
from __future__ import annotations

import io
import socket
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

import main_front
from agent_core.workflow_runner import WorkflowRunner
from configs.runtime_policy import RuntimePolicy
from storage.project_store import ProjectStore


def _png(color: str, size: tuple[int, int] = (320, 200)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, "PNG")
    return stream.getvalue()


@pytest.mark.browser
def test_annotation_draft_round_trips_through_status_dom_and_clears_after_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selenium = pytest.importorskip("selenium")
    uvicorn = pytest.importorskip("uvicorn")
    from selenium.webdriver import Firefox
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.support.ui import WebDriverWait

    project_id = "annotation-draft-dom"
    root = tmp_path / "projects"
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", root)
    store = ProjectStore(root, project_id)
    store.create(RuntimePolicy(offline_mode=True).snapshot())
    asset = store.artifacts.save_bytes(_png("white"), metadata={"kind": "draft_source"})
    old_checkpoint = store.checkpoint("human_prompt_iteration", {
        "state": "human_prompt_iteration",
        "domain_state": "quality_rework",
        "phase": "waiting_human_tune",
        "waiting": True,
        "human_tune_mode": True,
        "calibration_status": "waiting_human_tune",
        "termination_satisfied": False,
        "termination_reason": "human_tune_in_progress",
        "asset": asset,
        "current_asset": asset,
    })

    def image_child(runner: WorkflowRunner, *_args, **_kwargs) -> dict:
        return runner.store.artifacts.save_bytes(_png("blue"), metadata={"kind": "draft_child"})

    monkeypatch.setattr(WorkflowRunner, "_image_call", image_child)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main_front.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started

    options = Options()
    options.add_argument("-headless")
    try:
        driver = Firefox(options=options)
    except Exception as exc:  # pragma: no cover - depends on host Firefox runtime
        server.should_exit = True
        thread.join(timeout=10)
        pytest.skip(f"当前环境无法启动 Firefox WebDriver：{type(exc).__name__}")
    wait = WebDriverWait(driver, 30)

    def visible_button(text: str):
        return wait.until(lambda d: next((
            button for button in d.find_elements(By.TAG_NAME, "button")
            if button.text.strip() == text and button.is_displayed() and button.is_enabled()
        ), None))

    def pointer_path(canvas, points: list[tuple[float, float]], pointer_id: int) -> None:
        driver.execute_script(
            """
            const canvas = arguments[0], points = arguments[1], pointerId = arguments[2];
            Object.defineProperty(canvas, 'setPointerCapture', {value: () => {}, configurable: true});
            const rect = canvas.getBoundingClientRect();
            const emit = (type, point) => canvas.dispatchEvent(new PointerEvent(type, {
              bubbles: true, pointerId, pointerType: 'mouse',
              buttons: type === 'pointerup' ? 0 : 1,
              clientX: rect.left + point[0] * rect.width,
              clientY: rect.top + point[1] * rect.height,
            }));
            emit('pointerdown', points[0]);
            points.slice(1).forEach((point) => emit('pointermove', point));
            emit('pointerup', points[points.length - 1]);
            """,
            canvas, points, pointer_id,
        )

    try:
        driver.get(f"http://127.0.0.1:{port}/")
        project = wait.until(lambda d: next((
            item for item in d.find_elements(By.CSS_SELECTOR, ".project-item")
            if project_id in item.text
        ), None))
        driver.execute_script("arguments[0].click()", project)
        canvas = wait.until(lambda d: (
            (candidate := d.find_element(By.CSS_SELECTOR, "canvas[aria-label^='圈画标注画布']"))
            if d.execute_script("return arguments[0].width > 10 && arguments[0].height > 10", candidate)
            else False
        ))

        # 真实 PointerEvent 写入一个矩形，再切换工具写入一条自由笔迹。
        pointer_path(canvas, [(.12, .18), (.48, .58)], 1)
        color = driver.find_element(By.CSS_SELECTOR, "input[aria-label='标注颜色']")
        width = driver.find_element(By.CSS_SELECTOR, "input[aria-label='笔触粗细']")
        driver.execute_script(
            "arguments[0].value='#00ff00';arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
            "arguments[1].value='17';arguments[1].dispatchEvent(new Event('input',{bubbles:true}));",
            color, width,
        )
        driver.execute_script("arguments[0].click()", visible_button("自由画笔"))
        pointer_path(canvas, [(.2, .25), (.35, .42), (.6, .7)], 2)
        prompt = driver.find_element(By.ID, "annotate-prompt")
        explanation = "保留主体比例，只调暖圈画区域"
        driver.execute_script(
            "arguments[0].value=arguments[1];arguments[0].dispatchEvent(new Event('input',{bubbles:true}))",
            prompt, explanation,
        )

        draft_key = driver.execute_script(
            "return Array.from({length:sessionStorage.length},(_,i)=>sessionStorage.key(i))"
            ".find(key=>key.startsWith('studio-annotation:'))"
        )
        assert draft_key
        before = driver.execute_script("return JSON.parse(sessionStorage.getItem(arguments[0]))", draft_key)
        assert [mark["kind"] for mark in before["marks"]] == ["rectangle", "stroke"]

        # 工作区 -> 状态 -> 工作区：触发真实 capture、DOM 销毁、重建和 restore。
        driver.execute_script(
            "arguments[0].click()",
            driver.find_element(By.CSS_SELECTOR, ".topnav__tab[data-view='status']"),
        )
        wait.until(lambda d: d.find_elements(By.ID, "actor-input"))
        driver.execute_script(
            "arguments[0].click()",
            driver.find_element(By.CSS_SELECTOR, ".topnav__tab[data-view='workspace']"),
        )
        restored_canvas = wait.until(lambda d: (
            (candidate := d.find_element(By.CSS_SELECTOR, "canvas[aria-label^='圈画标注画布']"))
            if d.execute_script("return arguments[0].width > 10 && arguments[0].height > 10", candidate)
            else False
        ))
        wait.until(lambda d: d.execute_script(
            "const x=arguments[0].getContext('2d').getImageData(0,0,arguments[0].width,arguments[0].height).data;"
            "for(let i=3;i<x.length;i+=4){if(x[i])return true}return false",
            restored_canvas,
        ))
        assert driver.find_element(By.ID, "annotate-prompt").get_attribute("value") == explanation
        assert driver.find_element(By.CSS_SELECTOR, "button[aria-pressed='true']").text == "自由画笔"
        assert driver.find_element(By.CSS_SELECTOR, "input[aria-label='标注颜色']").get_attribute("value") == "#00ff00"
        assert driver.find_element(By.CSS_SELECTOR, "input[aria-label='笔触粗细']").get_attribute("value") == "17"
        assert visible_button("撤销").is_enabled()
        after = driver.execute_script("return JSON.parse(sessionStorage.getItem(arguments[0]))", draft_key)
        assert after == before

        # 使用真实后端提交；新 checkpoint 接管后旧 asset 草稿必须被删除。
        driver.execute_script("arguments[0].click()", visible_button("预览并提交微调"))
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "dialog[open]"))
        driver.execute_script("arguments[0].click()", visible_button("确认提交"))
        wait.until(lambda d: d.execute_script("return sessionStorage.getItem(arguments[0]) === null", draft_key))
        wait.until(lambda _d: ProjectStore(root, project_id).manifest()["current_checkpoint"]["checkpoint_id"] != old_checkpoint)
        assert ProjectStore(root, project_id).resume()["asset"]["artifact_id"] != asset["artifact_id"]
    finally:
        driver.quit()
        server.should_exit = True
        thread.join(timeout=10)
