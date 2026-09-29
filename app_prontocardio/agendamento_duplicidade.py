from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def duplicidade_exige_confirmacao(
    duplicado: Mapping[str, Any] | None,
    confirmar_duplicidade: bool,
) -> bool:
    """Indica se o operador ainda precisa decidir sobre a duplicidade."""
    return duplicado is not None and not confirmar_duplicidade
