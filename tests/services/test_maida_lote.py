from decimal import Decimal
from unittest.mock import Mock
import pytest
from app_prontocardio.routers import financeiro as f


@pytest.mark.parametrize('lotes,expected', [
    (['31644', ' 31644 ', None], '31644'),
    (['31644', '31645'], None),
    ([None, ''], None),
])
def test_lot_from_demonstrative_requires_unique_value(monkeypatch, lotes, expected):
    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *a: True)
    session = Mock()
    session.execute.return_value.mappings.return_value = [dict(
        registro_glosa_id=1, criterio_correspondencia='maida', codigo_glosa='2601',
        origem_maida='lotes', referencia=None, quantidade_executada=2,
        numero_lote=lote, valor_processado=Decimal('4.30'),
        valor_glosa=Decimal('4.30'), valor_liberado=0,
    ) for lote in lotes]
    origin = f._dados_demonstrativo_registros_follow_up(session, {1})
    assert origin[1]['numero_lote'] == expected
    assert 'demo.numero_lote' in str(session.execute.call_args.args[0])
    record = Mock(id=1, status_tratativa='pendente', qtd_registro=2,
                  valor=Decimal('4.30'), sn_ativo='true', motivo_glosa='2601',
                  numero_lote=None)
    item = f._item_follow_up_glosa([record], origin, {})
    assert item['numero_lote'] == expected
    assert item['valor_glosa'] == Decimal('4.30') * len(lotes)


@pytest.mark.parametrize('status', ['recurso', 'acato'])
def test_saved_treatment_lot_takes_precedence(status):
    origin = {1: {'numero_lote': '31644'}}
    source = Mock(id=1, status_tratativa='pendente', qtd_registro=2,
                  valor=Decimal('4.30'), sn_ativo='true', motivo_glosa='2601',
                  numero_lote=None)
    saved = Mock(id=2, status_tratativa=status, numero_lote='LOTE-INFORMADO',
                 valor_recursado=Decimal('4.30'), sn_ativo='true')
    item = f._item_follow_up_glosa([source, saved], origin, {})
    assert item['numero_lote'] == 'LOTE-INFORMADO'
