from decimal import Decimal
from unittest.mock import Mock
import pytest
from app_prontocardio.routers import financeiro as f


@pytest.mark.parametrize('count,unit,expected', [(10, '3.23', '32.30'), (9, '0.11', '0.99')])
def test_quantity_follows_the_same_demonstrative_rows_as_amounts(monkeypatch,count,unit,expected):
    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *a: True)
    session = Mock()
    session.execute.return_value.mappings.return_value = [dict(registro_glosa_id=1, criterio_correspondencia='maida', codigo_glosa='1', origem_maida='lotes', referencia=None, quantidade_executada=Decimal(1), valor_processado=Decimal(unit), valor_glosa=Decimal(unit), valor_liberado=0) for _ in range(count)]
    origin=f._dados_demonstrativo_registros_follow_up(session,{1})
    record=Mock(id=1,status_tratativa='pendente',qtd_registro=Decimal(1),valor=Decimal(unit),sn_ativo='true',motivo_glosa='1')
    item=f._item_follow_up_glosa([record],origin,{})
    assert item['qt_lancamento']==count
    assert item['qtd_glosada']==count
    assert item['valor_glosa']==Decimal(expected)
    assert item['registro_glosa'] is record
    assert record.qtd_registro==1


@pytest.mark.parametrize('maida,complete',[(False,True),(True,False)])
def test_other_sources_or_unknown_quantity_preserve_existing_quantity(maida,complete):
    record=Mock(id=1,status_tratativa='pendente',qtd_registro=Decimal(2),valor=Decimal('0.65'),sn_ativo='true',motivo_glosa='1')
    origin={1:dict(origem_maida=maida,quantidade_maida_completa=complete,quantidade_maida=10)}
    assert f._item_follow_up_glosa([record],origin,{})['qt_lancamento']==2
