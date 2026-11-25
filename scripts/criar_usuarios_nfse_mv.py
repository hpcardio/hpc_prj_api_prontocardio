# ruff: noqa: E501
"""Cria os usuários do Receita Certa para a tela "NFS-e do setor" a partir do MV.

Fonte: usuários ativos do MV com oficina designada para lançar NFS (regra
USUARIOS_OFICINA_FUNCAO, a mesma de SQL_OFICINAS_REGRA_MV, sem 'SS').

Para cada login MV:
- usuário novo: login = login MV (nome_usuario) ou CPF; senha padrão
  "prontocardio"; só a tela contas_pagar_nfse_setor; vínculo com o login MV
  (origem 'manual': para gravar no MV o usuário ainda entra pelo login MV/CAS);
- usuário que já existe (vinculado ao login MV ou com o mesmo nome de
  usuário): ganha a tela, o CPF (se livre) e o vínculo; senha não muda.

As oficinas não são copiadas: a API lê a regra do MV a cada consulta.

Sem --aplicar só mostra o plano (nada é gravado). O relatório não mostra CPF.

    python -m scripts.criar_usuarios_nfse_mv            # plano
    python -m scripts.criar_usuarios_nfse_mv --aplicar  # grava
"""

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import text

SENHA_PADRAO = 'prontocardio'
TELA_SETOR = 'contas_pagar_nfse_setor'
# Subdomínio do hospital sem caixa de e-mail. Não usar .local: a API recusa
# domínios reservados (EmailStr) e a listagem de usuários quebra.
DOMINIO_SEM_EMAIL = 'sem-email.hospitalprontocardio.com.br'
TAMANHO_CPF = 11
RE_EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
# Contas de sistema do MV (dono do schema) não viram usuário do Receita Certa.
LOGINS_DE_SISTEMA = frozenset({'DBAMV', 'DBASGU', 'MVINTEGRA'})

SQL_USUARIOS_MV = text("""
SELECT u.cd_usuario AS cd_usuario, u.nm_usuario AS nm_usuario,
       u.cpf AS cpf, u.ds_email AS ds_email,
       o.cd_oficina AS cd_oficina, o.ds_oficina AS ds_oficina
  FROM dbamv.usuarios_oficina_funcao uof
  JOIN dbamv.oficina o ON o.cd_oficina = uof.cd_oficina
  JOIN dbamv.setor s
    ON s.cd_setor = o.cd_setor AND s.cd_multi_empresa = o.cd_multi_empresa
  JOIN dbamv.config_acma ca
    ON ca.cd_multi_empresa = o.cd_multi_empresa
   AND ca.sn_usuario_oficina_funcao = 'S'
  JOIN dbasgu.usuarios u ON u.cd_usuario = uof.cd_usuario AND u.sn_ativo = 'S'
 WHERE uof.tp_funcao <> 'SS'
""")

SQL_USUARIOS_RC = text("""
SELECT u.id, u.nome, lower(u.email) AS email, lower(u.nome_usuario) AS nome_usuario,
       u.cpf, u.telas_permitidas, upper(m.cd_usuario_mv) AS cd_usuario_mv
  FROM api_prontocardio.usuarios_api u
  LEFT JOIN api_prontocardio.usuarios_mv m ON m.usuario_id = u.id
""")

SQL_INSERIR_USUARIO = text("""
INSERT INTO api_prontocardio.usuarios_api
    (nome, email, senha, nome_usuario, cpf, troca_senha_obrigatoria, perfil, ativo,
     origem_agendamento, telas_permitidas)
VALUES (:nome, :email, :senha, :nome_usuario, :cpf, false, 'usuario', true,
        'NAO_IDENTIFICADA', CAST(:telas AS json))
RETURNING id
""")

SQL_ATUALIZAR_USUARIO = text("""
UPDATE api_prontocardio.usuarios_api
   SET telas_permitidas = CAST(:telas AS json),
       cpf = COALESCE(cpf, :cpf)
 WHERE id = :id
""")

SQL_VINCULAR_MV = text("""
INSERT INTO api_prontocardio.usuarios_mv (usuario_id, cd_usuario_mv, nm_usuario_mv)
VALUES (:usuario_id, :cd_usuario_mv, :nm_usuario_mv)
ON CONFLICT DO NOTHING
""")


@dataclass
class UsuarioMv:
    login: str
    nome: str
    cpf: str | None
    email: str | None
    oficinas: list[str] = field(default_factory=list)


@dataclass
class Acao:
    tipo: str  # criar | atualizar | ignorar
    usuario: UsuarioMv
    usuario_id: int | None = None
    nome: str | None = None
    email: str | None = None
    cpf: str | None = None
    telas: list[str] | None = None
    vincular: bool = False
    motivo: str | None = None


def _cpf(valor) -> str | None:
    digitos = ''.join(ch for ch in str(valor or '') if ch.isdigit())
    return digitos if len(digitos) == TAMANHO_CPF and len(set(digitos)) > 1 else None


def usuarios_do_mv(linhas) -> list[UsuarioMv]:
    por_login: dict[str, UsuarioMv] = {}
    oficinas = defaultdict(set)
    for linha in linhas:
        login = str(linha['cd_usuario']).strip().upper()
        if login in LOGINS_DE_SISTEMA:
            continue
        email = (linha['ds_email'] or '').strip().lower()
        por_login.setdefault(login, UsuarioMv(
            login=login,
            nome=' '.join(str(linha['nm_usuario'] or login).split()),
            cpf=_cpf(linha['cpf']),
            email=email if RE_EMAIL.match(email) else None,
        ))
        oficinas[login].add(linha['ds_oficina'])
    for login, usuario in por_login.items():
        usuario.oficinas = sorted(oficinas[login])
    return sorted(por_login.values(), key=lambda u: u.nome)


def planejar(usuarios_mv: list[UsuarioMv], existentes: list[dict]) -> list[Acao]:
    """Decide o que fazer com cada login MV, sem gravar nada."""
    por_vinculo = {e['cd_usuario_mv']: e for e in existentes if e['cd_usuario_mv']}
    por_nome_usuario = {e['nome_usuario']: e for e in existentes if e['nome_usuario']}
    nomes = {e['nome'].strip().lower() for e in existentes}
    emails = {e['email'] for e in existentes if e['email']}
    cpfs = {e['cpf'] for e in existentes if e['cpf']}
    vinculados = set(por_vinculo)

    acoes = []
    for u in usuarios_mv:
        existente = por_vinculo.get(u.login) or por_nome_usuario.get(u.login.lower())
        cpf = u.cpf if u.cpf and u.cpf not in cpfs else None
        if existente is not None:
            if existente['cd_usuario_mv'] not in (None, u.login):
                acoes.append(Acao('ignorar', u, existente['id'], motivo=(
                    f"usuário {existente['nome_usuario']} já vinculado ao login MV "
                    f"{existente['cd_usuario_mv']}")))
                continue
            telas = list(existente['telas_permitidas'] or [])
            mudou_tela = TELA_SETOR not in telas
            if mudou_tela:
                telas.append(TELA_SETOR)
            cpf = cpf if not existente['cpf'] else None
            vincular = existente['cd_usuario_mv'] is None and u.login not in vinculados
            if not (mudou_tela or cpf or vincular):
                acoes.append(Acao('ignorar', u, existente['id'], motivo='já configurado'))
                continue
            acoes.append(Acao('atualizar', u, existente['id'], cpf=cpf, telas=telas, vincular=vincular))
            if cpf:
                cpfs.add(cpf)
            vinculados.add(u.login)
            continue
        if u.login in vinculados:
            acoes.append(Acao('ignorar', u, motivo='login MV já vinculado a outro usuário'))
            continue
        nome = u.nome if u.nome.lower() not in nomes else f'{u.nome} ({u.login})'
        email = u.email if u.email and u.email not in emails else f'{u.login.lower()}@{DOMINIO_SEM_EMAIL}'
        if email in emails:
            acoes.append(Acao('ignorar', u, motivo=f'email {email} já usado'))
            continue
        acoes.append(Acao('criar', u, nome=nome, email=email, cpf=cpf, telas=[TELA_SETOR], vincular=True))
        nomes.add(nome.lower())
        emails.add(email)
        vinculados.add(u.login)
        if cpf:
            cpfs.add(cpf)
    return acoes


def aplicar(postgres, acoes: list[Acao], hash_senha: str) -> None:
    import json  # noqa: PLC0415

    for acao in acoes:
        if acao.tipo == 'criar':
            acao.usuario_id = postgres.execute(SQL_INSERIR_USUARIO, {
                'nome': acao.nome, 'email': acao.email, 'senha': hash_senha,
                'nome_usuario': acao.usuario.login, 'cpf': acao.cpf,
                'telas': json.dumps(acao.telas),
            }).scalar_one()
        elif acao.tipo == 'atualizar':
            postgres.execute(SQL_ATUALIZAR_USUARIO, {
                'id': acao.usuario_id, 'telas': json.dumps(acao.telas), 'cpf': acao.cpf,
            })
        else:
            continue
        if acao.vincular:
            postgres.execute(SQL_VINCULAR_MV, {
                'usuario_id': acao.usuario_id, 'cd_usuario_mv': acao.usuario.login,
                'nm_usuario_mv': acao.usuario.nome,
            })


def _relatorio(acoes: list[Acao]) -> None:
    for acao in acoes:
        login_cpf = 'login MV ou CPF' if (acao.cpf or acao.usuario.cpf) else 'login MV (sem CPF)'
        detalhe = acao.motivo or login_cpf
        print(f'{acao.tipo:9} {acao.usuario.login:10} {acao.usuario.nome[:40]:40} '
              f'{len(acao.usuario.oficinas)} oficina(s) · {detalhe}')
    total = defaultdict(int)
    for acao in acoes:
        total[acao.tipo] += 1
    print('total', dict(total), '| com CPF para login', sum(1 for a in acoes if a.tipo == 'criar' and a.cpf))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--aplicar', action='store_true')
    args = parser.parse_args()

    from sqlalchemy.orm import Session  # noqa: PLC0415

    from app_prontocardio.database import (  # noqa: PLC0415
        oracle_engine,
        postgres_engine,
    )
    from app_prontocardio.security import gera_hash_senha  # noqa: PLC0415

    with oracle_engine.connect() as oracle:
        oracle.execute(text('SET TRANSACTION READ ONLY'))
        usuarios_mv = usuarios_do_mv(oracle.execute(SQL_USUARIOS_MV).mappings().all())
    with Session(postgres_engine) as postgres:
        existentes = [dict(linha) for linha in postgres.execute(SQL_USUARIOS_RC).mappings().all()]
        acoes = planejar(usuarios_mv, existentes)
        _relatorio(acoes)
        if not args.aplicar:
            print('Plano apenas (nada gravado). Use --aplicar para gravar.')
            return
        aplicar(postgres, acoes, gera_hash_senha(SENHA_PADRAO))
        postgres.commit()
        print('Gravado.')


if __name__ == '__main__':
    main()
