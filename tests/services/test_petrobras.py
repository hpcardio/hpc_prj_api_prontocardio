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
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app_prontocardio.models import table_registry
from app_prontocardio.petrobras_models import (
    PetrobrasEvento,
    PetrobrasExecucao,
    PetrobrasGlosa,
    PetrobrasProtocolo,
)
from app_prontocardio.routers import petrobras
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


@pytest.fixture
def config():
    return PetrobrasConfig(
        enabled=True,
        transmission_enabled=True,
        login=SecretStr('synthetic'),
        password=SecretStr('synthetic-only'),
        prestador_cnpj='00000000000000',
        oracle_guia='guia_operadora',
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
        if t.name.startswith('petrobras_')
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
        'guia_prestador': 'P001',
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
        'nr_guia': '000123',
        'cd_tuss': '00123456',
        'cd_reg': 10,
        'cd_lancamento': 1,
        'data_realizacao': '2026-10-01',
        'quantidade': '1',
        'valor': '100.00',
    }


def test_exact_match_keeps_zeros(source, candidate):
    assert (
        reconcile(source, [candidate], 37, 'guia_operadora')['status']
        == 'conciliado'
    )
    bad = {**candidate, 'nr_guia': '123'}
    assert (
        reconcile(source, [bad], 37, 'guia_operadora')['status']
        == 'nao_localizado'
    )
    assert (
        reconcile(
            source, [{**candidate, 'cd_convenio': 9}], 37, 'guia_operadora'
        )['status']
        == 'nao_localizado'
    )


def test_no_assumption_about_guide_type(source, candidate):
    assert (
        reconcile(source, [candidate], 37, 'pendente')['status']
        == 'dados_insuficientes'
    )
    assert (
        reconcile(source, [candidate], 37, 'guia_prestador')['status']
        == 'nao_localizado'
    )


def test_repeated_procedure_never_selects_first(source, candidate):
    other = {**candidate, 'cd_lancamento': 2}
    assert (
        reconcile(source, [candidate, other], 37, 'guia_operadora')['status']
        == 'ambiguo'
    )
    other['data_realizacao'] = '2026-10-02'
    assert (
        reconcile(source, [candidate, other], 37, 'guia_operadora')['oracle'][
            'cd_lancamento'
        ]
        == 1
    )


def test_financial_divergence_does_not_destroy_match(source, candidate):
    r = reconcile(
        source, [{**candidate, 'valor': '110.00'}], 37, 'guia_operadora'
    )
    assert r['status'] == 'conciliado'
    assert r['divergencias']


def test_numeric_quantity_disambiguation(source, candidate):
    other = {**candidate, 'cd_lancamento': 2, 'quantidade': '2'}
    assert (
        reconcile(source, [candidate, other], 37, 'guia_operadora')['count']
        == 1
    )


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
    assert [r['escopo'] for r in rows] == ['item', 'guia', 'protocolo']
    assert sum(Decimal(r['valor_glosa']) for r in rows) == Decimal('23')
    assert len(rows[0]['motivos']) == 2
    assert len({r['chave'] for r in rows}) == 3
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
    result = reconcile(source, [candidate], 37, 'guia_operadora')
    assert result['status'] == 'ambiguo'
    assert result['oracle'] is None
    assert any('carteira' in d for d in result['divergencias'])


def test_wallet_preserves_leading_zeros(source, candidate):
    source['carteira'] = '00000001'
    candidate['nr_carteira'] = '1'
    assert (
        reconcile(source, [candidate], 37, 'guia_operadora')['oracle'] is None
    )


def test_wallet_disambiguates_beneficiaries(source, candidate):
    source['carteira'] = '00000001'
    candidate['nr_carteira'] = '00000001'
    other = {**candidate, 'cd_lancamento': 2, 'nr_carteira': '00000002'}
    assert (
        reconcile(source, [candidate, other], 37, 'guia_operadora')['oracle'][
            'cd_lancamento'
        ]
        == 1
    )


def test_missing_wallet_is_visible(source, candidate):
    source['carteira'] = '00000001'
    candidate['nr_carteira'] = None
    result = reconcile(source, [candidate], 37, 'guia_operadora')
    assert result['status'] == 'conciliado'
    assert any('Carteira não informada' in d for d in result['divergencias'])


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


def test_analysis_general_total_cannot_disappear(config):
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    demo = element(root, 'demonstrativoAnaliseConta')
    header = element(demo, 'cabecalhoDemonstrativo')
    element(header, 'registroANS', config.registro_ans)
    element(header, 'numeroDemonstrativo', 'SYNTHETIC')
    protocol = element(element(demo, 'dadosConta'), 'dadosProtocolo')
    element(protocol, 'numeroProtocolo', '123')
    element(protocol, 'valorGlosaProtocolo', '0.00')
    element(demo, 'valorGlosaGeral', '10.00')
    with pytest.raises(TissError, match='Total geral'):
        parse_demonstrativos(root, config.prestador_cnpj, config.registro_ans)
