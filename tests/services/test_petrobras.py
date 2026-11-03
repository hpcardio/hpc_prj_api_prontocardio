# ruff: noqa: PLR2004
# Literal expected values make these protocol fixtures explicit.
import base64
import importlib.util
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import etree as ET
from pydantic import SecretStr
from sqlalchemy import create_engine, insert, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app_prontocardio.models import Tiss, table_registry
from app_prontocardio.petrobras_models import (
    PetrobrasEvento,
    PetrobrasExecucao,
    PetrobrasGlosa,
    PetrobrasProtocolo,
)
from app_prontocardio.routers import petrobras
from app_prontocardio.services import petrobras_sync
from app_prontocardio.services import petrobras_tiss as tiss
from app_prontocardio.services.petrobras_appeals import (
    response_data,
    send_appeal,
)
from app_prontocardio.services.petrobras_config import PetrobrasConfig
from app_prontocardio.services.petrobras_match import reconcile
from app_prontocardio.services.petrobras_sync import (
    discover_competencia,
    import_rows,
    synchronize,
    synchronize_competencia,
    synchronize_many,
)
from app_prontocardio.services.petrobras_tiss import (
    NS,
    PetrobrasClient,
    TissError,
    element,
    parse_demonstrativos,
    parse_xml,
    payment_protocols,
    text_hash,
    validate,
    validate_response_identity,
    value,
)
from scripts.sync_petrobras import competencias


@pytest.fixture
def config():
    return PetrobrasConfig(
        enabled=True,
        transmission_enabled=True,
        login=SecretStr('synthetic'),
        password=SecretStr('synthetic-only'),
        prestador_cnpj='00000000000000',
    )


@pytest.fixture
def db():

    engine = create_engine(
        'sqlite://',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
        execution_options={'schema_translate_map': {'api_prontocardio': None}},
    )
    tables = [
        t
        for t in table_registry.metadata.sorted_tables
        if t.name.startswith('petrobras_') or t.name == 'tiss'
    ]
    table_registry.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def protocol(db, config):
    row = PetrobrasProtocolo(prestador=config.prestador_cnpj, numero='123')
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def source():
    return {
        'chave': 'synthetic-key',
        'carteira': '00000001',
        'prestador': '00000000000000',
        'demonstrativo': 'D1',
        'protocolo': '123',
        'lote': '1',
        'escopo': 'item',
        'sequencial': '1',
        'guia_prestador': '000000000000000X9999',
        'senha': 'P0123',
        'guia_operadora': '000123',
        'tuss': '00123456',
        'tabela': '22',
        'descricao': 'Procedimento sintético',
        'data_realizacao': '2026-10-01',
        'quantidade': '1.0000',
        'valor_informado': '100.00',
        'valor_liberado': '80.00',
        'valor_glosa': '20.00',
        'motivos': [{'codigo': '1701', 'valor': '20.00'}],
    }


@pytest.fixture
def candidate():
    return {
        'nr_carteira': '00000001',
        'cd_convenio': 37,
        'nr_guia': 'P0123',
        'cd_pro_fat': '00123456',
        'cd_tuss': '99999999',
        'cd_reg': 10,
        'cd_lancamento': 1,
        'data_realizacao': '2026-10-01',
        'quantidade': '1',
        'valor': '100.00',
    }


def test_exact_match_keeps_zeros(source, candidate):
    assert reconcile(source, [candidate], 37)['status'] == 'conciliado'
    bad = {**candidate, 'nr_guia': '123'}
    assert reconcile(source, [bad], 37)['status'] == 'nao_localizado'
    assert (
        reconcile(source, [{**candidate, 'cd_convenio': 9}], 37)['status']
        == 'nao_localizado'
    )


def test_provider_guide_is_never_used_as_key(source, candidate):
    candidate['nr_guia'] = 'X9999'
    assert reconcile(source, [candidate], 37)['oracle'] is None
    source['senha'] = None
    source['guia_prestador'] = '000000000000000P0123'
    candidate['nr_guia'] = 'P0123'
    result = reconcile(source, [candidate], 37)
    assert result['status'] == 'dados_insuficientes'
    assert 'senha' in result['divergencias'][0]


def test_repeated_procedure_never_selects_first(source, candidate):
    other = {**candidate, 'cd_lancamento': 2}
    assert reconcile(source, [candidate, other], 37)['status'] == 'ambiguo'
    other['data_realizacao'] = '2026-10-02'
    assert (
        reconcile(source, [candidate, other], 37)['oracle']['cd_lancamento']
        == 1
    )


def test_financial_divergence_does_not_destroy_match(source, candidate):
    r = reconcile(source, [{**candidate, 'valor': '110.00'}], 37)
    assert r['status'] == 'conciliado'
    assert r['divergencias']


def test_numeric_quantity_disambiguation(source, candidate):
    other = {**candidate, 'cd_lancamento': 2, 'quantidade': '2'}
    assert reconcile(source, [candidate, other], 37)['count'] == 1


def test_idempotence_and_manual_preservation(
    db, protocol, config, source, candidate
):
    scopes = {('123', 'D1')}
    import_rows(db, protocol, [source], scopes, [candidate], config)
    db.commit()
    row = db.scalar(select(PetrobrasGlosa))
    row.justificativa = 'Trabalho manual'
    row.prazo = date(2027, 1, 1)
    db.commit()
    import_rows(db, protocol, [source], scopes, [candidate], config)
    db.commit()
    assert len(list(db.scalars(select(PetrobrasGlosa)))) == 1
    assert row.justificativa == 'Trabalho manual'
    assert row.prazo == date(2027, 1, 1)
    assert len(list(db.scalars(select(PetrobrasEvento)))) == 2
    updated = {**source, 'valor_glosa': '10.00'}
    import_rows(
        db,
        protocol,
        [updated],
        scopes,
        [],
        config,
        oracle_error='Falha Oracle',
    )
    db.commit()
    assert row.justificativa == 'Trabalho manual'
    assert row.oracle['cd_reg'] == 10
    assert row.valor_glosa == Decimal('10.00')
    assert row.erro_oracle


def test_absence_only_in_authoritative_scope(
    db, protocol, config, source, candidate
):
    import_rows(db, protocol, [source], {('123', 'D1')}, [candidate], config)
    db.commit()
    row = db.scalar(select(PetrobrasGlosa))
    import_rows(db, protocol, [], {('123', 'D2')}, [], config)
    db.commit()
    assert row.presente_na_fonte
    import_rows(db, protocol, [], {('123', 'D1')}, [], config)
    db.commit()
    assert not row.presente_na_fonte
    assert row.valor_glosa == Decimal('20')


def test_network_failure_preserves_existing(
    db, protocol, config, source, candidate
):
    import_rows(db, protocol, [source], {('123', 'D1')}, [candidate], config)
    db.commit()
    client = Mock()
    client.demonstrativos.side_effect = TissError('Falha de comunicação')
    with pytest.raises(TissError):
        synchronize(db, config, protocol.id, lambda *a: [], client=client)
    assert db.scalar(select(PetrobrasGlosa)).valor_glosa == Decimal('20')


def demo_xml():
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(
        element(root, 'demonstrativoRetorno'), 'demonstrativoAnaliseConta'
    )
    h = element(demo, 'cabecalhoDemonstrativo')
    element(h, 'registroANS', '422631')
    element(h, 'numeroDemonstrativo', 'D1')
    element(h, 'dataEmissao', '2026-10-01')
    p = element(element(demo, 'dadosConta'), 'dadosProtocolo')
    element(p, 'numeroProtocolo', '123')
    element(p, 'numeroLotePrestador', '1')
    g = element(p, 'relacaoGuias')
    element(g, 'numeroGuiaPrestador', 'P1')
    element(g, 'numeroGuiaOperadora', 'O1')
    d = element(g, 'detalhesGuia')
    element(d, 'sequencialItem', '1')
    proc = element(d, 'procedimento')
    element(proc, 'codigoProcedimento', '001')
    element(proc, 'codigoTabela', '22')
    for code, amount in [('1701', '10.00'), ('1702', '5.00')]:
        r = element(d, 'relacaoGlosa')
        element(r, 'tipoGlosa', code)
        element(r, 'valorGlosa', amount)
    element(g, 'valorGlosaGuia', '20.00')
    element(p, 'valorGlosaProtocolo', '23.00')
    return root


def test_parser_retains_unallocated_denials_without_double_counting():
    root = demo_xml()
    rows, scopes = parse_demonstrativos(root, '00000000000000', '422631')
    assert [r['escopo'] for r in rows] == ['item', 'guia']
    assert sum(Decimal(r['valor_glosa']) for r in rows) == Decimal('20')
    assert rows[0]['total_glosa_protocolo'] == '23.00'
    assert rows[0]['cobertura'] == 'parcial_nao_confirmada'
    assert len(rows[0]['motivos']) == 2
    assert len({r['chave'] for r in rows}) == 2
    assert scopes == {('123', 'D1')}


def test_duplicate_item_rejected():
    root = demo_xml()
    g = root.find('.//{%s}relacaoGuias' % NS)
    g.append(deepcopy(g.find('{%s}detalhesGuia' % NS)))
    with pytest.raises(TissError, match='duplicada'):
        parse_demonstrativos(root, '00000000000000', '422631')


def test_xml_entities_rejected():
    with pytest.raises(TissError):
        parse_xml(
            b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x>&x;</x>'
        )


def test_wire_contracts_and_no_real_transmission(config, source):
    client = PetrobrasClient(config, dry_run=True)
    consulta = client.demonstrativos(['123'])
    assert consulta.tag.endswith('solicitacaoDemonstrativoRetornoWS')
    validate(consulta)
    validate(client.status('123'))
    validate(client.appeal(source, 'Justificativa sintética', 'R001'))
    validate(
        client.document(
            source,
            SimpleNamespace(
                id=1,
                natureza_guia='2',
                formato='02',
                conteudo=b'%PDF-test',
                tipo_documento='02',
            ),
        )
    )
    with pytest.raises(TissError):
        client.appeal(source, 'x' * 501, 'R002')


def test_disabled_transmission(config, source):
    config.transmission_enabled = False
    with pytest.raises(TissError, match='habilitada'):
        PetrobrasClient(config, dry_run=True).appeal(
            source, 'Justificativa', 'R1'
        )


def test_send_timeout_does_not_allow_retry(
    db, protocol, config, source, candidate
):
    import_rows(db, protocol, [source], {('123', 'D1')}, [candidate], config)
    db.commit()
    row = db.scalar(select(PetrobrasGlosa))
    row.justificativa = 'Justificativa'
    row.prazo = date.today() + timedelta(days=30)
    row.origem_prazo = 'Regra comprovada'
    db.commit()
    client = Mock()
    client.appeal.side_effect = TimeoutError()
    with pytest.raises(TissError, match='não confirmado'):
        send_appeal(db, config, row, row.versao, 1, client)
    assert row.recurso_estado == 'envio_incerto'
    with pytest.raises(TissError, match='iniciada'):
        send_appeal(db, config, row, row.versao, 1, client)
    assert client.appeal.call_count == 1


def test_validation_failure_never_marks_sent(
    db, protocol, config, source, candidate
):
    import_rows(db, protocol, [source], {('123', 'D1')}, [candidate], config)
    db.commit()
    row = db.scalar(select(PetrobrasGlosa))
    row.justificativa = 'J'
    row.prazo = date.today() + timedelta(days=1)
    row.origem_prazo = 'Regra'
    row.fonte = {**source, 'tabela': 'invalid'}
    db.commit()
    client = Mock()
    with pytest.raises(TissError):
        send_appeal(db, config, row, row.versao, 1, client)
    assert row.recurso_estado == 'rascunho'
    client.appeal.assert_not_called()


def test_authorization_and_draft_conflict(
    db, protocol, config, source, candidate
):
    import_rows(db, protocol, [source], {('123', 'D1')}, [candidate], config)
    db.commit()
    row = db.scalar(select(PetrobrasGlosa))
    app = FastAPI()
    app.include_router(petrobras.router)
    user = SimpleNamespace(
        id=1, perfil='leitura', telas_permitidas=['follow_up_glosas']
    )
    app.dependency_overrides[petrobras.valida_token_usuario_atual] = lambda: (
        user
    )
    app.dependency_overrides[petrobras.get_session_postgres] = lambda: db
    app.dependency_overrides[petrobras.get_config] = lambda: config
    with TestClient(app) as client:
        assert client.get('/app_glosas/petrobras/glosas').status_code == 200
        url = f'/app_glosas/petrobras/glosas/{row.id}/rascunho'
        body = {'justificativa': 'Nova', 'versao': 1}
        assert client.put(url, json=body).status_code == 403
        user.perfil = 'usuario'
        assert client.put(url, json=body).status_code == 200
        assert client.put(url, json=body).status_code == 409
        user.telas_permitidas = []
        assert client.get('/app_glosas/petrobras/glosas').status_code == 403


def test_migration_matches_mapped_columns_and_runs_twice_independently():

    path = (
        Path(__file__).parents[2]
        / 'migrations/versions/20261029_063_petrobras_tiss.py'
    )
    spec = importlib.util.spec_from_file_location('petrobras_migration', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS api_prontocardio")
        mod.op = Operations(MigrationContext.configure(conn))
        mod.upgrade()
        inspector = inspect(conn)
        for table in table_registry.metadata.tables.values():
            if table.name.startswith('petrobras_'):
                actual = {
                    c['name']
                    for c in inspector.get_columns(
                        table.name, schema='api_prontocardio'
                    )
                }
                assert actual == {c.name for c in table.columns}
        mod.downgrade()
        assert not inspect(conn).get_table_names(schema='api_prontocardio')
        mod.upgrade()


def test_return_integrity_and_soap_faults(config):

    # A valid response with a status branch, signed using the published TISS
    # text hash.
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    h = element(root, 'cabecalho')
    trans = element(h, 'identificacaoTransacao')
    for key, val in [
        ('tipoTransacao', 'DEMONSTRATIVO_ANALISE_CONTA'),
        ('sequencialTransacao', '1'),
        ('dataRegistroTransacao', '2026-10-01'),
        ('horaRegistroTransacao', '10:00:00'),
    ]:
        element(trans, key, val)
    element(element(h, 'origem'), 'registroANS', '422631')
    element(
        element(element(h, 'destino'), 'identificacaoPrestador'),
        'CNPJ',
        '00000000000000',
    )
    element(h, 'Padrao', '4.01.00')
    info = element(
        element(root, 'demonstrativoRetorno'), 'situacaoDemonstrativoRetorno'
    )
    element(info, 'identificacaoOperadora', '422631')
    element(
        element(info, 'dadosPrestador'), 'cnpjContratado', '00000000000000'
    )
    for key, val in [
        ('numeroProtocolo', '123'),
        ('protocoloSolicitacaoDemonstrativo', '456'),
        ('tipoDemonstrativo', '1'),
        ('dataSituacaoDemonstrativo', '2026-10-01'),
        ('situacaoDemonstrativo', '1'),
    ]:
        element(info, key, val)
    element(root, 'hash', text_hash(root))
    validate(root)
    envelope = ET.Element(
        '{http://schemas.xmlsoap.org/soap/envelope/}Envelope'
    )
    ET.SubElement(
        envelope, '{http://schemas.xmlsoap.org/soap/envelope/}Body'
    ).append(root)
    raw = ET.tostring(envelope, pretty_print=True)
    opener = Mock()
    opener.open.return_value = BytesIO(raw)
    assert (
        PetrobrasClient(config, opener)
        .demonstrativos(['123'])
        .tag.endswith('demonstrativoRetornoWS')
    )
    opener.open.return_value = BytesIO(
        raw.replace(b'<ns0:numeroProtocolo>123', b'<ns0:numeroProtocolo>999')
    )
    # Use DOM to reliably tamper the value regardless of generated namespace
    # prefix.
    element_to_change = info.find('{%s}numeroProtocolo' % NS)
    element_to_change.text = '999'
    opener.open.return_value = BytesIO(ET.tostring(envelope))
    with pytest.raises(TissError, match='Integridade'):
        PetrobrasClient(config, opener).demonstrativos(['123'])
    opener.open.side_effect = HTTPError(
        'https://example.test',
        500,
        'Failure',
        {},
        BytesIO(
            b'<Envelope><faultstring>DESTINATARIO_INVALIDO</faultstring></Envelope>'
        ),
    )
    with pytest.raises(TissError, match='DESTINATARIO_INVALIDO'):
        PetrobrasClient(config, opener).demonstrativos(['123'])


def test_attachments_are_scoped_and_deduplicated(
    db, protocol, config, source, candidate
):

    import_rows(db, protocol, [source], {('123', 'D1')}, [candidate], config)
    db.commit()
    row = db.scalar(select(PetrobrasGlosa))
    app = FastAPI()
    app.include_router(petrobras.router)
    user = SimpleNamespace(
        id=1, perfil='usuario', telas_permitidas=['follow_up_glosas']
    )
    app.dependency_overrides[petrobras.valida_token_usuario_atual] = lambda: (
        user
    )
    app.dependency_overrides[petrobras.get_session_postgres] = lambda: db
    app.dependency_overrides[petrobras.get_config] = lambda: config
    url = f'/app_glosas/petrobras/glosas/{row.id}/anexos'
    body = {
        'nome': 'comprovante.pdf',
        'conteudo_base64': base64.b64encode(b'%PDF-synthetic').decode(),
        'tipo_documento': '02',
        'natureza_guia': '2',
    }
    with TestClient(app) as client:
        first = client.post(url, json=body)
        assert first.status_code == 200
        assert client.post(url, json=body).json()['id'] == first.json()['id']
        assert len(client.get(url).json()) == 1
        assert (
            client
            .get(url + '/' + str(first.json()['id']))
            .headers['content-disposition']
            .startswith('attachment;')
        )
        bad = {
            **body,
            'conteudo_base64': base64.b64encode(
                b'<script>bad</script>'
            ).decode(),
        }
        assert client.post(url, json=bad).status_code == 422
        config.prestador_cnpj = '11111111111111'
        assert client.get(url).status_code == 404


def test_competence_discovery_uses_only_payer(db, config):

    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(
        element(root, 'demonstrativoRetorno'), 'demonstrativoPagamento'
    )
    element(element(demo, 'cabecalhoDemonstrativo'), 'registroANS', '422631')
    element(
        element(element(demo, 'dadosContratado'), 'dadosPrestador'),
        'cnpjContratado',
        '00000000000000',
    )
    for number in ['123', '123', '456']:
        element(element(demo, 'relacaoProtocolos'), 'numeroProtocolo', number)
    assert payment_protocols(root, config) == ['123', '456']
    client = Mock()
    client.pagamentos.return_value = root
    client.response_warnings = ['Aviso sintético de compatibilidade']
    assert (
        discover_competencia(db, config, '202610', client=client)[
            'protocolos_localizados'
        ]
        == 2
    )
    result = discover_competencia(db, config, '202610', client=client)
    assert result['avisos'] == client.response_warnings
    assert (
        db.scalar(select(PetrobrasExecucao)).erro
        == client.response_warnings[0]
    )
    assert len(list(db.scalars(select(PetrobrasProtocolo)))) == 2
    validate(PetrobrasClient(config, dry_run=True).pagamentos('202610'))
    with pytest.raises(TissError):
        PetrobrasClient(config, dry_run=True).pagamentos('202613')


def test_wallet_mismatch_prevents_even_single_match(source, candidate):
    source['carteira'] = '00000001'
    candidate['nr_carteira'] = '00000002'
    result = reconcile(source, [candidate], 37)
    assert result['status'] == 'ambiguo'
    assert result['oracle'] is None
    assert any('carteira' in d for d in result['divergencias'])


def test_wallet_preserves_leading_zeros(source, candidate):
    source['carteira'] = '00000001'
    candidate['nr_carteira'] = '1'
    assert reconcile(source, [candidate], 37)['oracle'] is None


def test_wallet_disambiguates_beneficiaries(source, candidate):
    source['carteira'] = '00000001'
    candidate['nr_carteira'] = '00000001'
    other = {**candidate, 'cd_lancamento': 2, 'nr_carteira': '00000002'}
    assert (
        reconcile(source, [candidate, other], 37)['oracle']['cd_lancamento']
        == 1
    )


def test_missing_oracle_wallet_prevents_match(source, candidate):
    source['carteira'] = '00000001'
    candidate['nr_carteira'] = None
    result = reconcile(source, [candidate], 37)
    assert result['status'] == 'ambiguo'
    assert result['oracle'] is None
    assert any('carteira' in d for d in result['divergencias'])


def test_analysis_rejects_other_provider(config):
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(root, 'demonstrativoAnaliseConta')
    element(
        element(demo, 'cabecalhoDemonstrativo'),
        'registroANS',
        config.registro_ans,
    )
    element(
        element(element(demo, 'dadosPrestador'), 'dadosContratado'),
        'cnpjContratado',
        '11111111111111',
    )
    with pytest.raises(TissError, match='outro prestador'):
        parse_demonstrativos(root, config.prestador_cnpj, config.registro_ans)


def test_partial_appeal_response_preserves_repeated_items():
    root = ET.Element('{%s}respostaGuiaItens' % NS)
    for seq, accepted in [('1', '10.00'), ('2', '0.00')]:
        item = element(root, 'respostaItem')
        element(item, 'sequencialItem', seq)
        element(item, 'valorAcatadado', accepted)
    result = response_data(root)
    assert result['respostaItem'] == [
        {'sequencialItem': ['1'], 'valorAcatadado': ['10.00']},
        {'sequencialItem': ['2'], 'valorAcatadado': ['0.00']},
    ]


def test_provider_code_is_used_in_both_request_locations(config):
    config.codigo_prestador = 'PRESTADOR001'
    root = PetrobrasClient(config, dry_run=True).pagamentos('202609')
    assert (
        value(
            root,
            'cabecalho/origem/identificacaoPrestador/codigoPrestadorNaOperadora',
        )
        == 'PRESTADOR001'
    )
    assert (
        value(
            root,
            'solicitacaoDemonstrativoRetorno/demonstrativoPagamento/dadosPrestador/codigoPrestadorNaOperadora',
        )
        == 'PRESTADOR001'
    )


def test_echoed_header_requires_exact_provider_and_operator(config):
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    head = element(root, 'cabecalho')
    provider = element(element(head, 'origem'), 'identificacaoPrestador')
    code = element(
        provider, 'codigoPrestadorNaOperadora', config.identificador_prestador
    )
    ans = element(element(head, 'destino'), 'registroANS', config.registro_ans)
    validate_response_identity(root, config)
    code.text = 'different-provider'
    with pytest.raises(TissError, match='prestador'):
        validate_response_identity(root, config)
    code.text = config.identificador_prestador
    ans.text = '000000'
    with pytest.raises(TissError, match='operadora'):
        validate_response_identity(root, config)


def test_payment_protocols_ignore_zero_and_reject_other_provider(config):
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(root, 'demonstrativoPagamento')
    element(
        element(demo, 'cabecalhoDemonstrativo'),
        'registroANS',
        config.registro_ans,
    )
    code = element(
        element(element(demo, 'dadosContratado'), 'dadosPrestador'),
        'codigoPrestadorNaOperadora',
        config.identificador_prestador,
    )
    for number in ['0', '000', '123', '123']:
        element(element(demo, 'relacaoProtocolos'), 'numeroProtocolo', number)
    assert payment_protocols(root, config) == ['123']
    code.text = 'another-provider'
    with pytest.raises(TissError, match='prestador'):
        payment_protocols(root, config)


def test_header_compatibility_changes_only_validation_copy(
    config, monkeypatch
):
    config.prestador_cnpj = '01234567000189'
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(root, 'demonstrativoPagamento')
    cnpj = element(
        element(demo, 'cabecalhoDemonstrativo'), 'numeroCNPJ', '1234567000189'
    )
    element(
        element(element(demo, 'dadosContratado'), 'dadosPrestador'),
        'codigoPrestadorNaOperadora',
        config.identificador_prestador,
    )
    validation = Mock()
    monkeypatch.setattr(tiss, 'validate', validation)
    assert len(tiss.validate_response_schema(root, config)) == 1
    assert cnpj.text == '1234567000189'
    checked = validation.call_args.args[0]
    assert checked.find('.//{%s}numeroCNPJ' % NS).text == config.prestador_cnpj
    cnpj.text = 'invalid'
    assert not tiss.validate_response_schema(root, config)
    assert (
        validation.call_args.args[0].find('.//{%s}numeroCNPJ' % NS).text
        == 'invalid'
    )


@pytest.mark.parametrize('number', [None, '0', '000'])
def test_analysis_missing_protocol_is_explicitly_rejected(number, config):
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(root, 'demonstrativoAnaliseConta')
    header = element(demo, 'cabecalhoDemonstrativo')
    element(header, 'registroANS', config.registro_ans)
    element(header, 'numeroDemonstrativo', 'SYNTHETIC')
    protocol = element(element(demo, 'dadosConta'), 'dadosProtocolo')
    if number is not None:
        element(protocol, 'numeroProtocolo', number)
    element(protocol, 'numeroLotePrestador', '123')
    with pytest.raises(TissError, match='sem protocolo válido'):
        parse_demonstrativos(root, config.prestador_cnpj, config.registro_ans)


def test_partial_header_total_is_not_fabricated_as_item(config):
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(root, 'demonstrativoAnaliseConta')
    header = element(demo, 'cabecalhoDemonstrativo')
    element(header, 'registroANS', config.registro_ans)
    element(header, 'numeroDemonstrativo', 'SYNTHETIC')
    protocol = element(element(demo, 'dadosConta'), 'dadosProtocolo')
    element(protocol, 'numeroProtocolo', '123')
    element(protocol, 'valorGlosaProtocolo', '0.00')
    element(demo, 'valorGlosaGeral', '10.00')
    rows, scopes = parse_demonstrativos(
        root, config.prestador_cnpj, config.registro_ans
    )
    assert rows == []
    assert scopes == {('123', 'SYNTHETIC')}


def test_provider_guide_normalization_requires_exact_wallet(source, candidate):
    source['senha'] = '000123'
    candidate['nr_guia'] = '123'
    result = reconcile(source, [candidate], 37)
    assert result['status'] == 'conciliado'
    assert 'zeros' in result['divergencias'][0]
    for wallet in ['', '1', 'different']:
        source['carteira'] = wallet
        assert reconcile(source, [candidate], 37)['oracle'] is None
    source['carteira'] = '00000001'
    candidate['cd_pro_fat'] = '123456'
    assert reconcile(source, [candidate], 37)['oracle'] is None


@pytest.mark.parametrize('table', ['00', '98', None])
def test_table_does_not_block_confirmed_composite_key(
    source, candidate, table
):
    source['tabela'] = table
    source['senha'] = candidate['nr_guia']
    result = reconcile(source, [candidate], 37)
    assert result['status'] == 'conciliado'
    assert result['oracle'] == candidate


def test_partial_totals_are_retained_and_block_transmission(config):
    root = demo_xml()
    root.find('.//{%s}valorGlosaGuia' % NS).text = '1.00'
    root.find('.//{%s}valorGlosaProtocolo' % NS).text = '50.00'
    demo = root.find('.//{%s}demonstrativoAnaliseConta' % NS)
    element(demo, 'valorGlosaGeral', '100.00')
    rows, _ = parse_demonstrativos(
        root, config.prestador_cnpj, config.registro_ans
    )
    assert len(rows) == 1
    assert rows[0]['valor_glosa'] == '15.00'
    assert rows[0]['total_glosa_guia'] == '1.00'
    assert rows[0]['total_glosa_protocolo'] == '50.00'
    assert rows[0]['total_glosa_demonstrativo'] == '100.00'
    assert rows[0]['divergencia_totais']
    with pytest.raises(TissError, match='Totais divergentes'):
        PetrobrasClient(config, dry_run=True).appeal(rows[0], 'test', '1')


def test_batch_partial_response_preserves_unreturned_items(
    db, config, protocol, source, candidate
):
    import_rows(db, protocol, [source], set(), [candidate], config)
    previous = db.scalar(select(PetrobrasGlosa))
    previous.justificativa = 'Manual intacta'
    another = PetrobrasProtocolo(prestador=config.prestador_cnpj, numero='456')
    db.add(another)
    db.commit()
    client = Mock()
    client.demonstrativos.return_value = demo_xml()
    client.response_warnings = ['Aviso sintético']
    result = synchronize_many(
        db, config, [protocol.id, another.id], lambda *_: [], client=client
    )
    assert result['situacao'] == 'parcial'
    assert result['quantidade'] == 2
    assert result['protocolos_pendentes'] == 1
    assert previous.presente_na_fonte
    assert previous.justificativa == 'Manual intacta'
    assert another.ultima_sincronizacao is None
    client.demonstrativos.assert_called_once_with(['123', '456'])
    synchronize_many(
        db, config, [protocol.id, another.id], lambda *_: [], client=client
    )
    assert len(list(db.scalars(select(PetrobrasGlosa)))) == 3


def test_batch_rejects_foreign_protocol(db, config, protocol):
    root = demo_xml()
    root.find('.//{%s}numeroProtocolo' % NS).text = '999'
    client = Mock()
    client.demonstrativos.return_value = root
    with pytest.raises(TissError, match='diferente'):
        synchronize(db, config, protocol.id, lambda *_: [], client=client)
    assert not list(db.scalars(select(PetrobrasGlosa)))


def test_unknown_denial_received_does_not_weaken_send_contract(config, source):
    source['motivos'][0]['codigo'] = '3292'
    assert '3292' not in tiss.known_denial_codes()
    with pytest.raises(TissError, match='contrato'):
        PetrobrasClient(config, dry_run=True).appeal(source, 'Test', '1')
    # Build a valid outgoing structure with a known reason, then modify only
    # its denial code to exercise the independently compiled receive schema.
    source['motivos'][0]['codigo'] = '1701'
    root = PetrobrasClient(config, dry_run=True).appeal(source, 'Test', '1')
    node = root.find('.//{%s}codGlosaItem' % NS)
    node.text = '3292'
    assert tiss.receipt_schema().validate(root)
    assert not tiss.schema().validate(root)
    node.text = 'abcd'
    assert not tiss.receipt_schema().validate(root)
    node.text = '3292'
    root.find('.//{%s}valorRecursado' % NS).text = 'invalid'
    assert not tiss.receipt_schema().validate(root)


def test_competence_import_requests_discovered_protocols_only(
    db, config, monkeypatch
):
    protocol = PetrobrasProtocolo(
        prestador=config.prestador_cnpj, numero='123'
    )
    unrelated = PetrobrasProtocolo(
        prestador=config.prestador_cnpj, numero='999'
    )
    db.add_all([protocol, unrelated])
    db.commit()

    monkeypatch.setattr(
        petrobras_sync,
        'discover_competencia',
        lambda *a: {
            'numeros_protocolos': ['123'],
            'protocolos_localizados': 1,
            'avisos': [],
            'competencia': '202601',
        },
    )
    client = Mock()
    client.demonstrativos.return_value = demo_xml()
    result = synchronize_competencia(
        db, config, '202601', lambda *_: [], client=client
    )
    client.demonstrativos.assert_called_once_with(['123'])
    assert result['quantidade'] == 2
    assert result['competencia'] == '202601'
    assert 'numeros_protocolos' not in result


def test_historical_competences_january_through_october():
    assert competencias(date(2026, 10, 29), 3, '202601', '202610') == [
        f'2026{month:02}' for month in range(1, 11)
    ]
    for start, end in [
        ('202610', '202601'),
        ('202613', '202610'),
        ('202501', '202610'),
        ('202601', None),
    ]:
        with pytest.raises(ValueError, match='Informe|inválida'):
            competencias(date(2026, 10, 29), 3, start, end)


def test_analysis_identity_cannot_rely_only_on_echoed_header(
    config, monkeypatch
):
    root = demo_xml()
    demo = root.find('.//{%s}demonstrativoAnaliseConta' % NS)
    provider = element(element(demo, 'dadosPrestador'), 'dadosContratado')
    code = element(provider, 'codigoPrestadorNaOperadora', 'different')
    monkeypatch.setattr(tiss, 'validate', Mock())
    with pytest.raises(TissError, match='outro prestador'):
        tiss.validate_response_schema(root, config)
    code.text = config.identificador_prestador
    assert tiss.validate_response_schema(root, config) == []


@pytest.mark.parametrize('field', ['carteira', 'senha', 'tuss'])
def test_every_source_key_component_is_required(source, candidate, field):
    source['senha'] = candidate['nr_guia']
    source[field] = None
    result = reconcile(source, [candidate], 37)
    assert result['status'] == 'dados_insuficientes'
    assert result['oracle'] is None


@pytest.mark.parametrize('field', ['nr_carteira', 'nr_guia', 'cd_pro_fat'])
def test_each_component_must_belong_to_the_same_oracle_item(
    source, candidate, field
):
    source['senha'] = candidate['nr_guia']
    other = {**candidate, field: 'different', 'cd_lancamento': 2}
    assert reconcile(source, [other], 37)['oracle'] is None
    result = reconcile(source, [other, candidate], 37)
    assert result['oracle'] == candidate


def test_senha_without_left_padding_matches_mv_guide(source, candidate):
    source['senha'] = '0000B386348'
    candidate['nr_guia'] = 'B386348'
    result = reconcile(source, [candidate], 37)
    assert result['status'] == 'conciliado'
    assert result['oracle'] == candidate
    assert any('zeros' in d for d in result['divergencias'])


def test_procedure_matches_cd_pro_fat_not_cd_tuss(source, candidate):
    candidate['cd_tuss'] = source['tuss']
    candidate['cd_pro_fat'] = '99999999'
    result = reconcile(source, [candidate], 37)
    assert result['status'] == 'nao_localizado'
    assert result['oracle'] is None


def test_operator_guide_is_never_used_as_key(source, candidate):
    candidate['nr_guia'] = source['guia_operadora']
    assert reconcile(source, [candidate], 37)['oracle'] is None


def test_parser_keeps_senha_and_processed_value():
    root = demo_xml()
    guide = root.find('.//{%s}relacaoGuias' % NS)
    element(guide, 'senha', 'B386348')
    detail = guide.find('{%s}detalhesGuia' % NS)
    element(detail, 'valorProcessado', '298.43')
    rows, _ = parse_demonstrativos(root, '00000000000000', '422631')
    assert rows[0]['senha'] == 'B386348'
    assert rows[0]['valor_processado'] == '298.43'
    # New informative fields never change the persisted identity.
    assert (
        rows[0]['chave']
        == parse_demonstrativos(demo_xml(), '00000000000000', '422631')[0][0][
            'chave'
        ]
    )


def petrobras_client(db, config, perfil='usuario'):
    app = FastAPI()
    app.include_router(petrobras.router)
    user = SimpleNamespace(
        id=1, perfil=perfil, telas_permitidas=['follow_up_glosas']
    )
    app.dependency_overrides[petrobras.valida_token_usuario_atual] = lambda: (
        user
    )
    app.dependency_overrides[petrobras.get_session_postgres] = lambda: db
    app.dependency_overrides[petrobras.get_config] = lambda: config
    return TestClient(app)


def test_demonstrativos_page_groups_all_items_and_totals(
    db, protocol, config, source, candidate
):
    other = {
        **source,
        'chave': 'other-key',
        'demonstrativo': 'D2',
        'sequencial': '2',
        'data_emissao': '2026-10-05',
        'valor_glosa': '5.00',
    }
    source['data_emissao'] = '2026-10-01'
    import_rows(db, protocol, [source, other], set(), [candidate], config)
    db.commit()
    with petrobras_client(db, config) as client:
        data = client.get(
            '/app_glosas/petrobras/demonstrativos', params={'limit': 1}
        ).json()
        assert data['total'] == 2
        assert data['quantidade_glosas'] == 2
        assert Decimal(str(data['valor_total_glosado'])) == Decimal('25.00')
        assert Decimal(str(data['valor_total_tratado'])) == 0
        # Most recent demonstrativo first; no item from another page leaks.
        assert data['demonstrativos'] == ['D2']
        assert {i['demonstrativo'] for i in data['items']} == {'D2'}
        filtered = client.get(
            '/app_glosas/petrobras/demonstrativos',
            params={'guia': 'P0123'},
        ).json()
        assert filtered['quantidade_glosas'] == 2
        empty = client.get(
            '/app_glosas/petrobras/demonstrativos',
            params={'protocolo': 'outro'},
        ).json()
        assert empty['total'] == 0
        assert empty['items'] == []
        totals = client.get(
            '/app_glosas/petrobras/demonstrativos', params={'limit': 0}
        ).json()
        assert totals['total'] == 2
        assert totals['items'] == []


def test_match_all_rechecks_stored_sources_without_payer(  # noqa: PLR0913 - pytest fixtures
    db, protocol, config, source, candidate, monkeypatch
):
    import_rows(db, protocol, [source], set(), [], config)
    db.commit()
    row = db.scalar(select(PetrobrasGlosa))
    assert row.cruzamento == 'nao_localizado'
    monkeypatch.setattr(
        petrobras.petrobras_oracle, 'candidates', lambda s, c: [candidate]
    )
    with petrobras_client(db, config) as client:
        data = client.post('/app_glosas/petrobras/glosas/cruzar').json()
        assert data == {
            'conferidos': 1,
            'alterados': 1,
            'situacao': {'conciliado': 1},
        }
        assert (
            client.post('/app_glosas/petrobras/glosas/cruzar').json()[
                'alterados'
            ]
            == 0
        )
    db.refresh(row)
    assert row.oracle == candidate
    kinds = [
        e.tipo
        for e in db.scalars(
            select(PetrobrasEvento).where(PetrobrasEvento.glosa_id == row.id)
        )
    ]
    assert kinds.count('cruzamento_manual') == 1


def test_match_all_preserves_previous_match_when_oracle_fails(  # noqa: PLR0913 - pytest fixtures
    db, protocol, config, source, candidate, monkeypatch
):
    import_rows(db, protocol, [source], set(), [candidate], config)
    db.commit()

    def unavailable(*_):
        raise RuntimeError('offline')

    monkeypatch.setattr(petrobras.petrobras_oracle, 'candidates', unavailable)
    with petrobras_client(db, config) as client:
        assert (
            client.post('/app_glosas/petrobras/glosas/cruzar').status_code
            == 503
        )
    row = db.scalar(select(PetrobrasGlosa))
    assert row.cruzamento == 'conciliado'


def test_match_all_requires_writer(db, protocol, config):
    with petrobras_client(db, config, perfil='leitura') as client:
        assert (
            client.post('/app_glosas/petrobras/glosas/cruzar').status_code
            == 403
        )


def reissued(source, demonstrativo, emissao):
    return {
        **source,
        'chave': f'key-{demonstrativo}',
        'demonstrativo': demonstrativo,
        'data_emissao': emissao,
    }


@pytest.mark.parametrize('order', [('D1', 'D2'), ('D2', 'D1')])
def test_latest_demonstrativo_is_listed_once(  # noqa: PLR0913 - pytest fixtures
    db, protocol, config, source, candidate, order
):
    emitted = {'D1': '2026-09-01', 'D2': '2026-10-01'}
    for demo in order:
        import_rows(
            db,
            protocol,
            [reissued(source, demo, emitted[demo])],
            set(),
            [candidate],
            config,
        )
    db.commit()
    assert all(r.presente_na_fonte for r in db.scalars(select(PetrobrasGlosa)))
    with petrobras_client(db, config) as client:
        data = client.get('/app_glosas/petrobras/demonstrativos').json()
    assert data['demonstrativos'] == ['D2']
    assert data['quantidade_glosas'] == 1
    assert Decimal(str(data['valor_total_glosado'])) == Decimal('20.00')


def test_item_with_user_work_remains_listed(db, protocol, config, source):
    import_rows(
        db, protocol, [reissued(source, 'D1', '2026-09-01')], set(), [], config
    )
    db.scalar(select(PetrobrasGlosa)).justificativa = 'Recurso em elaboração'
    import_rows(
        db, protocol, [reissued(source, 'D2', '2026-10-01')], set(), [], config
    )
    db.commit()
    with petrobras_client(db, config) as client:
        data = client.get('/app_glosas/petrobras/demonstrativos').json()
    assert data['quantidade_glosas'] == 2


def test_missing_senha_uses_same_guide_senha(
    db, protocol, config, source, candidate
):
    import_rows(
        db,
        protocol,
        [reissued(source, 'D1', '2026-09-01')],
        set(),
        [candidate],
        config,
    )
    newer = {**reissued(source, 'D2', '2026-10-01'), 'senha': None}
    import_rows(db, protocol, [newer], set(), [candidate], config)
    db.commit()
    row = db.scalar(
        select(PetrobrasGlosa).where(PetrobrasGlosa.demonstrativo == 'D2')
    )
    assert row.cruzamento == 'conciliado'
    assert row.fonte.get('senha') is None
    assert any('mesma guia' in d for d in row.divergencias)


def test_guide_filter_finds_reissue_by_same_guide_senha(
    db, protocol, config, source, candidate
):
    import_rows(
        db,
        protocol,
        [reissued(source, 'D1', '2026-09-01')],
        set(),
        [candidate],
        config,
    )
    newer = {**reissued(source, 'D2', '2026-10-01'), 'senha': None}
    import_rows(db, protocol, [newer], set(), [candidate], config)
    db.commit()
    with petrobras_client(db, config) as client:
        data = client.get(
            '/app_glosas/petrobras/demonstrativos', params={'guia': 'P0123'}
        ).json()
        other = client.get(
            '/app_glosas/petrobras/demonstrativos', params={'guia': 'NADA'}
        ).json()
    assert data['demonstrativos'] == ['D2']
    assert data['items'][0]['fonte'].get('senha') is None
    assert data['items'][0]['senha_guia'] == 'P0123'
    assert other['total'] == 0


def test_guide_with_conflicting_senhas_is_not_inherited(
    db, protocol, config, source, candidate
):
    first = reissued(source, 'D1', '2026-09-01')
    second = {
        **reissued(source, 'D2', '2026-09-15'),
        'sequencial': '2',
        'senha': 'OUTRA',
    }
    import_rows(db, protocol, [first, second], set(), [candidate], config)
    newer = {**reissued(source, 'D3', '2026-10-01'), 'senha': None}
    import_rows(db, protocol, [newer], set(), [candidate], config)
    db.commit()
    row = db.scalar(
        select(PetrobrasGlosa).where(PetrobrasGlosa.demonstrativo == 'D3')
    )
    assert row.cruzamento == 'dados_insuficientes'


def test_match_all_uses_same_guide_senha(  # noqa: PLR0913 - pytest fixtures
    db, protocol, config, source, candidate, monkeypatch
):
    import_rows(
        db, protocol, [reissued(source, 'D1', '2026-09-01')], set(), [], config
    )
    newer = {**reissued(source, 'D2', '2026-10-01'), 'senha': None}
    import_rows(db, protocol, [newer], set(), [], config)
    db.commit()
    monkeypatch.setattr(
        petrobras.petrobras_oracle, 'candidates', lambda s, c: [candidate]
    )
    with petrobras_client(db, config) as client:
        data = client.post('/app_glosas/petrobras/glosas/cruzar').json()
    assert data['situacao'] == {'conciliado': 2}


def test_internal_acato_counts_as_treated_and_can_be_removed(
    db, protocol, config, source
):
    import_rows(db, protocol, [source], set(), [], config)
    db.commit()
    row = db.scalar(select(PetrobrasGlosa))
    url = f'/app_glosas/petrobras/glosas/{row.id}/acato'
    body = {'data': '2026-11-02', 'quantidade': '1', 'valor': '5.00'}
    with petrobras_client(db, config) as client:
        too_much = client.put(url, json={**body, 'valor': '999.00'})
        saved = client.put(url, json={**body, 'descricao': 'Glosa aceita'})
        listed = client.get('/app_glosas/petrobras/demonstrativos').json()
        removed = client.delete(url)
        again = client.delete(url)
        after = client.get('/app_glosas/petrobras/demonstrativos').json()
    assert too_much.status_code == 422
    assert saved.status_code == 200
    assert saved.json()['acato']['descricao'] == 'Glosa aceita'
    assert listed['items'][0]['acato']['valor'] == '5.00'
    assert Decimal(str(listed['valor_total_tratado'])) == Decimal('5.00')
    assert Decimal(str(listed['valor_total_acatado'])) == Decimal('5.00')
    assert Decimal(str(listed['valor_total_recursado'])) == 0
    assert removed.status_code == 200
    assert again.status_code == 404
    assert after['items'][0]['acato'] is None
    assert Decimal(str(after['valor_total_tratado'])) == 0
    assert row.recurso_estado == 'rascunho'


def test_acato_requires_writer_and_keeps_reissued_item(
    db, protocol, config, source
):
    import_rows(
        db, protocol, [reissued(source, 'D1', '2026-09-01')], set(), [], config
    )
    db.commit()
    first = db.scalar(select(PetrobrasGlosa))
    url = f'/app_glosas/petrobras/glosas/{first.id}/acato'
    body = {'data': '2026-11-02', 'quantidade': '1', 'valor': '1.00'}
    with petrobras_client(db, config, perfil='leitura') as client:
        assert client.put(url, json=body).status_code == 403
    with petrobras_client(db, config) as client:
        assert client.put(url, json=body).status_code == 200
    import_rows(
        db, protocol, [reissued(source, 'D2', '2026-10-01')], set(), [], config
    )
    db.commit()
    with petrobras_client(db, config) as client:
        data = client.get('/app_glosas/petrobras/demonstrativos').json()
    assert data['quantidade_glosas'] == 2


def test_list_brings_glosa_reason_description(db, protocol, config, source):
    db.execute(
        insert(Tiss).values(
            codigo_termo='1701',
            termo='COBRANÇA FORA DO PRAZO DE VALIDADE',
            fonte='TUSS 38',
            pagina_pdf=1,
        )
    )
    import_rows(db, protocol, [source], set(), [], config)
    db.commit()
    with petrobras_client(db, config) as client:
        data = client.get('/app_glosas/petrobras/demonstrativos').json()
    assert data['items'][0]['motivos'] == [
        {
            'codigo': '1701',
            'valor': '20.00',
            'descricao': 'COBRANÇA FORA DO PRAZO DE VALIDADE',
        }
    ]


def payment_xml(config, guides):
    """Payment demonstrativo: guides as (protocol, guide, senha, glosa)."""
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(
        element(root, 'demonstrativoRetorno'), 'demonstrativoPagamento'
    )
    head = element(demo, 'cabecalhoDemonstrativo')
    element(head, 'registroANS', config.registro_ans)
    element(head, 'numeroDemonstrativo', 'P1')
    element(head, 'dataEmissao', '2026-09-28')
    element(
        element(element(demo, 'dadosContratado'), 'dadosPrestador'),
        'codigoPrestadorNaOperadora',
        config.identificador_prestador,
    )
    by_date = element(element(demo, 'pagamentos'), 'pagamentosPorData')
    element(element(by_date, 'dadosPagamento'), 'dataPagamento', '2026-09-30')
    summary = element(by_date, 'dadosResumo')
    groups = {}
    for number, guide, senha, glosa in guides:
        if number not in groups:
            groups[number] = element(summary, 'relacaoProtocolos')
            element(groups[number], 'numeroProtocolo', number)
            element(groups[number], 'numeroLote', f'L{number}')
        lote = element(groups[number], 'guiasDoLote')
        element(lote, 'numeroGuiaPrestador', guide)
        element(lote, 'numeroGuiaOperadora', f'O{guide}')
        if senha:
            element(lote, 'senha', senha)
        element(lote, 'tipoPagamento', '1')
        element(lote, 'valorProcessadoGuia', '100.00')
        element(lote, 'valorLiberadoGuia', str(Decimal(100) - Decimal(glosa)))
        element(lote, 'valorGlosaGuia', glosa)
    return root


def test_payment_guides_keep_only_glosed_guides(config):
    root = payment_xml(
        config,
        [
            ('123', 'B507714', 'B507714', '81.36'),
            ('123', 'B000001', 'B000001', '0.00'),
            ('0', 'B000002', 'B000002', '9.00'),
        ],
    )
    rows = tiss.payment_guides(root, config)
    assert len(rows) == 1
    row = rows[0]
    assert {
        k: row[k]
        for k in (
            'demonstrativo',
            'data_emissao',
            'data_pagamento',
            'protocolo',
            'lote',
            'guia_prestador',
            'guia_operadora',
            'senha',
            'escopo',
            'valor_glosa',
            'valor_processado',
            'valor_liberado',
            'motivos',
        )
    } == {
        'demonstrativo': 'P1',
        'data_emissao': '2026-09-28',
        'data_pagamento': '2026-09-30',
        'protocolo': '123',
        'lote': 'L123',
        'guia_prestador': 'B507714',
        'guia_operadora': 'OB507714',
        'senha': 'B507714',
        'escopo': 'guia_pagamento',
        'valor_glosa': '81.36',
        'valor_processado': '100.00',
        'valor_liberado': '18.64',
        'motivos': [],
    }
    assert row['tuss'] is None
    assert row['avisos'] == [tiss.GUIDE_ONLY_NOTICE]
    assert len(row['chave']) == 64


def test_guide_only_glosa_never_goes_to_automatic_appeal(config):
    row = tiss.payment_guides(
        payment_xml(config, [('123', 'B507714', 'B507714', '81.36')]),
        config,
    )[0]
    with pytest.raises(TissError, match='itens discriminados'):
        PetrobrasClient(config, dry_run=True).appeal(row, 'Justificativa', 1)


def mv_row(**extra):
    return {
        'cd_convenio': 37,
        'nm_convenio': 'PETROBRAS',
        'cd_atendimento': 339183,
        'cd_paciente': 7,
        'nm_paciente': 'PACIENTE SINTETICO',
        'cd_remessa': 19197,
        'tp_atendimento': 'Externo',
        'nr_guia': 'B507714',
        'nr_carteira': '00000001',
        'cd_pro_fat': '10101012',
        'valor': '500.00',
        **extra,
    }


def test_guide_only_match_uses_senha_and_one_mv_account():
    source = {'escopo': 'guia_pagamento', 'senha': 'B507714'}
    rows = [mv_row(), mv_row(cd_pro_fat='40301630', valor='29.50')]
    match = reconcile(source, rows, 37)
    assert match['status'] == 'conciliado'
    assert match['oracle']['cd_atendimento'] == 339183
    assert match['oracle']['cd_remessa'] == 19197
    assert match['oracle']['nr_carteira'] == '00000001'
    assert match['oracle']['itens_mv'] == 2
    assert match['oracle']['valor_mv'] == '529.50'
    # Item data never chosen for a guide without items.
    assert 'cd_pro_fat' not in match['oracle']

    other_account = [mv_row(), mv_row(cd_atendimento=1)]
    assert reconcile(source, other_account, 37)['status'] == 'ambiguo'
    assert reconcile(source, [mv_row(cd_convenio=1)], 37)['status'] == (
        'nao_localizado'
    )
    assert (
        reconcile({'escopo': 'guia_pagamento'}, rows, 37)['status']
        == 'dados_insuficientes'
    )
    padded = reconcile({**source, 'senha': '00B507714'}, rows, 37)
    assert padded['status'] == 'conciliado'
    assert padded['divergencias'] == ['Senha conferida sem zeros à esquerda.']


def test_discovery_imports_guides_without_analysis(
    db, protocol, config, source, candidate
):
    # Guide X9999 of protocol 123 was detailed by the account analysis.
    import_rows(db, protocol, [source], set(), [candidate], config)
    db.commit()
    root = payment_xml(
        config,
        [
            ('123', '000X9999', 'P0123', '20.00'),
            ('123', 'B507714', 'B507714', '81.36'),
            ('456', 'B392968', 'B392968', '89.93'),
        ],
    )
    client = Mock()
    client.pagamentos.return_value = root
    client.response_warnings = []
    loaded = []

    def loader(sources, _config):
        loaded.extend(s['senha'] for s in sources)
        return [mv_row()]

    result = discover_competencia(db, config, '202609', None, client, loader)
    assert result['protocolos_localizados'] == 2
    assert result['glosas_por_guia'] == 2
    assert sorted(loaded) == ['B392968', 'B507714']
    guides = {
        g.guia_prestador: g
        for g in db.scalars(
            select(PetrobrasGlosa).where(
                PetrobrasGlosa.fonte['escopo'].as_string() == 'guia_pagamento'
            )
        )
    }
    assert set(guides) == {'B507714', 'B392968'}
    assert guides['B507714'].cruzamento == 'conciliado'
    assert guides['B507714'].oracle['cd_atendimento'] == 339183
    assert guides['B392968'].cruzamento == 'nao_localizado'
    assert Decimal(str(guides['B507714'].valor_glosa)) == Decimal('81.36')

    # Rerun is idempotent and without Oracle loader nothing is imported.
    discover_competencia(db, config, '202609', None, client, loader)
    assert (
        discover_competencia(db, config, '202609', None, client)[
            'glosas_por_guia'
        ]
        == 0
    )
    assert len(list(db.scalars(select(PetrobrasGlosa)))) == 3

    with petrobras_client(db, config) as api:
        data = api.get(
            '/app_glosas/petrobras/demonstrativos', params={'guia': 'B507714'}
        ).json()
    assert data['quantidade_glosas'] == 1
    assert data['items'][0]['fonte']['escopo'] == 'guia_pagamento'
    assert data['demonstrativos'] == ['P1']


def test_guide_detailed_later_by_analysis_is_listed_once(
    db, protocol, config, source, candidate
):
    guide = tiss.payment_guides(
        payment_xml(config, [('123', '000X9999', 'P0123', '20.00')]), config
    )
    import_rows(db, protocol, guide, set(), [candidate], config)
    db.commit()
    with petrobras_client(db, config) as api:
        before = api.get('/app_glosas/petrobras/demonstrativos').json()
        assert before['quantidade_glosas'] == 1
        import_rows(db, protocol, [source], set(), [candidate], config)
        db.commit()
        after = api.get('/app_glosas/petrobras/demonstrativos').json()
    assert after['quantidade_glosas'] == 1
    assert after['items'][0]['fonte']['escopo'] == 'item'
