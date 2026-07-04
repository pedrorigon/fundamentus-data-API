from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_html() -> Callable[[str], str]:
    def load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="iso-8859-1")

    return load
