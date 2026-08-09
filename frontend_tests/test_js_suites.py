"""前端 JS 模块的 Node 单元测试入口（T32/T33/T34/T35 纯逻辑核心）。

设计说明：markdown 清洗、圈画坐标几何、状态映射与画廊槽位均为纯函数，
在 Node 中直接断言；浏览器 E2E 在本容器的限制见 FRONTEND_README（无头
Firefox/Playwright 均不可用），故交互逻辑以纯函数测试 + Python 侧 HTTP
契约测试覆盖。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_TESTS = ROOT / "frontend_tests" / "js"


def test_frontend_js_suites() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("当前环境无 node；JS 纯逻辑套件需在含 node 的环境执行")
    # node --test 不接受目录参数（v22 会把目录当作模块解析），需显式展开 glob。
    test_files = sorted(str(p) for p in JS_TESTS.glob("*.test.mjs"))
    assert test_files, "frontend_tests/js 下应存在 *.test.mjs 套件"
    result = subprocess.run(
        [node, "--test", *test_files],
        capture_output=True, text=True, cwd=ROOT, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"node --test 失败：\n{result.stdout}\n{result.stderr}")
