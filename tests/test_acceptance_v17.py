"""v1.7.7 release gate: immutable mode and a real offline browser journey."""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main_front
import agent_core.workflow_runner as workflow_runner


TASK = {
    "task_id": "task-v17-browser",
    "project_id": "v17-browser",
    "source_refs": [{"ref_id": "brief-v17", "ref_type": "brief", "excerpt": "广告海报", "source_hash": None}],
    "deliverable_goal": "广告 海报",
    "usage_context": "内部审核",
    "category_ref": {"category_id": "generic", "version": "1"},
    "known_facts": {"audience": "审核人员"},
    "unknowns": {"output_spec": "待确认"},
    "asset_inputs": [],
    "status": "draft",
}


def test_existing_project_mode_comes_only_from_immutable_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    client = TestClient(main_front.app, raise_server_exceptions=False)
    created = client.post("/api/projects", json={"project_id": "v17-contract", "task_card": TASK, "offline": True})
    assert created.status_code == 201

    # Existing-project commands no longer accept a caller-selected mode.
    rejected = client.post("/api/projects/v17-contract/advance", json={"offline": False})
    assert rejected.status_code == 422
    advanced = client.post("/api/projects/v17-contract/advance", json={"clarification_answers": {"output_spec": "1:1"}})
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["runtime_policy"]["offline_mode"] is True


@pytest.mark.browser
def test_offline_checkbox_to_final_acceptance_uses_zero_real_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selenium = pytest.importorskip("selenium")
    uvicorn = pytest.importorskip("uvicorn")
    from selenium.webdriver import Firefox
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.common.exceptions import StaleElementReferenceException
    from selenium.webdriver.support.ui import WebDriverWait

    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    provider_calls: list[str] = []

    def forbidden(name: str):
        def call(*_args, **_kwargs):
            provider_calls.append(name)
            raise AssertionError(f"offline browser flow called real provider: {name}")
        return call

    monkeypatch.setattr(workflow_runner, "build_text_client", forbidden("text"))
    monkeypatch.setattr(workflow_runner, "build_vlm_client", forbidden("vlm"))
    monkeypatch.setattr(workflow_runner.ArkImageRenderClient, "render", forbidden("image"))

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
    driver = Firefox(options=options)
    wait = WebDriverWait(driver, 30, ignored_exceptions=(StaleElementReferenceException,))

    def button(text: str):
        found = wait.until(lambda d: next((b for b in d.find_elements(By.TAG_NAME, "button") if b.text.strip() == text and b.is_displayed() and b.is_enabled()), None))
        driver.execute_script("arguments[0].scrollIntoView({block:'center'})", found)
        return found

    def click_button(text: str) -> None:
        driver.execute_script("arguments[0].click()", button(text))

    try:
        driver.get(f"http://127.0.0.1:{port}/")
        click_button("新建工程")
        dialog_text = driver.find_element(By.ID, "project-dialog").text
        assert all(key not in dialog_text for key in ("audience", "output_spec", "task_id", "project_id", "source_refs"))
        assert not driver.find_elements(By.ID, "task-json")
        driver.find_element(By.ID, "project-id").send_keys("v17-browser")
        driver.find_element(By.ID, "creative-goal").send_keys("广告 海报")
        driver.find_element(By.ID, "usage-scene").send_keys("内部审核")
        driver.find_element(By.ID, "target-group").send_keys("审核人员")
        checkbox = driver.find_element(By.ID, "offline")
        if not checkbox.is_selected():
            checkbox.click()
        click_button("创建并启动")
        wait.until(lambda d: not d.find_element(By.ID, "project-dialog").get_attribute("open"))

        wait.until(lambda d: d.find_elements(By.ID, "answer-form"))
        for group in driver.find_elements(By.CSS_SELECTOR, "#answer-form .option-cards"):
            group.find_element(By.CSS_SELECTOR, "label").click()
        click_button("提交答案并继续")

        actor = wait.until(lambda d: d.find_element(By.ID, "actor-input"))
        driver.execute_script("arguments[0].value='browser-reviewer'; arguments[0].dispatchEvent(new Event('change', {bubbles:true}))", actor)
        click_button("编辑任务书")
        editor = driver.find_element(By.ID, "taskbook-editor")
        driver.execute_script("arguments[0].value += '\\n- 浏览器验收修订：v17'", editor)
        click_button("保存修改")
        click_button("确认任务书，开始生成候选图")

        click_button("选为主图")
        confirm = wait.until(lambda d: next((b for b in d.find_elements(By.TAG_NAME, "button") if b.text.startswith("确认方向") and b.is_enabled()), None))
        driver.execute_script("arguments[0].click()", confirm)
        click_button("接受当前图")
        click_button("确认最终交付")
        wait.until(lambda d: d.switch_to.alert).accept()
        wait.until(lambda d: "离线演练已完成最终验收" in d.find_element(By.ID, "content").text)

        view = TestClient(main_front.app).get("/api/projects/v17-browser").json()
        assert view["snapshot"]["completed"] is True
        assert view["snapshot"]["offline_rehearsal_completed"] is True
        assert "final_asset" not in view["snapshot"]
        assert view["runtime_policy"]["offline_mode"] is True
        assert provider_calls == []
        assert driver.find_element(By.ID, "health-text").text == "服务已就绪"
    finally:
        driver.quit()
        server.should_exit = True
        thread.join(timeout=10)
