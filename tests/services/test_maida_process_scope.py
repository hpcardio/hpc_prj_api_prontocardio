from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app_prontocardio.routers import financeiro as f


def test_lot_scope_recovers_reprocessed_origin_and_rejects_ambiguity(monkeypatch):
    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *a: True)
    monkeypatch.setattr(f, '_tabela_schema_existe', lambda *a: True)
    engine = create_engine('sqlite://')
    with engine.connect() as conn:
        conn.connection.create_function('BTRIM', 1, lambda v: v.strip() if v else v)
        for schema in ('api_prontocardio', 'raw_issec_portal'):
            conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
        conn.exec_driver_sql("CREATE TABLE raw_issec_portal.processos (processo TEXT)")
        conn.exec_driver_sql("INSERT INTO raw_issec_portal.processos VALUES ('2600035871'), ('2600099999')")
        conn.exec_driver_sql("CREATE TABLE api_prontocardio.registros_glosa (id INTEGER, cd_remessa INTEGER, cd_atendimento INTEGER, conta INTEGER, cd_lancamento INTEGER, motivo_glosa TEXT, guia TEXT, procedimento TEXT, sn_ativo TEXT, convenio TEXT, processo_controle_fatura_gab TEXT)")
        conn.exec_driver_sql("CREATE TABLE api_prontocardio.registros_glosa_demonstrativo_ipm (registro_glosa_id INTEGER, id_registro TEXT)")
        conn.exec_driver_sql("CREATE TABLE api_prontocardio.demonstrativo_conta_ipm (id_registro TEXT, numero_lote TEXT, origem_maida TEXT)")
        for ident, launch, process in [(1, 447, '2600035871'), (2, 447, '31644'), (3, 744, '31644'), (4, 921, '31644')]:
            conn.execute(text("INSERT INTO api_prontocardio.registros_glosa VALUES (:id,20021,340315,25219,:launch,'1705','552562','70360','true','ISSEC',:process)"), dict(id=ident, launch=launch, process=process))
        for ident in (2, 3, 4):
            conn.execute(text("INSERT INTO api_prontocardio.registros_glosa_demonstrativo_ipm VALUES (:id,:id)"), {'id': ident})
            conn.execute(text("INSERT INTO api_prontocardio.demonstrativo_conta_ipm VALUES (:id,'31644','lotes')"), {'id': ident})
        with Session(conn) as session:
            assert f._processos_lotes_maida_follow_up(session) == {i: '2600035871' for i in (1, 2, 3, 4)}
            # An unrelated remittance with the same lot must not inherit the process.
            conn.exec_driver_sql("INSERT INTO api_prontocardio.registros_glosa SELECT 5,999,cd_atendimento,conta,cd_lancamento,motivo_glosa,guia,procedimento,sn_ativo,convenio,'31644' FROM api_prontocardio.registros_glosa WHERE id=3")
            conn.exec_driver_sql("INSERT INTO api_prontocardio.registros_glosa_demonstrativo_ipm VALUES (5,'3')")
            assert 5 not in f._processos_lotes_maida_follow_up(session)
            conn.exec_driver_sql("UPDATE api_prontocardio.registros_glosa SET processo_controle_fatura_gab='2600099999' WHERE id=3")
            assert f._processos_lotes_maida_follow_up(session) == {}


def test_grouped_process_keeps_source_amounts_and_saved_treatment(monkeypatch):
    from datetime import date
    from decimal import Decimal as D
    from unittest.mock import Mock
    from app_prontocardio.models import RegistroGlosa

    from dataclasses import fields
    records = []
    for ident, launch, process, value in [
        (1,447,'2600035871','19.33'), (2,447,'31644','19.33'),
        (3,744,'31644','4.30'), (4,921,'31644','5.58'),
    ]:
        values = {field.name: None for field in fields(RegistroGlosa) if field.init}
        values.update(cd_remessa=20021,
            cd_atendimento=340315, conta=25219, cd_lancamento=launch,
            codigo_paciente=1, nm_paciente='Paciente teste', convenio='ISSEC',
            processo_controle_fatura_gab=process, valor=D(value),
            qtd_registro=D(1), motivo_glosa='1705', sn_ativo='true',
            sn_glosado='true', data_glosa=date(2026,9,1),
            dt_recurso=date(2026,10,1) if ident==1 else None,
            valor_recursado=D('.08') if ident==1 else None)
        record = RegistroGlosa(**values)
        record.id = ident
        records.append(record)
    session=Mock()
    session.scalars.side_effect=[records, []]
    monkeypatch.setattr(f, '_processos_lotes_maida_follow_up', lambda *a: {i:'2600035871' for i in (1,2,3,4)})
    monkeypatch.setattr(f, '_duplicatas_maida_pendentes', lambda *a: set())
    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *a: False)
    for name in ('_descricoes_tiss','_protocolos_cogestao_por_processo_glosa_follow_up','_processos_canonicos_por_protocolo_follow_up'):
        monkeypatch.setattr(f,name,lambda *a: {})
    origin={i:dict(origem_maida=True,valor_glosa=D(glosa),valor_processado=D(value),valor_liberado=D(value)-D(glosa),quantidade_maida_completa=True,quantidade_maida=qtd) for i,glosa,value,qtd in [(2,'.08','109.56',17),(3,'4.30','4.30',2),(4,'26.04','26.04',14)]}
    monkeypatch.setattr(f,'_dados_demonstrativo_registros_follow_up',lambda *a: origin)
    filters=dict.fromkeys(['q','numero_nfse','cd_remessa','convenio','processo_original','processo_recurso','paciente','cd_atendimento','tipo_atendimento'])
    filters['processo_original']='2600035871'
    cards=f._cards_registros_glosa_follow_up(session,set(),**filters)
    assert len(cards)==1
    card=cards[0]
    assert card['processo']['numero_processo']=='2600035871'
    assert card['valor_glosado']==D('30.42')
    assert card['valor_total_tratado']==D('.08')
    assert card['valor_glosa_pendente']==D('30.34')
    items=card['pacientes'][0]['itens']
    assert len(items)==3
    assert items[0]['valor_glosa']==D('.08')
    assert items[0]['valor_processado']==D('109.56')
    assert items[0]['registro_recusa'] is records[0]
    assert records[2].processo_controle_fatura_gab=='31644'
    query=session.scalars.call_args_list[0].args[0]
    assert [1,2,3,4] in query.compile().params.values()
