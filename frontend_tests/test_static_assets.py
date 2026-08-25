"""T35 模块化静态资源的发布契约测试：服务、路径安全与 wheel 打包。"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

import main_front

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "frontend" / "static" / "js"
CSS_DIR = ROOT / "frontend" / "static" / "css"

EXPECTED_JS = {
    "app.js", "api.js", "dom.js", "markdown.js", "store.js", "states.js",
    "home.js", "project.js", "taskbook.js", "clarify.js", "gallery.js",
    "annotate.js", "history.js", "jobrunner.js",
    "copy.js", "topnav.js", "viewswitch.js", "stepstatus.js",
    "createflow.js", "createform.js",
    "statuspage.js", "eventlog.js",
    "settingspage.js", "parentbridge.js",
    # T9 进度卡只读快照与历史分支
    "snapshots.js",
    # 分支查看/切换界面（顶栏分支徽章入口）
    "branches.js",
    # Q1-A 工作区纯 UI 状态恢复
    "workspace_state.js",
}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main_front, "PROJECTS_ROOT", tmp_path / "projects")
    return TestClient(main_front.app, raise_server_exceptions=False)


def test_all_modules_served_with_js_mime(client: TestClient) -> None:
    on_disk = {p.name for p in JS_DIR.glob("*.js")}
    assert on_disk == EXPECTED_JS  # 模块清单与磁盘一致，防止漏挂载
    for name in sorted(EXPECTED_JS):
        response = client.get(f"/static/js/{name}")
        assert response.status_code == 200, name
        assert "javascript" in response.headers["content-type"], name


def test_css_served(client: TestClient) -> None:
    response = client.get("/static/css/main.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_static_mount_rejects_traversal_and_missing(client: TestClient) -> None:
    assert client.get("/static/js/../../main_front.py").status_code in {400, 404}
    assert client.get("/static/js/nonexistent.js").status_code == 404
    assert client.get("/static/../main_front.py").status_code in {400, 404}


def test_index_shell_references_module_entry(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert 'type="module" src="/static/js/app.js"' in page.text
    assert 'data-view="settings">设置</button>' in page.text
    assert "data-unknown" in page.text


def test_t11_create_dialog_has_no_raw_contract_editor_or_english_keys(client: TestClient) -> None:
    """普通新建入口只展示中文表单；领域键可以存在于 JS 提交边界，但不得进入 DOM。"""
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="task-json"' not in page.text
    for raw_key in ("audience", "output_spec", "task_id", "project_id", "source_refs"):
        assert raw_key not in page.text
    for label in ("创作目标", "使用场景", "目标人群", "风格与语气", "交付规格"):
        assert label in page.text


def test_wheel_contains_static_frontend_modules(tmp_path: Path) -> None:
    """wheel 安装态必须包含全部前端模块，否则安装后工作台不可用（对齐 T03）。"""
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--wheel-dir", str(wheel_dir)],
        check=True, capture_output=True, text=True,
    )
    wheel = next(wheel_dir.glob("image_agent_mvp-*.whl"))
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "frontend/index.html" in names
    for name in EXPECTED_JS:
        assert f"frontend/static/js/{name}" in names, name
    assert "frontend/static/css/main.css" in names


def test_js_modules_parse_under_node() -> None:
    """所有 ES 模块必须可被解析（与浏览器 module 加载口径一致）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过语法门")
    for path in sorted(JS_DIR.glob("*.js")):
        subprocess.run([node, "--check", str(path)], check=True, capture_output=True, text=True)
