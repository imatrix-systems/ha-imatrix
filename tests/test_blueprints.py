from pathlib import Path

import pytest
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint import models
from homeassistant.util.yaml import load_yaml

BLUEPRINTS = sorted(
    (Path(__file__).parent.parent / "blueprints" / "automation" / "imatrix").glob(
        "*.yaml"
    )
)


def test_six_blueprints() -> None:
    assert len(BLUEPRINTS) == 6


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.stem)
def test_blueprint_is_valid(path: Path) -> None:
    blueprint = models.Blueprint(
        load_yaml(path),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    assert blueprint.metadata["source_url"].endswith(f"/{path.name}")
    assert blueprint.inputs
