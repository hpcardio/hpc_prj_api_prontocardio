# ruff: noqa: PLR2004
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from jwt import decode
from sqlalchemy.exc import SQLAlchemyError

from app_prontocardio import security
from app_prontocardio.permissions import telas_padrao
from app_prontocardio.routers import contas_pagar


@pytest.fixture
def person():
    return SimpleNamespace(
        id=7,
        email='synthetic@example.test',
        senha='synthetic-hash',
        ativo=True,
        perfil='usuario',
    )


def test_current_token_expiry_and_password_revocation(person):
    session = Mock()
    session.scalar.return_value = person
    token = security.criar_token_usuario(person)
    claims = decode(
        token,
        security.settings.SECRET_KEY,
        algorithms=[security.settings.ALGORITHM],
    )
    assert claims['exp'] - claims['iat'] == timedelta(hours=8).total_seconds()
    assert security.valida_token_usuario_atual(session, token) is person
    person.senha = 'changed-hash'
    with pytest.raises(HTTPException) as error:
        security.valida_token_usuario_atual(session, token)
    assert error.value.status_code == 401


def test_inactive_user_cannot_keep_session(person):
    session = Mock()
    session.scalar.return_value = person
    token = security.criar_token_usuario(person)
    person.ativo = False
    with pytest.raises(HTTPException) as error:
        security.valida_token_usuario_atual(session, token)
    assert error.value.status_code == 401


@pytest.mark.parametrize('kind', ['legacy', 'expired', 'long_lived'])
def test_invalid_session_lifetimes_are_rejected(person, kind):
    session = Mock()
    session.scalar.return_value = person
    now = datetime.now(timezone.utc)
    claim = {'sub': person.email}
    if kind != 'legacy':
        claim.update(
            iat=now - timedelta(hours=2),
            exp=now - timedelta(hours=1)
            if kind == 'expired'
            else now + timedelta(hours=12),
            sv=security._versao_credencial(person),
        )
    with pytest.raises(HTTPException) as error:
        security.valida_token_usuario_atual(
            session, security.criar_token(claim)
        )
    assert error.value.status_code == 401


def test_clinical_audience_token_contract_is_preserved():
    token = security.criar_token({'sub': 'synthetic', 'aud': 'clinical-panel'})
    assert (
        decode(
            token,
            security.settings.SECRET_KEY,
            algorithms=[security.settings.ALGORITHM],
            audience='clinical-panel',
        )['sub']
        == 'synthetic'
    )


def test_spu_requires_explicit_assignment():
    assert 'spu_automacao' not in telas_padrao()


@pytest.mark.parametrize(
    'operation',
    [
        contas_pagar.registrar_pagamento_titulo,
        contas_pagar.atualizar_pagamento_titulo,
    ],
)
def test_foreign_supplier_payment_is_rejected_before_postgres_write(operation):
    oracle, postgres = Mock(), Mock()
    oracle.execute.return_value.scalar_one_or_none.return_value = None
    args = dict(
        codigo_fornecedor=10,
        codigo_parcela=20,
        payload=Mock(),
        usuario=Mock(),
        session=postgres,
        oracle_session=oracle,
    )
    if operation == contas_pagar.atualizar_pagamento_titulo:
        args['pagamento_id'] = 30
    with pytest.raises(HTTPException) as error:
        operation(**args)
    assert error.value.status_code == 404
    postgres.execute.assert_not_called()


def test_oracle_failure_does_not_allow_payment():
    oracle = Mock()
    oracle.execute.side_effect = SQLAlchemyError('synthetic failure')
    with pytest.raises(HTTPException) as error:
        contas_pagar._validar_fornecedor_titulo(oracle, 10, 20)
    assert error.value.status_code == 503


def test_payment_from_another_supplier_does_not_reduce_balance():
    titles = [
        dict(
            codigo_fornecedor=10,
            nome_fornecedor='Synthetic',
            codigo_parcela=20,
            valor_total=Decimal('100'),
            valor_honrado_oracle=Decimal('0'),
            data_vencimento=date(2026, 1, 1),
        )
    ]
    payments = {
        20: [
            dict(codigo_fornecedor=99, valor_pago=Decimal('80')),
            dict(codigo_fornecedor=10, valor_pago=Decimal('5')),
        ]
    }
    result = contas_pagar._agrupar_fornecedores(titles, payments)
    assert result[0]['valor_total_honrado'] == Decimal('5')
    assert result[0]['saldo_a_pagar'] == Decimal('95')
