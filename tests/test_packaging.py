from __future__ import annotations

import json
from pathlib import Path
import tomllib

import config
import server
import traceweave_mcp


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_NAME = "io.github.gokeshenzhen/traceweave"


def test_distribution_registry_and_runtime_versions_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    registry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert project["name"] == "traceweave-mcp"
    assert project["version"] == traceweave_mcp.__version__ == registry["version"]
    assert registry["packages"][0]["identifier"] == project["name"]
    assert registry["packages"][0]["version"] == project["version"]
    assert registry["name"] == REGISTRY_NAME


def test_distribution_installs_only_the_traceweave_namespace():
    setuptools = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["setuptools"]

    assert "py-modules" not in setuptools
    assert setuptools["packages"] == [
        "traceweave_mcp",
        "traceweave_mcp._runtime",
        "traceweave_mcp._runtime.src",
    ]
    assert set(setuptools["package-dir"]) == {
        "traceweave_mcp",
        "traceweave_mcp._runtime",
        "traceweave_mcp._runtime.src",
    }


def test_pypi_description_contains_registry_ownership_marker():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    readme_path = ROOT / project["readme"]["file"]

    assert f"<!-- mcp-name: {REGISTRY_NAME} -->" in readme_path.read_text(
        encoding="utf-8"
    )


def test_mcp_runtime_reports_package_version():
    options = server.app.create_initialization_options()

    assert options.server_name == "traceweave"
    assert options.server_version == traceweave_mcp.__version__


def test_packaged_custom_patterns_default_is_present():
    packaged = Path(traceweave_mcp.__file__).with_name("custom_patterns.yaml")

    assert packaged.is_file()
    assert config.CUSTOM_PATTERNS_FILE == str(ROOT / "custom_patterns.yaml")


def test_packaged_runtime_does_not_depend_on_repository_scripts():
    production_sources = [ROOT / "server.py", *sorted((ROOT / "src").glob("*.py"))]

    for source in production_sources:
        text = source.read_text(encoding="utf-8")
        assert "from scripts" not in text
        assert "import scripts" not in text
