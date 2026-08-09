"""Compatibility build entry point mirroring the canonical project metadata."""
from setuptools import find_packages, setup

setup(
    name="image-agent-mvp",
    version="0.1.0",
    description="Generic Phase 1 image agent core with offline human approval gates.",
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
    install_requires=["pydantic>=2.6,<3", "PyYAML>=6.0,<7", "openai>=1.0,<2",
                      "Pillow>=10.0,<13", "fastapi>=0.110,<1"],
    extras_require={"dev": ["pytest>=8.0,<9", "jsonschema>=4.0,<5"]},
)
