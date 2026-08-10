import threading

from agent_core.jobs import JobRegistry


def test_second_project_job_reuses_active_record_instead_of_failing_lock(tmp_path):
    registry = JobRegistry(tmp_path / "jobs", workers=2)
    release = threading.Event()
    first, created = registry.submit("test", "first-v21", "执行质检建议", lambda: release.wait(2))
    second, second_created = registry.submit("test", "second-v21", "执行质检建议", lambda: None)
    try:
        assert created is True
        assert second_created is False
        assert second["job_id"] == first["job_id"]
        assert registry.active_for_project("test")["job_id"] == first["job_id"]
    finally:
        release.set()


def test_another_project_can_still_run_in_parallel(tmp_path):
    registry = JobRegistry(tmp_path / "jobs", workers=2)
    release = threading.Event()
    first, _ = registry.submit("one", "one-v21", "推进工作流", lambda: release.wait(2))
    second, created = registry.submit("two", "two-v21", "推进工作流", lambda: None)
    try:
        assert created is True
        assert second["job_id"] != first["job_id"]
    finally:
        release.set()
