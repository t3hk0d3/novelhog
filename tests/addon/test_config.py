"""The add-on's definition and its release contract.

Home Assistant pulls the image tagged with the add-on's `version`, so a `config.yaml` that drifts
from the project version, or from the image CI publishes, makes the add-on uninstallable.
"""

import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
ADDON_DIR = ROOT / "homeassistant" / "novelhog"
REPOSITORY_URL = "https://github.com/t3hk0d3/novelhog"


def _yaml(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def _config() -> dict[str, Any]:
    return _yaml(ADDON_DIR / "config.yaml")


class TestConfigYaml__Parsed:
    def test_config__version_equals_the_project_version(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert _config()["version"] == project["project"]["version"]

    def test_config__image_is_the_published_addon_image(self) -> None:
        assert _config()["image"] == "ghcr.io/t3hk0d3/novelhog-addon"

    def test_config__arch_is_amd64_and_aarch64(self) -> None:
        assert sorted(_config()["arch"]) == ["aarch64", "amd64"]

    def test_config__exposes_port_8080(self) -> None:
        assert _config()["ports"] == {"8080/tcp": 8080}

    def test_config__web_ui_points_at_that_port(self) -> None:
        assert _config()["webui"] == "http://[HOST]:[PORT:8080]"

    def test_config__every_default_option_has_a_schema_entry(self) -> None:
        config = _config()
        assert set(config["options"]) <= set(config["schema"])

    def test_config__every_schema_entry_is_optional_or_has_a_default(self) -> None:
        # The Supervisor rejects options that miss a required entry. `?` marks an optional one (for
        # a list, on its element type: ["str?"]); optional entries are left out of `options`.
        config = _config()
        required = [
            key
            for key, kind in config["schema"].items()
            if not (kind[0] if isinstance(kind, list) else kind).endswith("?")
        ]
        assert [key for key in required if key not in config["options"]] == []


class TestRepositoryYaml__Parsed:
    def test_repository__names_the_repository_url(self) -> None:
        assert _yaml(ROOT / "repository.yaml")["url"] == REPOSITORY_URL
