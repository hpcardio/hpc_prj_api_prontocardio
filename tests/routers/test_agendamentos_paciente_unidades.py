import pytest

from app_prontocardio.routers.agendamentos import (
    montar_resposta_agendamentos_paciente,
)


def test_monta_agendamentos_normaliza_unidade_e_preserva_deduplicacao():
    item_movimento_mais_recente = 20
    rows = [
        {
            'cd_it_agenda_central': 1084690,
            'item_movimento_id': 10,
            'cd_unidade_atendimento': 2,
            'ds_unidade_atendimento': 'UNIDADE DIAGNOSTICO 2',
            'status': 'M',
        },
        {
            'cd_it_agenda_central': 1084690,
            'item_movimento_id': item_movimento_mais_recente,
            'cd_unidade_atendimento': 2,
            'ds_unidade_atendimento': 'UNIDADE DIAGNOSTICO 2',
            'status': 'A',
        },
    ]

    resposta = montar_resposta_agendamentos_paciente(rows)

    assert resposta['total'] == 1
    assert (
        resposta['agendamentos'][0]['item_movimento_id']
        == item_movimento_mais_recente
    )
    assert resposta['agendamentos'][0]['status'] == 'A'
    assert resposta['agendamentos'][0]['ds_unidade_atendimento'] == (
        'Unidade Diagnóstica 02 - Endereço: Avenida Barão de Studart '
        '- Nº 600 - Meireles'
    )
    assert 'cd_unidade_atendimento' not in resposta['agendamentos'][0]


@pytest.mark.parametrize(
    ('codigo', 'nome_atual', 'nome_esperado'),
    [
        (
            1,
            'CLINICA PRONTOCARDIO SAUDE',
            'Unidade Diagnóstica 01 - Endereço: Avenida Barão de Studart '
            '- Nº 560 - Meireles',
        ),
        (3, 'OUTRA UNIDADE', 'OUTRA UNIDADE'),
        (None, None, None),
    ],
)
def test_montar_resposta_agendamentos_paciente_preserva_demais_unidades(
    codigo,
    nome_atual,
    nome_esperado,
):
    resposta = montar_resposta_agendamentos_paciente(
        [
            {
                'cd_it_agenda_central': 1,
                'item_movimento_id': None,
                'cd_unidade_atendimento': codigo,
                'ds_unidade_atendimento': nome_atual,
            }
        ]
    )

    assert resposta['total'] == 1
    assert (
        resposta['agendamentos'][0]['ds_unidade_atendimento']
        == nome_esperado
    )
    assert 'cd_unidade_atendimento' not in resposta['agendamentos'][0]
