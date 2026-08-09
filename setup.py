"""Compatibility metadata for build frontends shipping setuptools < 61."""
from setuptools import find_packages, setup

setup(
    name="image-agent-mvp",
    version="0.1.0",
    packages=find_packages(include=[
        "agent_core*", "interaction*", "storage*", "prompt_engine*", "skills*",
        "model_router*", "render_clients*", "calibrator*", "review*", "configs*",
        "schemas*", "examples*",
    ]),
    py_modules=["workspace_cli", "main", "main_front"],
    include_package_data=True,
    package_data={"": ["*.yaml", "*.json", "*.md"]},
    entry_points={"console_scripts": ["image-agent=workspace_cli:main"]},
    python_requires=">=3.10",
)
