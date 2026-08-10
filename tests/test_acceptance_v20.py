import time

from agent_core.jobs import JobRegistry
from main_front import ProjectNotFoundError, _translate_error


def test_job_registry_recovers_when_persistence_directory_was_replaced(tmp_path):
    """A live server must still accept five-choice work after projects restore."""
    jobs_root = tmp_path / "projects" / ".jobs"
    registry = JobRegistry(jobs_root)
    jobs_root.rmdir()

    job, created = registry.submit("test", "select-v20", "advance", lambda: {"project_id": "test"})

    assert created is True
    for _ in range(100):
        record = registry.get(job["job_id"])
        if record["status"] == "succeeded":
            break
        time.sleep(0.01)
    assert record["status"] == "succeeded"
    assert record["result"] == {"project_id": "test"}


def test_missing_internal_file_is_not_mislabeled_as_missing_project():
    response = _translate_error(FileNotFoundError("资产索引不存在。"))
    assert response.status_code == 409
    assert response.detail == {"code": "PROJECT_FILE_MISSING", "message": "资产索引不存在。"}


def test_actual_missing_project_keeps_public_error_contract():
    response = _translate_error(ProjectNotFoundError("工程不存在：test"))
    assert response.status_code == 404
    assert response.detail == "PROJECT_NOT_FOUND: 工程不存在。"
