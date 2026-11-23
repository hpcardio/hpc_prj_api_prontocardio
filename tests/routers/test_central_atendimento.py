# ruff: noqa: PLR2004, E501
"""Central de Atendimento recuperada para o Git (rodava fora dele em produção).

Hoje a Central é só leitura: abrir atendimento devolve 423 sem gravar no MV.
"""

import uuid
from http import HTTPStatus
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_prontocardio import app as app_module
from app_prontocardio.central_atendimento_schema import CentralCommand
from app_prontocardio.routers import central_atendimento as rota
from app_prontocardio.services.central_atendimento_rules import (
    ValidationFacts,
    canonical_command_hash,
    validate_command,
)
from app_prontocardio.settings import Settings


def _comando(**extra):
    dados = {
        'attempt_id': str(uuid.uuid4()),
        'patient_code': 10,
        'attendance_type': 'AMBULATORIAL',
        'source': 'AVULSO',
        'item_code': 1,
        'insurance_code': 2,
        'plan_code': 3,
        'unit_code': 4,
        'sector_code': 5,
        'provider_code': 6,
        'origin_clinic_code': 7,
        'appointment_code': None,
        'ticket_code': None,
        'risk_classification_code': None,
    }
    dados.update(extra)
    return CentralCommand.model_validate(dados)


def _fatos(**extra):
    dados = {
        'source_fresh': True,
        'source_matches_type': True,
        'appointment_cancelled': False,
        'ticket_ambiguous': False,
        'account_closed': False,
        'bed_occupied': False,
        'attendance_exists': False,
        'official_plan_code': 3,
    }
    dados.update(extra)
    return ValidationFacts(**dados)


def test_regras_aceitam_comando_coerente_e_listam_cada_motivo():
    assert validate_command(_comando(), _fatos()).accepted is True
    decisao = validate_command(
        _comando(plan_code=9),
        _fatos(source_fresh=False, bed_occupied=True),
    )
    assert decisao.accepted is False
    assert decisao.reason_codes == ('SOURCE_STALE', 'BED_OCCUPIED', 'PLAN_DIVERGENT')


def test_hash_do_comando_ignora_o_id_da_tentativa():
    a = _comando()
    b = a.model_copy(update={'attempt_id': uuid.uuid4()})
    assert canonical_command_hash(a) == canonical_command_hash(b)
    assert canonical_command_hash(a) != canonical_command_hash(a.model_copy(update={'item_code': 99}))


def test_gravacao_no_mv_so_com_flag_e_escopo():
    desligada = Settings(CENTRAL_MV_WRITE_ENABLED=False, CENTRAL_MV_WRITE_SCOPES='4:U')
    assert desligada.central_mv_write_allowed('4', 'U') is False
    ligada = Settings(CENTRAL_MV_WRITE_ENABLED=True, CENTRAL_MV_WRITE_SCOPES=' 4:U , 5:A ')
    assert ligada.central_mv_write_scopes == frozenset({'4:U', '5:A'})
    assert ligada.central_mv_write_allowed('4', 'U') is True
    assert ligada.central_mv_write_allowed('4', 'A') is False


@pytest.mark.parametrize(
    ('perfil', 'telas', 'permitido'),
    [
        ('usuario', [], False),
        ('usuario', ['central_atendimento'], True),
        ('leitura', [], True),
        ('ti', [], True),
    ],
)
def test_acesso_a_central(perfil, telas, permitido):
    usuario = SimpleNamespace(perfil=perfil, telas_permitidas=telas)
    if permitido:
        assert rota.central_user(usuario) is usuario
    else:
        with pytest.raises(HTTPException) as erro:
            rota.central_user(usuario)
        assert erro.value.status_code == HTTPStatus.FORBIDDEN


def test_perfil_leitura_so_libera_consultas_feitas_por_post():
    # O middleware bloqueia POST de perfil leitura; as consultas da Central
    # usam POST e ficam liberadas. Abrir/pré-validar/retomar continuam bloqueados.
    liberadas = app_module.CENTRAL_READ_ONLY_POST_PATHS
    assert '/central-atendimento/pacientes/pesquisar' in liberadas
    for escrita in ('/central-atendimento/abrir', '/central-atendimento/pre-validar'):
        assert escrita not in liberadas
    assert not any('tentativas' in caminho for caminho in liberadas)


def test_tela_central_e_restrita():
    from app_prontocardio.permissions import (  # noqa: PLC0415
        TELAS_PADRAO,
        TELAS_RESTRITAS,
    )

    assert 'central_atendimento' in TELAS_RESTRITAS
    assert 'central_atendimento' not in TELAS_PADRAO


def test_abrir_atendimento_nao_grava_no_mv(session):
    usuario = SimpleNamespace(id=1, perfil='ti', telas_permitidas=[])
    with pytest.raises(HTTPException) as erro:
        rota.open_attendance(_comando(), usuario, session)
    assert erro.value.status_code == HTTPStatus.LOCKED
    assert erro.value.detail['code'] == 'MV_WRITE_DISABLED'
