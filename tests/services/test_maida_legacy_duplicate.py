from unittest.mock import Mock

from sqlalchemy import create_engine, text

from app_prontocardio.routers import financeiro as f


def test_legacy_duplicate_requires_exact_identity_and_no_treatment(
    monkeypatch,
):
    monkeypatch.setattr(f, '_tabela_ipm_existe', lambda *a: True)
    capture = Mock()
    capture.execute.return_value.scalars.return_value = []
    f._duplicatas_maida_pendentes(capture, set(range(1, 10)))
    sql = str(capture.execute.call_args.args[0])
    sql = sql.replace('g.id = ANY(:ids)', 'g.id < 10')
    sql = sql.replace('BTRIM(', 'TRIM(')
    sql = sql.replace('d.data_realizacao::date', 'date(d.data_realizacao)')
    sql = sql.replace(
        'COALESCE(g.data_lancamento, g.data_atendimento)::date',
        'date(COALESCE(g.data_lancamento, g.data_atendimento))',
    )
    with create_engine('sqlite://').begin() as conn:
        conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS api_prontocardio")
        conn.exec_driver_sql("""CREATE TABLE api_prontocardio.registros_glosa (
            id INTEGER, sn_ativo TEXT, convenio TEXT, origem_registro TEXT,
            dt_recurso TEXT, sn_glosado TEXT, cd_remessa INTEGER,
            cd_atendimento INTEGER, conta INTEGER, motivo_glosa TEXT,
            numero_lote TEXT, processo_controle_fatura_gab TEXT, guia TEXT,
            procedimento TEXT, cd_tuss TEXT, data_lancamento TEXT,
            data_atendimento TEXT, valor_recursado NUMERIC, valor NUMERIC)""")
        conn.exec_driver_sql("""CREATE TABLE
            api_prontocardio.registros_glosa_demonstrativo_ipm (
            registro_glosa_id INTEGER, id_registro TEXT)""")
        conn.exec_driver_sql("""CREATE TABLE
            api_prontocardio.demonstrativo_conta_ipm (
            id_registro TEXT, origem_maida TEXT, numero_lote TEXT,
            numero_guia_senha TEXT, codigo_servico TEXT,
            data_realizacao TEXT, valor_glosa NUMERIC)""")
        conn.exec_driver_sql("""INSERT INTO api_prontocardio.registros_glosa
            VALUES(100,'true','ISSEC','triagem',NULL,'true',20035,1,424496,
            '1811',NULL,'31554','580314','40101010',NULL,
            '2026-08-26','2026-08-26',NULL,30)""")
        for ident in range(1, 10):
            conn.exec_driver_sql(
                """INSERT INTO api_prontocardio.registros_glosa
                SELECT ?,sn_ativo,convenio,origem_registro,
                dt_recurso,sn_glosado,
                cd_remessa,cd_atendimento,conta,motivo_glosa,numero_lote,
                processo_controle_fatura_gab,guia,procedimento,cd_tuss,
                data_lancamento,data_atendimento,valor_recursado,valor
                FROM api_prontocardio.registros_glosa WHERE id=100""",
                (ident,),
            )
        conn.exec_driver_sql("""INSERT INTO
            api_prontocardio.registros_glosa_demonstrativo_ipm
            VALUES(100,'x')""")
        conn.exec_driver_sql("""INSERT INTO
            api_prontocardio.demonstrativo_conta_ipm VALUES(
            'x','lotes','31554','580314','40101010','2026-08-26',30)""")
        changes = {
            2: "dt_recurso='2026-10-21'",
            3: "sn_glosado='not'",
            4: 'conta=999',
            5: "numero_lote='999'",
            6: "guia='999'",
            7: "data_lancamento='2026-08-25'",
            8: 'valor=31',
            9: "motivo_glosa='2601'",
        }
        for ident, change in changes.items():
            conn.exec_driver_sql(
                f'UPDATE api_prontocardio.registros_glosa SET {change} '
                f'WHERE id={ident}'
            )
        assert set(conn.execute(text(sql)).scalars()) == {1}
