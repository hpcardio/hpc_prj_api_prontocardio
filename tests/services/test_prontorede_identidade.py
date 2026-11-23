# ruff: noqa: E501
"""ProntoRede 2.4.7: identidade MV por código de usuário ou nome do prestador.

Documenta o comportamento recuperado de produção (imagens
prontorede-identity-2.4.7 e prontorede-wildcard-route de 23/11). Atenção: com
PRONTOREDE_MV_ALLOWED_USERS='*' qualquer prestador ativo com CRM é aceito.
"""

from contextlib import contextmanager

import pytest

from evolucao_sadt_backend import prontorede_oracle as po

LINHA = {
    'cd_atendimento': 123,
    'cd_usuario': 'MEDICO01',
    'cd_prestador': 77,
    'ds_conselho': 'CRM',
    'ds_codigo_conselho': '12345',
    'cd_uf_orgao_emissor': 'CE',
    'match_count': 1,
}


def _conexao(linha):
    executadas = []

    class Resultado:
        def mappings(self):
            return self

        def first(self):
            return linha

    class Conexao:
        def execute(self, sql, params):
            executadas.append(params)
            return Resultado()

    @contextmanager
    def fabrica():
        yield Conexao()

    return fabrica, executadas


def _resolver(identidade, linha=LINHA):
    fabrica, executadas = _conexao(linha)
    contexto = po.resolve_authorized_mv_context(123, identidade, connection_factory=fabrica)
    return contexto, executadas


def test_identidade_pelo_nome_do_prestador_com_acentos(monkeypatch):
    monkeypatch.setenv('PRONTOREDE_MV_ALLOWED_USERS', 'MEDICO01')
    contexto, executadas = _resolver("  José d'Ávila Médico  ")
    assert executadas[0]['identity'] == "JOSÉ D'ÁVILA MÉDICO"
    assert (contexto.cd_usuario, contexto.cd_prestador, contexto.crm) == ('MEDICO01', 77, 'CRM-CE 12345')


def test_usuario_resolvido_precisa_estar_na_lista(monkeypatch):
    monkeypatch.setenv('PRONTOREDE_MV_ALLOWED_USERS', 'OUTRO')
    with pytest.raises(po.MvContextAuthorizationError):
        _resolver('MEDICO01')


def test_curinga_aceita_qualquer_prestador_com_crm(monkeypatch):
    monkeypatch.setenv('PRONTOREDE_MV_ALLOWED_USERS', '*')
    contexto, _ = _resolver('QUALQUER NOME')
    assert contexto.cd_usuario == 'MEDICO01'


def test_lista_vazia_bloqueia_antes_de_consultar(monkeypatch):
    monkeypatch.delenv('PRONTOREDE_MV_ALLOWED_USERS', raising=False)
    fabrica, executadas = _conexao(LINHA)
    with pytest.raises(po.MvContextAuthorizationError):
        po.resolve_authorized_mv_context(123, 'MEDICO01', connection_factory=fabrica)
    assert executadas == []


@pytest.mark.parametrize('linha', [
    {**LINHA, 'match_count': 2},           # nome de prestador ambíguo
    {**LINHA, 'ds_conselho': 'CRF'},       # não é médico
    {**LINHA, 'cd_atendimento': 999},      # outro atendimento
    None,                                   # atendimento com alta ou identidade inexistente
])
def test_contexto_recusado(monkeypatch, linha):
    monkeypatch.setenv('PRONTOREDE_MV_ALLOWED_USERS', '*')
    with pytest.raises(po.MvContextAuthorizationError):
        _resolver('MEDICO01', linha)


@pytest.mark.parametrize('identidade', ['A', 'X' * 121, 'NOME;DROP', 'NOME<script>'])
def test_identidade_fora_do_formato_e_recusada(monkeypatch, identidade):
    monkeypatch.setenv('PRONTOREDE_MV_ALLOWED_USERS', '*')
    with pytest.raises(po.MvContextAuthorizationError):
        _resolver(identidade)
