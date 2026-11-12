from datetime import datetime

from app_prontocardio.routers import agendamentos as agendamentos_module
from app_prontocardio.routers.agendamentos import (
    _deve_liberar_bloqueio_residual,
)


def _agendamento_anterior(
    *,
    bloqueado='S',
    data_agendamento=None,
    data_bloqueio=None,
):
    return (
        10,
        20,
        30,
        40,
        bloqueado,
        data_agendamento,
        data_bloqueio,
    )


def test_libera_apenas_bloqueio_anterior_ao_agendamento():
    assert _deve_liberar_bloqueio_residual(
        _agendamento_anterior(
            data_agendamento=datetime(2026, 10, 8, 9),
            data_bloqueio=datetime(2026, 10, 8, 8),
        )
    )


def test_preserva_bloqueio_manual_posterior_ao_agendamento():
    assert not _deve_liberar_bloqueio_residual(
        _agendamento_anterior(
            data_agendamento=datetime(2026, 10, 8, 8),
            data_bloqueio=datetime(2026, 10, 8, 9),
        )
    )


def test_nao_libera_slot_sem_bloqueio_ativo():
    assert not _deve_liberar_bloqueio_residual(
        _agendamento_anterior(
            bloqueado='N',
            data_agendamento=datetime(2026, 10, 8, 9),
            data_bloqueio=datetime(2026, 10, 8, 8),
        )
    )


def test_update_endereco_nao_envia_bind_que_nao_existe_no_sql():
    montar_parametros = getattr(
        agendamentos_module,
        '_parametros_update_endereco',
        None,
    )

    assert montar_parametros is not None
    assert montar_parametros(
        {
            'cd_paciente': 23619,
            'nr_cep': '60115220',
            'ds_endereco': 'RUA TESTE',
            'nr_endereco': '177',
            'ds_complemento': 'CASA',
            'nm_bairro': 'CENTRO',
            'cd_cidade': 1,
        },
        cd_endereco=99,
    ) == {
        'nr_cep': '60115220',
        'ds_endereco': 'RUA TESTE',
        'nr_endereco': '177',
        'ds_complemento': 'CASA',
        'nm_bairro': 'CENTRO',
        'cd_cidade': 1,
        'cd_endereco': 99,
    }


def test_update_endereco_legado_envia_apenas_binds_existentes_no_sql():
    montar_parametros = getattr(
        agendamentos_module,
        '_parametros_update_endereco_legado',
        None,
    )

    assert montar_parametros is not None
    assert montar_parametros(
        {
            'cd_paciente': 23619,
            'nr_cep': '60115220',
            'ds_endereco': 'RUA TESTE',
            'nr_endereco': '177',
            'ds_complemento': 'CASA',
            'nm_bairro': 'CENTRO',
            'cd_cidade': 1,
        },
        nr_fone='989403457',
        cd_endereco=100,
    ) == {
        'nr_cep': '60115220',
        'ds_endereco': 'RUA TESTE',
        'nr_endereco': '177',
        'ds_complemento': 'CASA',
        'nm_bairro': 'CENTRO',
        'nr_fone': '989403457',
        'cd_endereco': 100,
    }
