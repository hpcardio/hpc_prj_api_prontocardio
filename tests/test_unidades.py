import pytest

from app_prontocardio.unidades import nome_unidade_publica


@pytest.mark.parametrize(
    ('codigo', 'nome_atual', 'nome_esperado'),
    [
        (
            1,
            'CLINICA PRONTOCARDIO SAUDE',
            'Unidade Diagnóstica 01 - Endereço: Avenida Barão de Studart '
            '- Nº 560 - Meireles',
        ),
        (
            ' 1 ',
            'NOME ALTERADO NO MV',
            'Unidade Diagnóstica 01 - Endereço: Avenida Barão de Studart '
            '- Nº 560 - Meireles',
        ),
        (
            2,
            'UNIDADE DIAGNOSTICO 2',
            'Unidade Diagnóstica 02 - Endereço: Avenida Barão de Studart '
            '- Nº 600 - Meireles',
        ),
        (
            '2',
            'OUTRO NOME CADASTRAL',
            'Unidade Diagnóstica 02 - Endereço: Avenida Barão de Studart '
            '- Nº 600 - Meireles',
        ),
        (3, 'OUTRA UNIDADE', 'OUTRA UNIDADE'),
        (None, 'UNIDADE SEM CÓDIGO', 'UNIDADE SEM CÓDIGO'),
        (None, None, None),
    ],
)
def test_nome_unidade_publica(
    codigo,
    nome_atual,
    nome_esperado,
):
    assert nome_unidade_publica(codigo, nome_atual) == nome_esperado
