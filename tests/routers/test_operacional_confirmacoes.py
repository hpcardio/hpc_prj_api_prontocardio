from copy import deepcopy
from datetime import date

import pytest

from app_prontocardio.routers.operacional import (
    montar_resposta_confirmacoes,
    montar_resumo_confirmacoes,
)


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


def test_montar_resumo_soma_indicadores_sem_dados_de_paciente():
    linhas = [
        {
            'cd_unidade_atendimento': 1,
            'ds_unidade_atendimento': 'CLINICA PRONTOCARDIO SAUDE',
            'total_agendamentos': 12,
            'confirmados': 5,
            'cancelamentos_solicitados': 1,
            'pendentes': 6,
            'encaixes': 2,
            'horarios_regulares': 10,
            'presencas_registradas': 4,
            'atendidos': 3,
            'sem_registro_presenca': 8,
        },
        {
            'cd_unidade_atendimento': 2,
            'ds_unidade_atendimento': 'UNIDADE DIAGNOSTICO 2',
            'total_agendamentos': 8,
            'confirmados': 3,
            'cancelamentos_solicitados': 0,
            'pendentes': 5,
            'encaixes': 1,
            'horarios_regulares': 7,
            'presencas_registradas': 2,
            'atendidos': 2,
            'sem_registro_presenca': 6,
        },
    ]

    resposta = montar_resumo_confirmacoes(
        linhas,
        date(2026, 11, 24),
        date(2026, 12, 24),
    )

    assert resposta['data_inicio'] == '2026-11-24'
    assert resposta['data_fim'] == '2026-12-24'
    assert resposta['totais'] == {
        'total_agendamentos': 20,
        'confirmados': 8,
        'cancelamentos_solicitados': 1,
        'pendentes': 11,
        'encaixes': 3,
        'horarios_regulares': 17,
        'presencas_registradas': 6,
        'atendidos': 5,
        'sem_registro_presenca': 14,
        'taxa_confirmacao': 40.0,
    }
    assert all('nm_paciente' not in grupo for grupo in resposta['grupos'])
