# ruff: noqa: PLR2004
# Literal expected values make these protocol fixtures explicit.
import base64
import importlib.util
import json
import re
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
    PetrobrasAnexo,
    PetrobrasEvento,
    PetrobrasExecucao,
    PetrobrasGlosa,
    PetrobrasProtocolo,
)
from app_prontocardio.routers import petrobras
from app_prontocardio.services import petrobras_appeals, petrobras_sync
from app_prontocardio.services import petrobras_tiss as tiss
from app_prontocardio.services.petrobras_appeals import (
    appeal_codes,
    response_data,
    send_appeal,
    table38_codes,
)
from app_prontocardio.services.petrobras_config import PetrobrasConfig
from app_prontocardio.services.petrobras_match import reconcile
from app_prontocardio.services.petrobras_portal_appeal import (
    PortalAppeal,
    error_text,
    lot_forms,
    multipart,
)
from app_prontocardio.services.petrobras_sync import (
    apply_payer_lots,
    discover_competencia,
    event,
    import_rows,
    known_payer_lots,
    rows_with_payer_lots,
    synchronize,
    synchronize_competencia,
    synchronize_many,
)
from app_prontocardio.services.petrobras_tiss import (
    NS,
    PetrobrasClient,
    TissError,
    TissRejected,
    appeal_block,
    element,
    parse_demonstrativos,
    parse_xml,
    payment_lots,
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
        'lote_prestador': '1',
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
    # Same MV account: patient/attendance kept, the launch stays undecided.
    account = {
        'cd_atendimento': 337959,
        'nm_paciente': 'PACIENTE',
        'cd_remessa': 9,
    }
    result = reconcile(
        source, [{**candidate, **account}, {**other, **account}], 37
    )
    assert result['status'] == 'ambiguo'
    assert result['oracle']['cd_atendimento'] == 337959
    assert result['oracle']['nm_paciente'] == 'PACIENTE'
    assert 'cd_lancamento' not in result['oracle']
    split = reconcile(
        source,
        [{**candidate, **account}, {**other, **account, 'cd_atendimento': 1}],
        37,
    )
    assert split['status'] == 'ambiguo'
    assert split['oracle'] is None
    assert reconcile(source, [candidate, other], 37)['oracle'] is None
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
    document = client.document(
        source,
        SimpleNamespace(
            id=1,
            natureza_guia='2',
            formato='02',
            conteudo=b'%PDF-test',
            tipo_documento='02',
        ),
        ('12345678901234567890', 'RG1'),
    )
    validate(document)
    # Proof of the appeal: cites the appeal guide/protocol, nature 3.
    assert value(document, 'envioDOcumento/numeroGuiaPrestador') == (
        '12345678901234567890'
    )
    assert value(document, 'envioDOcumento/numeroProtocolo') == 'RG1'
    assert value(document, 'envioDOcumento/naturezaGuia') == '3'
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
    client.appeal_protocol.side_effect = TimeoutError()
    with pytest.raises(TissError, match='não confirmado'):
        send_appeal(db, config, row, row.versao, 1, client)
    assert row.recurso_estado == 'envio_incerto'
    with pytest.raises(TissError, match='iniciada'):
        send_appeal(db, config, row, row.versao, 1, client)
    assert client.appeal_protocol.call_count == 1


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
    client.appeal_protocol.assert_not_called()


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
    with pytest.raises(TissError, match='3292 fora da Tabela 38'):
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
    # MV items listed as reference, with no glosa value of their own.
    assert [i['cd_pro_fat'] for i in match['oracle']['itens']] == [
        '10101012',
        '40301630',
    ]
    assert match['oracle']['itens'][1]['valor'] == '29.50'
    assert 'valor_glosa' not in match['oracle']['itens'][0]

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


def test_pilot_allows_only_listed_users(config):
    config.transmission_users = (
        ' Leticia.Oliveira@hospitalprontocardio.com.br ,'
    )
    assert config.can_transmit('leticia.oliveira@hospitalprontocardio.com.br')
    assert not config.can_transmit('outro@hospitalprontocardio.com.br')
    assert not config.can_transmit(None)
    config.transmission_users = ''
    assert config.can_transmit('outro@hospitalprontocardio.com.br')
    config.transmission_enabled = False
    assert not config.can_transmit(
        'leticia.oliveira@hospitalprontocardio.com.br'
    )


def test_operator_codes_outside_published_domain_are_blocked(config, source):
    source = {**source, 'motivos': [{'codigo': '3209', 'valor': '74.00'}]}
    message = tiss.appeal_block(source)
    assert '3209' in message
    assert 'portal' in message
    with pytest.raises(TissError, match='3209'):
        PetrobrasClient(config, dry_run=True).appeal(source, 'J', 'R1')
    assert (
        tiss.appeal_block({**source, 'motivos': [{'codigo': '1705'}]}) is None
    )


def test_send_endpoint_respects_pilot_and_exposes_block(
    db, protocol, config, source, candidate
):
    other = {
        **source,
        'chave': 'blocked-key',
        'sequencial': '2',
        'motivos': [{'codigo': '3253', 'valor': '6.61'}],
    }
    import_rows(db, protocol, [source, other], set(), [candidate], config)
    db.commit()
    config.transmission_users = 'piloto@hospitalprontocardio.com.br'
    user = SimpleNamespace(
        id=1,
        perfil='usuario',
        telas_permitidas=['follow_up_glosas'],
        email='outro@hospitalprontocardio.com.br',
    )
    app = FastAPI()
    app.include_router(petrobras.router)
    app.dependency_overrides[petrobras.valida_token_usuario_atual] = lambda: (
        user
    )
    app.dependency_overrides[petrobras.get_session_postgres] = lambda: db
    app.dependency_overrides[petrobras.get_config] = lambda: config
    rows = {
        r.fonte['sequencial']: r for r in db.scalars(select(PetrobrasGlosa))
    }
    with TestClient(app) as client:
        assert (
            client.get('/app_glosas/petrobras/status').json()[
                'transmissao_usuario'
            ]
            is False
        )
        url = f'/app_glosas/petrobras/glosas/{rows["1"].id}/enviar'
        body = {'versao': rows['1'].versao, 'confirmar_envio': True}
        denied = client.post(url, json=body)
        assert denied.status_code == 403
        assert 'piloto' in denied.json()['detail']
        assert rows['1'].recurso_estado == 'rascunho'
        user.email = 'Piloto@hospitalprontocardio.com.br'
        assert (
            client.get('/app_glosas/petrobras/status').json()[
                'transmissao_usuario'
            ]
            is True
        )
        items = {
            i['fonte']['sequencial']: i
            for i in client.get('/app_glosas/petrobras/glosas').json()['items']
        }
    assert items['1']['bloqueio_envio'] is None
    assert '3253' in items['2']['bloqueio_envio']


def test_table38_codes_relax_only_the_denial_code(config, source):
    source = {**source, 'motivos': [{'codigo': '3209', 'valor': '74.00'}]}
    assert tiss.operator_codes(source) == ['3209']
    with pytest.raises(TissError, match='3209 fora da Tabela 38'):
        PetrobrasClient(config, dry_run=True).appeal(source, 'J', 'R1')
    assert tiss.appeal_block(source, frozenset({'3209'})) is None
    root = PetrobrasClient(config, dry_run=True).appeal(
        source, 'J', 'R1', frozenset({'3209'})
    )
    assert root.find('.//{%s}codGlosaItem' % NS).text == '3209'
    # A code missing from the table stays blocked.
    assert '3253' in tiss.appeal_block(
        {**source, 'motivos': [{'codigo': '3253'}]}, frozenset({'3209'})
    )
    # Any other contract violation still fails with the relaxed schema.
    with pytest.raises(TissError, match='contrato'):
        PetrobrasClient(config, dry_run=True).appeal(
            {**source, 'tabela': 'invalid'}, 'J', 'R1', frozenset({'3209'})
        )


def table38(db):
    today = date.today()
    for code, start, end, fonte in [
        ('3209', today - timedelta(days=30), None, 'Tabela 38, versão 202607'),
        (
            '3253',
            today - timedelta(days=60),
            today - timedelta(days=1),
            'Tabela 38, versão 202607',
        ),
        ('3210', today + timedelta(days=5), None, 'Tabela 38, versão 202607'),
        ('3292', today - timedelta(days=30), None, 'Outra tabela'),
    ]:
        db.execute(
            insert(Tiss).values(
                codigo_termo=code,
                termo=f'Termo {code}',
                dt_inicio_vigencia=start,
                dt_fim_vigencia=end,
                fonte=fonte,
                pagina_pdf=0,
            )
        )
    db.commit()


def test_table38_codes_use_vigencia_and_pilot_flag(db, config):
    table38(db)
    codes = {'3209', '3253', '3210', '3292', '1705'}
    assert table38_codes(db, codes) == {'3209'}
    assert appeal_codes(db, config, [{'motivos': [{'codigo': '3209'}]}]) == (
        frozenset()
    )
    config.appeal_table_codes = True
    assert appeal_codes(
        db, config, [{'motivos': [{'codigo': '3209'}, {'codigo': '1705'}]}]
    ) == {'3209'}


def test_send_and_listing_accept_table38_reason(  # noqa: PLR0913 - pytest fixtures
    db, protocol, config, source, candidate
):
    table38(db)
    config.appeal_table_codes = True
    accepted = {**source, 'motivos': [{'codigo': '3209', 'valor': '20.00'}]}
    expired = {
        **source,
        'chave': 'expired-key',
        'sequencial': '2',
        'motivos': [{'codigo': '3253', 'valor': '6.61'}],
    }
    import_rows(db, protocol, [accepted, expired], set(), [candidate], config)
    db.commit()
    with petrobras_client(db, config) as client:
        items = {
            i['fonte']['sequencial']: i
            for i in client.get('/app_glosas/petrobras/glosas').json()['items']
        }
    assert items['1']['bloqueio_envio'] is None
    assert '3253' in items['2']['bloqueio_envio']
    row = db.get(PetrobrasGlosa, items['1']['id'])
    row.justificativa = 'Justificativa'
    row.prazo = date.today() + timedelta(days=30)
    row.origem_prazo = 'Regra comprovada'
    db.commit()
    client = Mock()
    client.appeal_protocol.side_effect = TimeoutError()
    with pytest.raises(TissError, match='não confirmado'):
        send_appeal(db, config, row, row.versao, 1, client)
    assert client.appeal_protocol.call_args.args[2] == {'3209'}


def receipt(protocolo, numero='RG1'):
    root = ET.Element('{%s}protocoloRecebimentoRecursoWS' % NS)
    node = element(element(root, 'recebimentoRecurso'), 'protocoloRecebimento')
    element(node, 'nrProtocoloRecursoGlosa', numero)
    element(node, 'nrProtocoloRecursado', protocolo)
    return root


def ready_rows(db, protocol, config, source, candidate):
    other = {**source, 'chave': 'k2', 'sequencial': '2'}
    blocked = {
        **source,
        'chave': 'k3',
        'sequencial': '3',
        'motivos': [{'codigo': '3253', 'valor': '1.00'}],
    }
    import_rows(
        db, protocol, [source, other, blocked], set(), [candidate], config
    )
    db.commit()
    rows = {
        r.fonte['sequencial']: r for r in db.scalars(select(PetrobrasGlosa))
    }
    for row in rows.values():
        row.justificativa = 'Justificativa'
        row.prazo = date.today() + timedelta(days=10)
        row.origem_prazo = 'Contrato'
    rows['2'].prazo = None
    db.commit()
    return rows


def pilot_client(db, config, email='piloto@hospitalprontocardio.com.br'):
    config.transmission_users = 'piloto@hospitalprontocardio.com.br'
    app = FastAPI()
    app.include_router(petrobras.router)
    user = SimpleNamespace(
        id=1,
        perfil='usuario',
        telas_permitidas=['follow_up_glosas'],
        email=email,
    )
    app.dependency_overrides[petrobras.valida_token_usuario_atual] = lambda: (
        user
    )
    app.dependency_overrides[petrobras.get_session_postgres] = lambda: db
    app.dependency_overrides[petrobras.get_config] = lambda: config
    return TestClient(app)


def test_ready_appeals_lists_sendable_drafts_and_pending_reasons(  # noqa: PLR0913 - pytest fixtures
    db, protocol, config, source, candidate
):
    rows = ready_rows(db, protocol, config, source, candidate)
    with pilot_client(db, config) as client:
        data = client.get('/app_glosas/petrobras/glosas/prontos').json()
    assert [r['id'] for r in data['prontos']] == [rows['1'].id]
    pending = {r['id']: r['pendencia'] for r in data['pendencias']}
    assert 'prazo' in pending[rows['2'].id]
    assert '3253' in pending[rows['3'].id]
    assert data['usuario_liberado'] is True


def test_batch_send_transmits_each_and_reports_failures(  # noqa: PLR0913 - pytest fixtures
    db, protocol, config, source, candidate, monkeypatch
):
    rows = ready_rows(db, protocol, config, source, candidate)
    real = petrobras_appeals.PetrobrasClient
    sent = []

    def factory(cfg, dry_run=False):
        if dry_run:
            return real(cfg, dry_run=True)
        fake = Mock()
        fake.appeal_protocol.side_effect = lambda entries, *a: (
            sent.extend(src['sequencial'] for src, _ in entries)
            or receipt(entries[0][0]['protocolo'])
        )
        return fake

    monkeypatch.setattr(petrobras_appeals, 'PetrobrasClient', factory)
    body = {
        'itens': [{'id': r.id, 'versao': r.versao} for r in rows.values()],
        'confirmar_envio': True,
    }
    with pilot_client(db, config, 'outro@x.com') as client:
        assert (
            client.post(
                '/app_glosas/petrobras/glosas/enviar-lote', json=body
            ).status_code
            == 403
        )
    with pilot_client(db, config) as client:
        data = client.post(
            '/app_glosas/petrobras/glosas/enviar-lote', json=body
        ).json()
    result = {r['id']: r for r in data['resultados']}
    assert data['protocolados'] == 1
    assert sent == ['1']
    assert result[rows['1'].id]['protocolo'] == 'RG1'
    assert 'prazo' in result[rows['2'].id]['erro']
    assert '3253' in result[rows['3'].id]['erro']
    assert result[rows['2'].id]['situacao'] == 'rascunho'


def test_uncertain_send_records_safe_cause(
    db, protocol, config, source, candidate
):
    rows = ready_rows(db, protocol, config, source, candidate)
    client = Mock()
    client.appeal_protocol.side_effect = TissError(
        'Operadora recusou a consulta/transação (5001 - X); verificar.'
    )
    with pytest.raises(TissError, match='Motivo: Operadora recusou'):
        send_appeal(db, config, rows['1'], rows['1'].versao, 1, client)
    data = db.scalars(
        select(PetrobrasEvento.dados).where(
            PetrobrasEvento.tipo == 'envio_incerto'
        )
    ).one()
    assert '5001 - X' in data['causa']


def test_soap_fault_reason_is_kept_without_credentials(config):
    opener = Mock()
    opener.open.side_effect = HTTPError(
        'https://example.test',
        500,
        'Failure',
        {},
        BytesIO(
            b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            b'envelope/"><soap:Body><soap:Fault><faultcode>soap:Server'
            b'</faultcode><faultstring>Guia synthetic invalida</faultstring>'
            b'<detail>numeroGuiaRecGlosaPrestador\n deve ser numerico'
            b'</detail></soap:Fault></soap:Body></soap:Envelope>'
        ),
    )
    with pytest.raises(TissRejected) as error:
        PetrobrasClient(config, opener).demonstrativos(['123'])
    message = str(error.value)
    assert 'HTTP 500' in message
    assert 'Guia *** invalida' in message
    assert 'numeroGuiaRecGlosaPrestador deve ser numerico' in message
    assert 'synthetic' not in message


def test_lost_connection_fault_is_not_a_refusal(
    db, protocol, config, source, candidate
):
    rows = ready_rows(db, protocol, config, source, candidate)
    row = rows['1']
    opener = Mock()
    opener.open.side_effect = HTTPError(
        'https://example.test',
        500,
        'Failure',
        {},
        BytesIO(
            b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/'
            b'envelope/"><soap:Body><soap:Fault><faultcode>SOAP-ENV:Client'
            b'</faultcode><faultstring>org.springframework.dao.'
            b'RecoverableDataAccessException: Error updating database. Cause: '
            b'java.sql.SQLRecoverableException: No more data to read from '
            b'socket</faultstring></soap:Fault></soap:Body></soap:Envelope>'
        ),
    )
    with pytest.raises(TissError, match='não confirmado.*perdeu a conexão'):
        send_appeal(
            db, config, row, row.versao, 1, PetrobrasClient(config, opener)
        )
    assert row.recurso_estado == 'envio_incerto'
    assert row.recurso_guia
    with pytest.raises(TissError, match='iniciada'):
        send_appeal(
            db, config, row, row.versao, 1, PetrobrasClient(config, opener)
        )
    assert opener.open.call_count == 1


def test_refused_appeal_returns_to_draft(
    db, protocol, config, source, candidate
):
    rows = ready_rows(db, protocol, config, source, candidate)
    row = rows['1']
    client = Mock()
    client.appeal_protocol.side_effect = TissRejected(
        'Petrobras recusou a mensagem (HTTP 500): campo invalido.'
    )
    with pytest.raises(TissError, match='não aceito.*campo invalido'):
        send_appeal(db, config, row, row.versao, 1, client)
    sent_id = client.appeal_protocol.call_args.args[1]
    assert sent_id.isdigit()
    assert len(sent_id) == 20
    assert row.recurso_estado == 'rascunho'
    assert row.recurso_guia is None
    data = db.scalars(
        select(PetrobrasEvento.dados).where(
            PetrobrasEvento.tipo == 'recusado_operadora'
        )
    ).one()
    assert 'campo invalido' in data['causa']
    assert data['guia_recurso'] == sent_id
    client.appeal_protocol.side_effect = TimeoutError()
    with pytest.raises(TissError, match='não confirmado'):
        send_appeal(db, config, row, row.versao, 1, client)
    assert client.appeal_protocol.call_count == 2


def test_business_refusal_is_a_rejection(config):
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    error = element(root, 'mensagemErro')
    element(error, 'codigoGlosa', '1307')
    element(error, 'descricaoGlosa', 'Guia não encontrada')
    envelope = ET.Element(
        '{http://schemas.xmlsoap.org/soap/envelope/}Envelope'
    )
    ET.SubElement(
        envelope, '{http://schemas.xmlsoap.org/soap/envelope/}Body'
    ).append(root)
    opener = Mock()
    opener.open.return_value = BytesIO(ET.tostring(envelope))
    with pytest.raises(TissRejected, match='Guia não encontrada'):
        PetrobrasClient(config, opener).demonstrativos(['123'])


def test_proof_is_sent_after_the_appeal_and_never_undoes_it(
    db, protocol, config, source, candidate
):
    rows = ready_rows(db, protocol, config, source, candidate)
    row = rows['1']
    proof = PetrobrasAnexo(
        glosa_id=row.id,
        nome='formulario.pdf',
        mime='application/pdf',
        conteudo=b'%PDF-test',
        sha256='a' * 64,
        tipo_documento='01',
        natureza_guia='2',
        formato='02',
    )
    db.add(proof)
    db.commit()
    calls = []

    def appeal(entries, appeal_id, codes):
        calls.append('recurso')
        answer = ET.Element('{%s}protocoloRecebimentoRecursoWS' % NS)
        node = element(
            element(answer, 'recebimentoRecurso'), 'protocoloRecebimento'
        )
        element(node, 'nrProtocoloRecursoGlosa', 'RG9')
        element(node, 'nrProtocoloRecursado', entries[0][0]['protocolo'])
        return answer

    def document(source, attachment, cited):
        calls.append(('documento', cited))
        raise TissRejected('Petrobras recusou: documento invalido.')

    client = Mock()
    client.appeal_protocol.side_effect = appeal
    client.document.side_effect = document
    send_appeal(db, config, row, row.versao, 1, client)
    assert calls == ['recurso', ('documento', (row.recurso_guia, 'RG9'))]
    assert row.recurso_estado == 'protocolado'
    assert proof.estado == 'rascunho'
    data = db.scalars(
        select(PetrobrasEvento.dados).where(
            PetrobrasEvento.tipo == 'documento_nao_enviado'
        )
    ).one()
    assert 'documento invalido' in data['causa']


def test_protocol_appeal_groups_guides_and_items(config, source):
    second_item = {
        **source,
        'sequencial': '2',
        'valor_glosa': '5.00',
        'motivos': [{'codigo': '1705', 'valor': '5.00'}],
    }
    other_guide = {
        **source,
        'guia_prestador': '000000000000000X8888',
        'guia_operadora': '000124',
        'sequencial': '1',
    }
    root = PetrobrasClient(config, dry_run=True).appeal_protocol(
        [(source, 'J1'), (second_item, 'J2'), (other_guide, 'J3')], 'R1'
    )
    guide = root.find('.//{%s}guiaRecursoGlosa' % NS)
    assert len(guide.findall('.//{%s}guiaRecursoGlosa' % NS)) == 0
    origins = guide.findall('{%s}opcaoRecurso/{%s}recursoGuia' % (NS, NS))
    assert [value(o, 'senha') for o in origins] == ['P0123', 'P0123']
    # The guide as the operator returned it (padded), not the unpadded one.
    assert [value(o, 'numeroGuiaOrigem') for o in origins] == [
        '000000000000000X9999',
        '000000000000000X8888',
    ]
    assert len(origins[0].findall('.//{%s}itensGuia' % NS)) == 2
    assert value(guide, 'valorTotalRecursado') == '45.00'
    assert value(guide, 'numeroProtocolo') == '123'
    with pytest.raises(TissError, match='único lote/protocolo'):
        PetrobrasClient(config, dry_run=True).appeal_protocol(
            [(source, 'J'), ({**source, 'protocolo': '456'}, 'J')], 'R2'
        )


def test_batch_sends_one_appeal_per_protocol(  # noqa: PLR0913 - pytest fixtures
    db, protocol, config, source, candidate, monkeypatch
):
    same = {**source, 'chave': 'k2', 'sequencial': '2'}
    other = {
        **source,
        'chave': 'k3',
        'sequencial': '1',
        'protocolo': '456',
        'lote': '9',
    }
    second = PetrobrasProtocolo(prestador=config.prestador_cnpj, numero='456')
    db.add(second)
    db.commit()
    import_rows(db, protocol, [source, same], set(), [candidate], config)
    import_rows(db, second, [other], set(), [candidate], config)
    db.commit()
    rows = list(db.scalars(select(PetrobrasGlosa).order_by(PetrobrasGlosa.id)))
    for row in rows:
        row.justificativa = 'J'
        row.prazo = date.today() + timedelta(days=5)
        row.origem_prazo = 'Contrato'
    db.commit()
    calls = []

    def factory(cfg, dry_run=False):
        if dry_run:
            return PetrobrasClient(cfg, dry_run=True)
        fake = Mock()

        def answer(entries, *a):
            calls.append(sorted(s['sequencial'] for s, _ in entries))
            return receipt(entries[0][0]['protocolo'], f'RG{len(calls)}')

        fake.appeal_protocol.side_effect = answer
        return fake

    monkeypatch.setattr(petrobras_appeals, 'PetrobrasClient', factory)
    body = {
        'itens': [{'id': r.id, 'versao': r.versao} for r in rows],
        'confirmar_envio': True,
    }
    with pilot_client(db, config) as client:
        data = client.post(
            '/app_glosas/petrobras/glosas/enviar-lote', json=body
        ).json()
    assert len(calls) == 2
    assert sorted(calls) == [['1'], ['1', '2']]
    appeals = {a['protocolo_petrobras']: a for a in data['recursos']}
    assert len(appeals['123']['itens']) == 2
    assert appeals['123']['protocolo'] != appeals['456']['protocolo']
    assert data['protocolados'] == 3
    db.expire_all()
    guides = {r.fonte['protocolo']: set() for r in rows}
    for r in db.scalars(select(PetrobrasGlosa)):
        guides[r.fonte['protocolo']].add(r.recurso_guia)
    assert all(len(v) == 1 for v in guides.values())


class FakePortal:
    """Screen state: denied items of one lot, no network."""

    def __init__(self, rows, account_extra=None):
        self.lot = {'numeroLote': '77', 'numeroCapaLote': '77'}
        self.denied = [
            {
                'numeroItem': row.id,
                'numeroConta': 1,
                'numeroCapaLote': 77,
                'numeroContaPrestador': row.guia_prestador,
                'codigoItem': row.fonte['tuss'],
                'valorGlosado': float(row.fonte['valor_glosa']),
                'flagDenyItem': 'S',
            }
            for row in rows
        ]
        self.account = {
            'numeroConta': 1,
            'numeroCapaLote': 77,
            'numeroCapaLotePrestador': 36022538,
            'numeroContaPrestador': rows[0].fonte['guia_prestador'],
            'numeroContaOperadora': rows[0].fonte['guia_operadora'],
            **(account_extra or {}),
        }
        self.saved, self.efetivados, self.closed = [], 0, False
        self.accepted = []
        self.efetivar_error = None

    def lots(self, _demonstrativo):
        yield self.lot

    def accounts(self, _lot):
        return [self.account]

    def items(self, _lot, _account):
        return [dict(i) for i in self.denied]

    def save(self, _lot, item, justification, attachments):
        self.saved.append((item['numeroItem'], justification, attachments))
        for row in self.denied:
            if row['numeroItem'] == item['numeroItem']:
                row['situacaoRecursadoAceito'] = 'R'

    def accept(self, _lot, item):
        assert self.efetivados == 0, 'aceite vem antes de efetivar'
        self.accepted.append(item['numeroItem'])
        for row in self.denied:
            if row['numeroItem'] == item['numeroItem']:
                row['situacaoRecursadoAceito'] = 'A'

    def efetivar(self, _lot):
        if self.efetivar_error:
            raise self.efetivar_error
        self.efetivados += 1
        self.account['quantidadeItensRecursado'] = 1
        for row in self.denied:
            if row.get('situacaoRecursadoAceito') == 'R':
                row['protocoloRecurso'] = 162723152
        return {'mensagemRetorno': 'ok'}

    def close(self):
        self.closed = True


def portal_rows(db, protocol, config, source, candidate):
    rows = ready_rows(db, protocol, config, source, candidate)
    for sequence, row in rows.items():
        row.justificativa = 'Justificativa com mais de vinte caracteres'
        row.tuss = f'1000000{sequence}'
        row.fonte = {**row.fonte, 'tuss': row.tuss}
    config.portal_fallback = True
    db.commit()
    return rows


def run_portal(db, config, row, portal):
    return petrobras_appeals.appeal_by_portal(
        db, config, [row.id], TissRejected('ORA-01403'), 'G1', 1, portal
    )


def acato_row(db, row):
    event(db, row, 'acato', {'valor': str(row.valor_glosa)}, 1)
    db.commit()


def test_webservice_failure_uses_portal_only_when_enabled(  # noqa: PLR0913, PLR0917 - fixtures
    db, protocol, config, source, candidate, monkeypatch
):
    rows = portal_rows(db, protocol, config, source, candidate)
    row = rows['1']
    client = Mock()
    client.appeal_protocol.side_effect = TissRejected('ORA-01403')
    calls = []
    monkeypatch.setattr(
        petrobras_appeals,
        'appeal_by_portal',
        lambda *args, **_kw: calls.append(args[2]) or [row],
    )
    send_appeal(db, config, row, row.versao, 1, client)
    assert calls == [[row.id]]
    config.portal_fallback = False
    row.recurso_estado = 'rascunho'
    db.commit()
    with pytest.raises(TissError, match='não aceito'):
        send_appeal(db, config, row, row.versao, 1, client)
    assert calls == [[row.id]]


def test_portal_efetiva_only_when_every_denied_item_is_treated(
    db, protocol, config, source, candidate
):
    rows = portal_rows(db, protocol, config, source, candidate)
    target, other = rows['1'], rows['2']
    portal = FakePortal([target, other])
    run_portal(db, config, target, portal)
    assert [s[0] for s in portal.saved] == [target.id]
    assert portal.efetivados == 0
    assert target.recurso_estado == 'salvo_portal'
    assert portal.closed
    with pytest.raises(TissError, match=r'1 item\(ns\).*seriam aceitos'):
        petrobras_appeals.efetivar_portal(db, config, target, 1, portal)
    assert portal.efetivados == 0
    acato_row(db, other)
    petrobras_appeals.efetivar_portal(db, config, target, 1, portal)
    assert portal.efetivados == 1
    assert target.recurso_estado == 'enviado_portal'


def test_portal_sends_at_once_when_the_rest_of_the_lot_is_accepted(
    db, protocol, config, source, candidate
):
    rows = portal_rows(db, protocol, config, source, candidate)
    target, other = rows['1'], rows['2']
    acato_row(db, other)
    proof = PetrobrasAnexo(
        glosa_id=target.id,
        nome='formulario.pdf',
        mime='application/pdf',
        conteudo=b'%PDF-test',
        sha256='b' * 64,
        tipo_documento='01',
        natureza_guia='3',
        formato='02',
    )
    db.add(proof)
    db.commit()
    portal = FakePortal([target, other])
    run_portal(db, config, target, portal)
    assert portal.efetivados == 1
    assert target.recurso_estado == 'enviado_portal'
    assert portal.saved[0][2][0].nome == 'formulario.pdf'
    assert proof.estado == 'protocolado'
    assert portal.accepted == [other.id]
    assert target.recurso_protocolo == '162723152'
    assert other.recurso_estado == 'rascunho'


def test_portal_does_not_accept_while_an_item_is_untreated(
    db, protocol, config, source, candidate
):
    rows = portal_rows(db, protocol, config, source, candidate)
    target = rows['1']
    portal = FakePortal([target, rows['2']])
    run_portal(db, config, target, portal)
    assert portal.accepted == []
    assert portal.efetivados == 0


def test_portal_keeps_the_send_when_the_protocol_is_not_listed(
    db, protocol, config, source, candidate
):
    rows = portal_rows(db, protocol, config, source, candidate)
    target, other = rows['1'], rows['2']
    acato_row(db, other)
    portal = FakePortal([target, other])
    original = portal.efetivar

    def without_protocol(lot):
        answer = original(lot)
        for row in portal.denied:
            row.pop('protocoloRecurso', None)
        return answer

    portal.efetivar = without_protocol
    run_portal(db, config, target, portal)
    assert target.recurso_estado == 'enviado_portal'
    assert target.recurso_protocolo is None


class CapturingAppeal(PortalAppeal):
    def __init__(self):
        self.calls = []

    def _rest(self, path, query=None, data=None, content_type=None):
        self.calls.append((path, data, content_type))
        return b'{}'


def posted(call):
    """Fields of a captured multipart body, as the server would split them."""
    _, data, content_type = call
    boundary = content_type.split('boundary=')[1].encode()
    fields = {}
    for part in data.split(b'--' + boundary):
        head, _, value = part.partition(b'\r\n\r\n')
        name = re.search(rb'name="([^"]+)"', head)
        if name:
            fields[name.group(1).decode()] = (head, value[:-2])
    return fields


PORTAL_ITEM = {
    'numeroItem': 731101237,
    'numeroConta': 1,
    'codigoItem': '60000384',
    'valorGlosado': 40.19,
    'valorAcerto': None,
    'flagDenyItem': 'S',
    'dataRealizacao': 1755561600000,
    'situacaoRecursadoAceito': None,
    'listaDocumentoAnexo': [],
    'stringArrayDetalhesRecursoGlosa': None,
    'justificativaRecurso': None,
}
PORTAL_LOT = {'numeroLote': '12241103', 'codigoUsuario': 'U'}


def test_portal_save_posts_what_the_screen_posts():
    client = CapturingAppeal()
    proof = SimpleNamespace(
        nome='formulario.pdf', mime='application/pdf', conteudo=b'%PDF'
    )
    client.save(PORTAL_LOT, PORTAL_ITEM, 'Justificativa', [proof])
    fields = posted(client.calls[0])
    row = json.loads(fields['listaItensSelecionadosRecursoGlosa'][1])
    assert 'situacaoRecursadoAceito' not in fields
    assert fields['codigosSelecionados'][1] == b'731101237;'
    assert fields['listaDocumentoAnexo[0].file'][1] == b'%PDF'
    assert row['situacaoRecursadoAceito'] == 'R'
    assert row['valorRecursado'] == pytest.approx(40.19)
    assert row['valorGlosado'] == '40.19'
    assert row['dataRealizacao'] == '1755561600000'
    assert row['listaDetalheRecursoGlosa'] is None
    assert 'listaDocumentoAnexo' not in row
    assert 'stringArrayDetalhesRecursoGlosa' not in row
    assert row['mensagemRespostaPrestador'] == 'Justificativa'


def test_portal_accept_posts_zero_value_and_an_empty_file():
    client = CapturingAppeal()
    client.accept(PORTAL_LOT, PORTAL_ITEM)
    fields = posted(client.calls[0])
    row = json.loads(fields['listaItensSelecionadosRecursoGlosa'][1])
    assert row['situacaoRecursadoAceito'] == 'A'
    assert row['valorRecursado'] == '0.0'
    assert fields['mensagemRespostaPrestador'][1] == b''
    assert fields['listaDocumentoAnexo[0].file'][1] == b''
    assert b'filename=""' in fields['listaDocumentoAnexo[0].file'][0]


def test_portal_http_error_reports_the_reason_of_the_server():
    body = BytesIO(b'<html><b>Falha</b> ao gravar</html>')
    exc = HTTPError('http://x', 500, 'erro', {}, body)
    assert error_text(exc) == 'Falha ao gravar'
    body = BytesIO(json.dumps({'errorMessage': 'x' * 900}).encode())
    exc = HTTPError('http://x', 500, 'erro', {}, body)
    assert error_text(exc) == 'x' * 200


def test_portal_never_touches_an_account_already_sent(
    db, protocol, config, source, candidate
):
    rows = portal_rows(db, protocol, config, source, candidate)
    target = rows['1']
    portal = FakePortal([target], {'quantidadeItensRecursado': 1})
    with pytest.raises(TissError, match='já tem item enviado'):
        run_portal(db, config, target, portal)
    assert portal.saved == []
    assert portal.efetivados == 0
    assert target.recurso_estado == 'rascunho'


def test_failed_efetivar_is_uncertain_and_blocks_resend(
    db, protocol, config, source, candidate
):
    rows = portal_rows(db, protocol, config, source, candidate)
    target = rows['1']
    portal = FakePortal([target])
    portal.efetivar_error = TissError('Portal Petrobras: HTTP 500.')
    with pytest.raises(TissError, match='não confirmado'):
        run_portal(db, config, target, portal)
    assert target.recurso_estado == 'envio_incerto'
    with pytest.raises(TissError, match='iniciada'):
        send_appeal(db, config, target, target.versao, 1, Mock())


@pytest.mark.parametrize(
    ('justification', 'mime', 'message'),
    [
        ('curta', 'application/pdf', 'ao menos 20'),
        ('Justificativa com mais de vinte', 'image/png', 'PDF, JPG ou BMP'),
    ],
)
def test_portal_rules_are_checked_before_any_write(  # noqa: PLR0913, PLR0917 - fixtures
    db, protocol, config, source, candidate, justification, mime, message
):
    rows = portal_rows(db, protocol, config, source, candidate)
    target = rows['1']
    target.justificativa = justification
    db.add(
        PetrobrasAnexo(
            glosa_id=target.id,
            nome='a',
            mime=mime,
            conteudo=b'x',
            sha256='c' * 64,
            tipo_documento='01',
            natureza_guia='3',
            formato='02',
        )
    )
    db.commit()
    portal = FakePortal([target])
    with pytest.raises(TissError, match=message):
        run_portal(db, config, target, portal)
    assert portal.saved == []
    assert target.recurso_estado == 'rascunho'


def test_portal_screen_parsers():
    html = (
        '<form id="formularioBuscarLote_36022538" action="x"><input '
        'name="lote" value="12241103" type="hidden" value=""/><input '
        "name='periodo' value='60'/></form>"
    )
    assert lot_forms(html) == {
        '36022538': {'lote': '12241103', 'periodo': '60'}
    }
    body, kind = multipart(
        [('campo', 'é')],
        [('arq[0].file', 'nó me.pdf', 'application/pdf', b'P')],
    )
    assert 'boundary=' in kind
    assert 'name="campo"'.encode() in body
    assert b'filename="n__me.pdf"' in body


def lot_response(*protocols):
    root = ET.Element('{%s}demonstrativoRetornoWS' % NS)
    for protocol, lot in protocols:
        node = element(root, 'relacaoProtocolos')
        element(node, 'numeroProtocolo', protocol)
        element(node, 'numeroLote', lot)
    return root


def test_payment_lots_ignores_blank_and_conflicting_protocols():
    root = lot_response(
        ('0', '9'), ('1', '36022538'), ('2', '5'), ('2', '6'), ('3', '7')
    )
    assert payment_lots(root) == {'1': '36022538', '3': '7'}


def test_payer_lot_is_stored_and_the_appeal_sends_it(
    db, protocol, config, source, candidate
):
    rows = ready_rows(db, protocol, config, source, candidate)
    row = rows['1']
    original = row.fonte['lote']
    lots = {protocol.numero: '36022538'}
    assert apply_payer_lots(db, config, lots) == len(rows)
    db.commit()
    assert row.fonte['lote'] == '36022538'
    assert row.fonte['lote_prestador'] == original
    assert apply_payer_lots(db, config, lots) == 0
    client = Mock()
    client.appeal_protocol.side_effect = TimeoutError()
    with pytest.raises(TissError, match='não confirmado'):
        send_appeal(db, config, row, row.versao, 1, client)
    sent = client.appeal_protocol.call_args.args[0][0][0]
    assert sent['lote'] == '36022538'
    client.pagamentos.assert_not_called()


def test_protocol_sync_keeps_the_stored_payer_lot(
    db, protocol, config, source, candidate
):
    ready_rows(db, protocol, config, source, candidate)
    apply_payer_lots(db, config, {protocol.numero: '36022538'})
    db.commit()
    assert known_payer_lots(db, [protocol.id]) == {
        protocol.numero: '36022538'
    }


def test_row_without_the_payer_lot_cannot_be_appealed(source):
    unknown = {k: v for k, v in source.items() if k != 'lote_prestador'}
    assert 'Lote da Petrobras' in appeal_block(unknown)
    assert appeal_block(source) is None


def test_new_rows_of_a_known_protocol_get_the_payer_lot():
    rows = [
        {'protocolo': '1', 'lote': '1928'},
        {'protocolo': '2', 'lote': '5'},
    ]
    assert rows_with_payer_lots(rows, {'1': '36022538'}) == [
        {'protocolo': '1', 'lote': '36022538', 'lote_prestador': '1928'},
        {'protocolo': '2', 'lote': '5'},
    ]
