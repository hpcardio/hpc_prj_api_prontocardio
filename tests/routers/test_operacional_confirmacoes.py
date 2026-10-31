from copy import deepcopy

import pytest

from app_prontocardio.routers.operacional import montar_resposta_confirmacoes


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
            '1',
            'NOME CADASTRAL ALTERADO',
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
    ],
)
def test_formata_unidades_diagnosticas_com_endereco(
    codigo,
    nome_atual,
    nome_esperado,
):
    resposta = montar_resposta_confirmacoes(
        [
            {
                'cd_paciente': 10,
                'cd_unidade_atendimento': codigo,
                'ds_unidade_atendimento': nome_atual,
            }
        ]
    )

    assert (
        resposta['agendamentos'][0]['ds_unidade_atendimento']
        == nome_esperado
    )


@pytest.mark.parametrize(
    ('codigo', 'nome'),
    [
        (3, 'OUTRA UNIDADE'),
        (None, None),
    ],
)
def test_preserva_unidade_nao_mapeada_e_nome_nulo(codigo, nome):
    resposta = montar_resposta_confirmacoes(
        [
            {
                'cd_paciente': 10,
                'cd_unidade_atendimento': codigo,
                'ds_unidade_atendimento': nome,
            }
        ]
    )

    assert resposta['agendamentos'][0]['ds_unidade_atendimento'] == nome


def test_montar_resposta_preserva_contagens_e_nao_muta_linhas():
    linhas = [
        {
            'cd_paciente': 10,
            'cd_unidade_atendimento': 1,
            'ds_unidade_atendimento': 'CLINICA PRONTOCARDIO SAUDE',
        },
        {
            'cd_paciente': 10,
            'cd_unidade_atendimento': 2,
            'ds_unidade_atendimento': 'UNIDADE DIAGNOSTICO 2',
        },
        {
            'cd_paciente': 20,
            'cd_unidade_atendimento': 3,
            'ds_unidade_atendimento': 'OUTRA UNIDADE',
        },
    ]
    linhas_antes = deepcopy(linhas)
    total_agendamentos_esperado = len(linhas)
    total_pacientes_esperado = len({linha['cd_paciente'] for linha in linhas})

    resposta = montar_resposta_confirmacoes(linhas)

    assert resposta['total'] == total_agendamentos_esperado
    assert resposta['total_agendamentos'] == total_agendamentos_esperado
    assert resposta['total_pacientes'] == total_pacientes_esperado
    assert linhas == linhas_antes
