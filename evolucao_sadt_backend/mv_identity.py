import re

_MV_USER_PATTERN = re.compile(r'^[A-Z0-9_.-]{2,30}$')


class EvolutionOwnershipError(PermissionError):
    pass


def normalize_mv_user(value: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ''
    if not _MV_USER_PATTERN.fullmatch(normalized):
        raise ValueError('Usuário MV inválido.')
    return normalized


def assert_evolution_ownership(
    *,
    evolution_user: str,
    evolution_provider: int,
    logged_user: str,
    logged_provider: int,
) -> None:
    if (
        normalize_mv_user(evolution_user) != normalize_mv_user(logged_user)
        or evolution_provider != logged_provider
    ):
        raise EvolutionOwnershipError(
            'A evolução pertence a outro profissional e não pode ser alterada.'
        )
