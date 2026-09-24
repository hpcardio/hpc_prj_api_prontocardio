from __future__ import annotations

import unicodedata


def _normalized(value: str | None) -> str:
    return ''.join(
        character
        for character in unicodedata.normalize('NFKD', value or '')
        if not unicodedata.combining(character)
    ).strip().lower()


def select_ipm_models(
    attendance_type: str | None,
    purpose: str = 'procedimentos',
    has_opme: bool = False,
) -> tuple[str, ...]:
    """Select the official IPM guide sequence for the current request."""
    normalized_purpose = _normalized(purpose).replace(' ', '_')
    normalized_attendance = _normalized(attendance_type)

    if normalized_purpose == 'nova_internacao':
        primary = 'g2'
    elif normalized_attendance in {'i', 'internacao', 'internado'}:
        primary = 'g3'
    else:
        primary = 'g1'

    return (primary, 'g4') if has_opme else (primary,)


def ipm_page_count(exam_count: int, models: tuple[str, ...]) -> int:
    primary = models[0]
    capacity = 10 if primary == 'g3' else 5 if primary == 'g1' else max(exam_count, 1)
    primary_pages = max(1, (exam_count + capacity - 1) // capacity)
    return primary_pages + (1 if 'g4' in models else 0)
