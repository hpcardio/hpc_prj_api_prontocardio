from unittest.mock import Mock
from app_prontocardio.routers import financeiro


def test_duplicate_scope_keeps_treatments_and_requires_exact_demonstrative(monkeypatch):
    monkeypatch.setattr(financeiro, '_tabela_ipm_existe', lambda *a: True)
    session = Mock()
    session.execute.return_value.scalars.return_value = [10, 11]
    assert financeiro._duplicatas_maida_pendentes(session, {10, 11, 12}) == {10, 11}
    sql = str(session.execute.call_args.args[0])
    for condition in ['g.dt_recurso IS NULL', 'NOT EXISTS', 'canonical.sn_ativo',
        'd.cd_remessa = g.cd_remessa', 'd.numero_lote', 'd.numero_guia_senha',
        'd.codigo_servico', 'd.data_realizacao', 'g.valor_recursado']:
        assert condition in sql
    assert session.execute.call_args.args[1] == {'ids': [10, 11, 12]}


def test_no_records_or_missing_tables_do_not_query(monkeypatch):
    session = Mock()
    assert financeiro._duplicatas_maida_pendentes(session, set()) == set()
    monkeypatch.setattr(financeiro, '_tabela_ipm_existe', lambda *a: False)
    assert financeiro._duplicatas_maida_pendentes(session, {1}) == set()
    session.execute.assert_not_called()


def test_cards_remove_duplicate_group_before_patient_and_total_calculation(monkeypatch):
    from types import SimpleNamespace
    from decimal import Decimal
    from datetime import date
    records = [SimpleNamespace(id=i, processo_controle_fatura_gab=process,
        cd_remessa=17942, convenio='ISSEC', data_glosa=date(2026, 5, 1),
        status_tratativa='pendente') for i, process in [(10, '2600015457'), (20, '24683')]]
    session = Mock()
    session.scalars.side_effect = [records, []]
    monkeypatch.setattr(financeiro, '_duplicatas_maida_pendentes', lambda *a: {10})
    monkeypatch.setattr(financeiro, '_tabela_ipm_existe', lambda *a: False)
    monkeypatch.setattr(financeiro, '_dados_demonstrativo_registros_follow_up', lambda *a: {})
    monkeypatch.setattr(financeiro, '_descricoes_tiss', lambda *a: {})
    monkeypatch.setattr(financeiro, '_protocolos_cogestao_por_processo_glosa_follow_up', lambda *a: {})
    monkeypatch.setattr(financeiro, '_processos_canonicos_por_protocolo_follow_up', lambda *a: {})
    patients = Mock(return_value=[dict(valor_itens=Decimal('6.27'),
        valor_glosado=Decimal('0.34'), valor_total_tratado=Decimal('0'))])
    monkeypatch.setattr(financeiro, '_pacientes_follow_up_glosa', patients)
    cards = financeiro._cards_registros_glosa_follow_up(session, set(), **dict.fromkeys([
        'q', 'numero_nfse', 'cd_remessa', 'convenio', 'processo_original',
        'processo_recurso', 'paciente', 'cd_atendimento', 'tipo_atendimento']))
    assert len(cards) == 1
    assert cards[0]['valor_glosado'] == Decimal('0.34')
    assert cards[0]['valor_glosa_pendente'] == Decimal('0.34')
    assert patients.call_args.args[0] == [records[1]]
