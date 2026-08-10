"""Compatibility adapter; all project metadata is sourced from pyproject.toml."""
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10 build environments
    import tomli as tomllib

from setuptools import find_packages, setup


PROJECT = tomllib.loads(Path(__file__).with_name("pyproject.toml").read_text(encoding="utf-8"))["project"]

setup(
    name=PROJECT["name"],
    version=PROJECT["version"],
    description=PROJECT["description"],
    python_requires=PROJECT["requires-python"],
    install_requires=PROJECT["dependencies"],
    extras_require=PROJECT["optional-dependencies"],
    packages=find_packages(include=[
        "agent_core*", "interaction*", "storage*", "prompt_engine*", "skills*",
        "model_router*", "render_clients*", "calibrator*", "review*", "configs*",
        "schemas*", "examples*", "frontend*",
    ]),
    py_modules=["workspace_cli", "main", "main_front", "diagnostics"],
    include_package_data=True,
    package_data={
        "": ["*.yaml", "*.json", "*.md", "*.html"],
        "frontend": ["static/css/*.css", "static/js/*.js"],
    },
    entry_points={"console_scripts": ["image-agent=workspace_cli:main"]},
)
