import re
from http import HTTPStatus
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app_prontocardio.database import (
    get_session_oracle,
    get_session_postgres,
)
from evolucao_sadt_backend.evolucao_schema import (
    ContextoEvolucaoMv,
    EvolucaoMv,
    EvolucaoRascunhoInput,
    EvolucaoTextoInput,
    EvolucoesMv,
    LinhasCuidadoEvolucao,
)
from app_prontocardio.evolucao_service import (
    ConfiguracaoEvolucaoMv,
    EvolucaoMvConfiguracaoInvalida,
    EvolucaoMvConflito,
    EvolucaoMvNaoEncontrada,
    assinar_evolucao,
    buscar_contexto,
    criar_rascunho,
    listar_evolucoes,
    obter_evolucao,
    salvar_texto,
)
from app_prontocardio.models import AuditoriaEvolucaoMv, Usuario
from app_prontocardio.security import valida_usuario_ti
from app_prontocardio.settings import Settings

router = APIRouter(prefix='/mv/evolucoes', tags=['evolucoes-mv'])
settings = Settings()
LIMITE_TEXTO_EVOLUCAO = 4000

LINHAS_CUIDADO_CARDIOLOGIA = (
    {
        'codigo': 'LC-DAC',
        'nome': 'Doença Arterial Coronariana e Dor Torácica',
        'documento_mv_parametrizado': True,
        'fluxo_resumido': [
            'Avaliar sintomas e fatores de risco',
            'Estimar a probabilidade pré-teste',
            'Definir a estratégia diagnóstica',
            'Interpretar anatomia e/ou carga isquêmica',
            'Definir necessidade de internação',
            'Implementar tratamento e seguimento',
        ],
        'exames': [
            {
                'codigo': '40101010',
                'descricao': 'ECG convencional de até 12 derivações',
                'criterio': 'Avaliação inicial',
            },
            {
                'codigo': '41001230',
                'descricao': 'TC - Angiotomografia coronariana',
                'criterio': 'Risco intermediário ou dúvida anatômica',
            },
            {
                'codigo': '40101037',
                'descricao': 'Teste ergométrico computadorizado',
                'criterio': 'Capacidade de exercício e ECG interpretável',
            },
            {
                'codigo': '40901696',
                'descricao': 'Ecodopplercardiograma com estresse físico',
                'criterio': 'Avaliação funcional de isquemia',
            },
            {
                'codigo': '40701131',
                'descricao': (
                    'Cintilografia do miocárdio - estresse farmacológico'
                ),
                'criterio': 'Estratificação funcional conforme indicação',
            },
        ],
        'diagramas': [
            {
                'titulo': 'Fluxo executivo da LC-DAC',
                'url': '/linhas-cuidado/diagramas/lc-dac.png',
            },
        ],
    },
    {
        'codigo': 'LC-IC',
        'nome': 'Insuficiência Cardíaca',
        'documento_mv_parametrizado': False,
        'fluxo_resumido': [
            'Confirmar diagnóstico e definir fenótipo',
            'Estratificar risco e investigar etiologia',
            'Identificar alto risco e necessidade de navegação intensificada',
            'Otimizar tratamento conforme fenótipo',
            'Reavaliar em cada consulta e acompanhar progressão',
        ],
        'exames': [
            {
                'codigo': '40304361',
                'descricao': 'Hemograma com plaquetas',
                'criterio': 'Primeira consulta',
            },
            {
                'codigo': '40302776',
                'descricao': 'Peptídeo natriurético BNP/PROBNP',
                'criterio': 'Diagnóstico e estratificação',
            },
            {
                'codigo': '40302580',
                'descricao': 'Ureia',
                'criterio': 'Função renal',
            },
            {
                'codigo': '40301630',
                'descricao': 'Creatinina',
                'criterio': 'Função renal',
            },
            {
                'codigo': '40302423',
                'descricao': 'Sódio',
                'criterio': 'Eletrólitos',
            },
            {
                'codigo': '40302318',
                'descricao': 'Potássio',
                'criterio': 'Eletrólitos e segurança terapêutica',
            },
            {
                'codigo': '40302237',
                'descricao': 'Magnésio',
                'criterio': 'Eletrólitos',
            },
            {
                'codigo': '40302750',
                'descricao': 'Perfil lipídico / lipidograma',
                'criterio': 'Risco cardiovascular',
            },
            {
                'codigo': '40316521',
                'descricao': 'Hormônio tireoestimulante (TSH)',
                'criterio': 'Investigação etiológica',
            },
            {
                'codigo': '40316270',
                'descricao': 'Ferritina',
                'criterio': 'Avaliação de deficiência de ferro',
            },
            {
                'codigo': '40302520',
                'descricao': 'Transferrina',
                'criterio': 'Avaliação de deficiência de ferro',
            },
            {
                'codigo': '40101010',
                'descricao': 'ECG convencional de até 12 derivações',
                'criterio': 'Avaliação cardiológica',
            },
            {
                'codigo': '40901106',
                'descricao': 'Ecodopplercardiograma transtorácico',
                'criterio': 'Fenótipo e função ventricular',
            },
        ],
        'diagramas': [
            {
                'titulo': 'Fluxograma ambulatorial de insuficiência cardíaca',
                'url': '/linhas-cuidado/diagramas/lc-ic.png',
            },
        ],
    },
    {
        'codigo': 'LC-PREVENCAO',
        'nome': 'Prevenção Cardiovascular e Cardiometabólica',
        'documento_mv_parametrizado': False,
        'fluxo_resumido': [
            'Realizar avaliação global inicial',
            'Estratificar risco cardiovascular',
            'Definir metas terapêuticas individualizadas',
            'Construir plano terapêutico integrado',
            'Reavaliar em 30 dias, 6 meses, 1 ano e semestralmente',
        ],
        'exames': [
            {
                'codigo': '40304361',
                'descricao': 'Hemograma com plaquetas',
                'criterio': 'Avaliação inicial',
            },
            {
                'codigo': '40302040',
                'descricao': 'Glicose',
                'criterio': 'Risco metabólico',
            },
            {
                'codigo': '40302733',
                'descricao': 'Hemoglobina glicada (HbA1c)',
                'criterio': 'Risco metabólico',
            },
            {
                'codigo': '40301630',
                'descricao': 'Creatinina',
                'criterio': 'Risco cardiorrenal',
            },
            {
                'codigo': '40302423',
                'descricao': 'Sódio',
                'criterio': 'Avaliação inicial',
            },
            {
                'codigo': '40302318',
                'descricao': 'Potássio',
                'criterio': 'Avaliação inicial e ajustes terapêuticos',
            },
            {
                'codigo': '40302750',
                'descricao': 'Perfil lipídico / lipidograma',
                'criterio': 'Estratificação de risco',
            },
            {
                'codigo': '40301362',
                'descricao': 'Apolipoproteína B (Apo B)',
                'criterio': 'Estratificação avançada',
            },
            {
                'codigo': '40302210',
                'descricao': 'Lipoproteína (a) - Lp(a)',
                'criterio': 'Uma vez na vida',
            },
            {
                'codigo': '40311171',
                'descricao': 'Microalbuminúria',
                'criterio': 'Risco cardiorrenal',
            },
            {
                'codigo': '40302504',
                'descricao': 'Transaminase oxalacética (TGO/AST)',
                'criterio': 'Avaliação inicial',
            },
            {
                'codigo': '40302512',
                'descricao': 'Transaminase pirúvica (TGP/ALT)',
                'criterio': 'Avaliação inicial',
            },
            {
                'codigo': '40316521',
                'descricao': 'Hormônio tireoestimulante (TSH)',
                'criterio': 'Avaliação inicial',
            },
            {
                'codigo': '40101010',
                'descricao': 'ECG convencional de até 12 derivações',
                'criterio': 'Avaliação cardiovascular',
            },
            {
                'codigo': '40901106',
                'descricao': 'Ecodopplercardiograma transtorácico',
                'criterio': 'Conforme risco e achados',
            },
            {
                'codigo': '20102038',
                'descricao': 'MAPA de 24 horas',
                'criterio': 'Conforme indicação',
            },
            {
                'codigo': '40901360',
                'descricao': 'Doppler de carótidas e vertebrais bilateral',
                'criterio': 'Conforme indicação',
            },
            {
                'codigo': '41001230',
                'descricao': 'TC - Angiotomografia coronariana',
                'criterio': 'Conforme indicação',
            },
            {
                'codigo': '40103528',
                'descricao': 'Polissonografia de noite inteira',
                'criterio': 'Suspeita de apneia do sono',
            },
        ],
        'diagramas': [
            {
                'titulo': 'Estratificação e plano terapêutico',
                'url': '/linhas-cuidado/diagramas/prevencao-1.png',
            },
            {
                'titulo': 'Seguimento ambulatorial padronizado',
                'url': '/linhas-cuidado/diagramas/prevencao-2.png',
            },
            {
                'titulo': 'Metas da linha de cuidado',
                'url': '/linhas-cuidado/diagramas/prevencao-3.png',
            },
        ],
    },
    {
        'codigo': 'LC-VALVOPATIAS',
        'nome': 'Valvopatias',
        'documento_mv_parametrizado': False,
        'fluxo_resumido': [
            'Definir a valva e avaliar sintomas',
            'Realizar avaliação estrutural por ecocardiograma',
            'Classificar gravidade e identificar critérios de alerta',
            'Definir exames complementares conforme a valvopatia',
            'Encaminhar ao Heart Team quando indicado',
            'Programar tratamento e seguimento',
        ],
        'exames': [
            {
                'codigo': '40901106',
                'descricao': 'Ecodopplercardiograma transtorácico',
                'criterio': 'Exame central',
            },
            {
                'codigo': '40101010',
                'descricao': 'ECG convencional de até 12 derivações',
                'criterio': 'Avaliação inicial e seguimento',
            },
            {
                'codigo': '40901092',
                'descricao': 'Ecodopplercardiograma transesofágico',
                'criterio': 'Anatomia, planejamento ou dúvida diagnóstica',
            },
            {
                'codigo': '40302776',
                'descricao': 'Peptídeo natriurético BNP/PROBNP',
                'criterio': 'Repercussão funcional ou suspeita de IC',
            },
            {
                'codigo': '40301630',
                'descricao': 'Creatinina',
                'criterio': 'Função renal e planejamento',
            },
            {
                'codigo': '40302423',
                'descricao': 'Sódio',
                'criterio': 'Conforme condição clínica',
            },
            {
                'codigo': '40302318',
                'descricao': 'Potássio',
                'criterio': 'Conforme condição clínica',
            },
        ],
        'diagramas': [
            {
                'titulo': 'Estenose aórtica',
                'url': (
                    '/linhas-cuidado/diagramas/valvopatia-estenose-aortica.png'
                ),
            },
            {
                'titulo': 'Insuficiência aórtica',
                'url': (
                    '/linhas-cuidado/diagramas/'
                    'valvopatia-insuficiencia-aortica.png'
                ),
            },
            {
                'titulo': 'Estenose mitral',
                'url': (
                    '/linhas-cuidado/diagramas/valvopatia-estenose-mitral.png'
                ),
            },
            {
                'titulo': 'Insuficiência mitral',
                'url': (
                    '/linhas-cuidado/diagramas/'
                    'valvopatia-insuficiencia-mitral.png'
                ),
            },
            {
                'titulo': 'Insuficiência tricúspide',
                'url': (
                    '/linhas-cuidado/diagramas/'
                    'valvopatia-insuficiencia-tricuspide.png'
                ),
            },
        ],
    },
)

MOMENTOS_CONSULTA = (
    'cal1',
    'cir1',
    'cal2',
    'cir2',
    'cal3',
    'cir3',
    'retorno_semestral',
    'consulta_extra',
)


def _exame_por_momento(
    codigo: str,
    descricao: str,
    momentos: tuple[str, ...],
    *,
    opcional: bool = False,
):
    return {
        'codigo': codigo,
        'descricao': descricao,
        'criterio': (
            'Opcional conforme avaliação médica'
            if opcional
            else 'Previsto para ' + ', '.join(item.upper() for item in momentos)
        ),
        'momentos': list(momentos),
        'opcional': opcional,
    }


EXAMES_POR_MOMENTO = {
    'LC-DAC': [
        _exame_por_momento('40304361', 'Hemograma com contagem de plaquetas', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40301630', 'Creatinina', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302580', 'Ureia', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302423', 'Sódio', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302318', 'Potássio', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302750', 'Perfil lipídico / colesterol total e frações', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302040', 'Glicemia de jejum', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302733', 'Hemoglobina glicada (HbA1c)', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302504', 'TGO/AST', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302512', 'TGP/ALT', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40301648', 'CPK total', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40901360', 'Doppler de artérias carótidas e vertebrais bilateral', ('cal1', 'cal3')),
        _exame_por_momento('40901475', 'Doppler arterial de membro inferior - unilateral', ('cal1', 'cal3')),
        _exame_por_momento('40901106', 'Ecodopplercardiograma transtorácico', ('cal1', 'cal3')),
        _exame_por_momento('40101010', 'ECG convencional de até 12 derivações', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('20102038', 'MAPA de 24 horas', ('cal2', 'cal3')),
        _exame_por_momento('41001230', 'AngioTC de coronárias para ICP CHIP', ('cal3',)),
    ],
    'LC-IC': [
        _exame_por_momento('40304361', 'Hemograma com contagem de plaquetas', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40301630', 'Creatinina', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302580', 'Ureia', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302776', 'Peptídeo natriurético BNP/PROBNP', ('cal1', 'cal3')),
        _exame_por_momento('40302423', 'Sódio', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302318', 'Potássio', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302237', 'Magnésio', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302750', 'Perfil lipídico / colesterol total e frações', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302040', 'Glicemia de jejum', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302733', 'Hemoglobina glicada (HbA1c)', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40316521', 'Hormônio tireoestimulante (TSH)', ('cal1', 'cal3')),
        _exame_por_momento('40316270', 'Ferritina', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40321231', 'Índice de saturação de ferro / saturação de transferrina', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302504', 'TGO/AST', ('cal1', 'cal3')),
        _exame_por_momento('40302512', 'TGP/ALT', ('cal1', 'cal3')),
        _exame_por_momento('40301648', 'CPK total', ('cal1',)),
        _exame_por_momento('40311210', 'Sumário de urina', ('cal1',)),
        _exame_por_momento('40311171', 'Relação albumina/creatinina urinária', ('cal1',)),
        _exame_por_momento('40901360', 'Doppler de artérias carótidas e vertebrais bilateral', ('cal1',)),
        _exame_por_momento('40901475', 'Doppler arterial de membro inferior - unilateral', ('cal1',)),
        _exame_por_momento('40901106', 'Ecodopplercardiograma transtorácico', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40101010', 'ECG convencional de até 12 derivações', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('41001230', 'Angiotomografia coronariana (AngioTC)', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('30911079', 'Coronariografia', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('20102020', 'Holter de 24 horas - 3 canais digital', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('20102038', 'MAPA de 24 horas', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('40101037', 'Teste ergométrico computadorizado', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('41101138', 'Ressonância magnética cardíaca', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('40302717', 'Eletroforese de proteínas séricas', MOMENTOS_CONSULTA, opcional=True),
    ],
    'LC-PREVENCAO': [
        _exame_por_momento('40304361', 'Hemograma com contagem de plaquetas', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40301630', 'Creatinina', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302580', 'Ureia', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302423', 'Sódio', ('cal1',)),
        _exame_por_momento('40302318', 'Potássio', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302040', 'Glicemia de jejum', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302733', 'Hemoglobina glicada (HbA1c)', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302750', 'Perfil lipídico / colesterol total e frações', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40301362', 'Apolipoproteína B (Apo B)', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302210', 'Lipoproteína (a) - Lp(a)', ('cal1',)),
        _exame_por_momento('40311171', 'Relação albumina/creatinina urinária', ('cal1', 'cal2', 'cal3')),
        _exame_por_momento('40302504', 'TGO/AST', ('cal1',)),
        _exame_por_momento('40302512', 'TGP/ALT', ('cal1',)),
        _exame_por_momento('40316521', 'Hormônio tireoestimulante (TSH)', ('cal1',)),
        _exame_por_momento('40301648', 'CPK total', ('cal1',)),
        _exame_por_momento('40101010', 'ECG convencional de até 12 derivações', ('cal1', 'cal3')),
        _exame_por_momento('40901106', 'Ecodopplercardiograma transtorácico', ('cal1', 'cal3')),
        _exame_por_momento('20102038', 'MAPA de 24 horas', ('cal1', 'cal3')),
        _exame_por_momento('40901360', 'Doppler de artérias carótidas e vertebrais bilateral', ('cal1', 'cal3')),
        _exame_por_momento('40901475', 'Doppler arterial de membro inferior - unilateral', ('cal1',)),
        _exame_por_momento('41301439', 'Fundoscopia sob midríase - binocular', ('cal1', 'cal3')),
        _exame_por_momento('20102020', 'Holter de 24 horas - 3 canais digital', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('41001087', 'TC de coração para escore de cálcio coronariano', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('41001230', 'AngioTC de coronárias', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento('40103528', 'Polissonografia de noite inteira', MOMENTOS_CONSULTA, opcional=True),
        _exame_por_momento(
            '40901475',
            'Doppler arterial de membro inferior - unilateral',
            tuple(item for item in MOMENTOS_CONSULTA if item != 'cal1'),
            opcional=True,
        ),
    ],
}

_MARCADOR_LINHA_CUIDADO = re.compile(
    r'^LINHA DE CUIDADO:[^\r\n]*(?:\r?\n)*',
    re.IGNORECASE,
)


def _normalizar_linha_cuidado(texto: str, codigo: str | None) -> str:
    if not codigo:
        return texto.strip()
    linha = next(
        (
            item
            for item in LINHAS_CUIDADO_CARDIOLOGIA
            if item['codigo'] == codigo
        ),
        None,
    )
    if linha is None:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='Linha de cuidado inválida.',
        )
    conteudo = _MARCADOR_LINHA_CUIDADO.sub('', texto).lstrip()
    marcador = f'LINHA DE CUIDADO: {linha["codigo"]} | {linha["nome"]}'
    texto_normalizado = f'{marcador}\n\n{conteudo}'.rstrip()
    if len(texto_normalizado) > LIMITE_TEXTO_EVOLUCAO:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail='O texto da evolução excede 4000 caracteres.',
        )
    return texto_normalizado

SessionOracle = Annotated[Session, Depends(get_session_oracle)]
SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
UsuarioTi = Annotated[Usuario, Depends(valida_usuario_ti)]
ChaveIdempotencia = Annotated[
    str,
    Header(
        alias='Idempotency-Key',
        min_length=8,
        max_length=64,
        pattern=r'^[A-Za-z0-9._:-]+$',
    ),
]


def _configuracao() -> ConfiguracaoEvolucaoMv:
    return ConfiguracaoEvolucaoMv(
        usuario_mv=settings.EVOLUCAO_MV_USUARIO.upper(),
        cd_tipo_documento=settings.EVOLUCAO_MV_CD_TIPO_DOCUMENTO,
        cd_objeto=settings.EVOLUCAO_MV_CD_OBJETO,
        hora_validade=settings.EVOLUCAO_MV_HORA_VALIDADE,
    )


def _validar_escrita_habilitada() -> None:
    if not settings.EVOLUCAO_MV_WRITE_ENABLED:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=(
                'Escrita de evolucoes no MV desabilitada. '
                'Ative EVOLUCAO_MV_WRITE_ENABLED somente para '
                'teste controlado.'
            ),
        )


def _mapear_erro(exc: Exception) -> HTTPException:
    if isinstance(exc, EvolucaoMvNaoEncontrada):
        return HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc))
    if isinstance(exc, EvolucaoMvConflito):
        return HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(exc))
    if isinstance(exc, EvolucaoMvConfiguracaoInvalida):
        return HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    return HTTPException(
        status_code=HTTPStatus.BAD_GATEWAY,
        detail='Falha de comunicacao com o MV.',
    )


def _reservar_auditoria(  # noqa: PLR0913
    session: Session,
    chave: str,
    acao: str,
    cd_atendimento: int,
    usuario: Usuario,
    cd_pre_med: int | None = None,
) -> tuple[AuditoriaEvolucaoMv, bool]:
    existente = session.scalar(
        select(AuditoriaEvolucaoMv).where(
            AuditoriaEvolucaoMv.chave_idempotencia == chave
        )
    )
    if existente is not None:
        if (
            existente.acao != acao
            or existente.cd_atendimento != cd_atendimento
            or (cd_pre_med is not None and existente.cd_pre_med != cd_pre_med)
        ):
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail='Chave de idempotencia ja usada em outra operacao.',
            )
        if existente.status == 'concluido':
            return existente, True
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                'Operacao com esta chave requer reconciliacao '
                'antes de repetir.'
            ),
        )

    auditoria = AuditoriaEvolucaoMv(
        chave_idempotencia=chave,
        acao=acao,
        status='iniciado',
        operador_id=getattr(usuario, 'id', None),
        operador_nome=getattr(usuario, 'nome', 'TI'),
        cd_atendimento=cd_atendimento,
        cd_pre_med=cd_pre_med,
        cd_documento_clinico=None,
        detalhe=None,
    )
    session.add(auditoria)
    session.commit()
    session.refresh(auditoria)
    return auditoria, False


def _concluir_auditoria(
    session: Session,
    auditoria: AuditoriaEvolucaoMv,
    evolucao: EvolucaoMv,
) -> None:
    auditoria.status = 'concluido'
    auditoria.cd_pre_med = evolucao.cd_pre_med
    auditoria.cd_documento_clinico = evolucao.cd_documento_clinico
    auditoria.detalhe = None
    session.commit()


def _falhar_auditoria(
    session: Session,
    auditoria: AuditoriaEvolucaoMv,
    exc: Exception,
) -> None:
    session.rollback()
    auditoria.status = 'falhou'
    auditoria.detalhe = ' '.join(str(exc).split())[:500]
    session.add(auditoria)
    session.commit()


def _executar_com_auditoria(
    session_postgres: Session,
    auditoria: AuditoriaEvolucaoMv,
    operacao: Callable[[], EvolucaoMv],
) -> EvolucaoMv:
    try:
        evolucao = operacao()
        _concluir_auditoria(session_postgres, auditoria, evolucao)
        return evolucao
    except (
        EvolucaoMvNaoEncontrada,
        EvolucaoMvConflito,
        EvolucaoMvConfiguracaoInvalida,
        SQLAlchemyError,
    ) as exc:
        _falhar_auditoria(session_postgres, auditoria, exc)
        raise _mapear_erro(exc) from exc
    except Exception as exc:
        _falhar_auditoria(session_postgres, auditoria, exc)
        raise _mapear_erro(exc) from exc


def _obter_evolucao_ou_erro(
    session: Session,
    cd_pre_med: int | None,
) -> EvolucaoMv:
    if cd_pre_med is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail='Auditoria concluida sem identificador da evolucao.',
        )
    try:
        return obter_evolucao(session, cd_pre_med)
    except (EvolucaoMvNaoEncontrada, SQLAlchemyError) as exc:
        raise _mapear_erro(exc) from exc


@router.get(
    '/contexto/{cd_atendimento}',
    response_model=ContextoEvolucaoMv,
)
def consultar_contexto(
    cd_atendimento: int,
    session: SessionOracle,
    _: UsuarioTi,
):
    try:
        return buscar_contexto(
            session,
            cd_atendimento,
            _configuracao(),
            settings.EVOLUCAO_MV_WRITE_ENABLED,
        )
    except (
        EvolucaoMvNaoEncontrada,
        EvolucaoMvConfiguracaoInvalida,
        SQLAlchemyError,
    ) as exc:
        raise _mapear_erro(exc) from exc


@router.get(
    '/atendimentos/{cd_atendimento}',
    response_model=EvolucoesMv,
)
def consultar_evolucoes(
    cd_atendimento: int,
    session: SessionOracle,
    _: UsuarioTi,
):
    try:
        return {
            'evolucoes': listar_evolucoes(session, cd_atendimento),
        }
    except SQLAlchemyError as exc:
        raise _mapear_erro(exc) from exc


@router.get(
    '/linhas-cuidado',
    response_model=LinhasCuidadoEvolucao,
)
def consultar_linhas_cuidado(_: UsuarioTi):
    return {
        'linhas': [
            {
                **linha,
                'exames': EXAMES_POR_MOMENTO.get(
                    linha['codigo'], linha['exames']
                ),
                'texto_evolucao': (
                    f'LINHA DE CUIDADO: {linha["codigo"]} | {linha["nome"]}'
                ),
            }
            for linha in LINHAS_CUIDADO_CARDIOLOGIA
        ]
    }


@router.post(
    '/rascunhos',
    status_code=HTTPStatus.CREATED,
    response_model=EvolucaoMv,
)
def criar_evolucao_rascunho(
    payload: EvolucaoRascunhoInput,
    chave: ChaveIdempotencia,
    session_oracle: SessionOracle,
    session_postgres: SessionPostgres,
    usuario: UsuarioTi,
):
    _validar_escrita_habilitada()
    auditoria, repetida = _reservar_auditoria(
        session_postgres,
        chave,
        'criar',
        payload.cd_atendimento,
        usuario,
    )
    if repetida:
        return _obter_evolucao_ou_erro(session_oracle, auditoria.cd_pre_med)

    def operacao() -> EvolucaoMv:
        contexto = buscar_contexto(
            session_oracle,
            payload.cd_atendimento,
            _configuracao(),
            True,
        )
        return criar_rascunho(
            session_oracle,
            contexto,
            _configuracao(),
        )

    return _executar_com_auditoria(session_postgres, auditoria, operacao)


@router.put('/{cd_pre_med}', response_model=EvolucaoMv)
def salvar_evolucao(  # noqa: PLR0913
    cd_pre_med: int,
    payload: EvolucaoTextoInput,
    chave: ChaveIdempotencia,
    session_oracle: SessionOracle,
    session_postgres: SessionPostgres,
    usuario: UsuarioTi,
):
    _validar_escrita_habilitada()
    atual = _obter_evolucao_ou_erro(session_oracle, cd_pre_med)
    auditoria, repetida = _reservar_auditoria(
        session_postgres,
        chave,
        'salvar',
        atual.cd_atendimento,
        usuario,
        cd_pre_med,
    )
    if repetida:
        return _obter_evolucao_ou_erro(session_oracle, cd_pre_med)
    texto = _normalizar_linha_cuidado(
        payload.texto,
        payload.linha_cuidado,
    )
    return _executar_com_auditoria(
        session_postgres,
        auditoria,
        lambda: salvar_texto(
            session_oracle,
            cd_pre_med,
            texto,
            {atual.cd_atendimento},
        ),
    )


def _cancelar_rascunho(
    session: Session,
    cd_pre_med: int,
    atendimentos_permitidos: set[int],
) -> EvolucaoMv:
    configuracao = _configuracao()
    row = session.execute(
        text(
            """
            SELECT P.CD_ATENDIMENTO,
                   P.CD_DOCUMENTO_CLINICO,
                   P.FL_IMPRESSO,
                   P.SN_FECHADO,
                   P.NM_USUARIO,
                   P.CD_OBJETO,
                   D.TP_STATUS,
                   D.CD_TIPO_DOCUMENTO
              FROM DBAMV.PRE_MED P
              JOIN DBAMV.PW_DOCUMENTO_CLINICO D
                ON D.CD_DOCUMENTO_CLINICO = P.CD_DOCUMENTO_CLINICO
             WHERE P.CD_PRE_MED = :cd_pre_med
               FOR UPDATE NOWAIT
            """
        ),
        {'cd_pre_med': cd_pre_med},
    ).mappings().one_or_none()
    if row is None:
        session.rollback()
        raise EvolucaoMvNaoEncontrada('Evolucao nao encontrada no MV.')
    if row['cd_atendimento'] not in atendimentos_permitidos:
        session.rollback()
        raise EvolucaoMvConflito('Evolucao pertence a outro atendimento.')
    if (
        row['cd_tipo_documento'] != configuracao.cd_tipo_documento
        or row['cd_objeto'] != configuracao.cd_objeto
        or str(row['nm_usuario'] or '').upper() != configuracao.usuario_mv.upper()
    ):
        session.rollback()
        raise EvolucaoMvConflito(
            'Somente rascunhos criados por esta integracao podem ser descartados.'
        )
    if (
        row['tp_status'] != 'ABERTO'
        or row['fl_impresso'] != 'N'
        or row['sn_fechado'] != 'N'
    ):
        session.rollback()
        raise EvolucaoMvConflito(
            'Somente uma evolucao aberta e nao assinada pode ser descartada.'
        )

    try:
        documento = session.execute(
            text(
                """
                UPDATE DBAMV.PW_DOCUMENTO_CLINICO
                   SET TP_STATUS = 'CANCELADO',
                       DH_FECHAMENTO = SYSDATE,
                       CD_USUARIO_AUTORIZADOR = :usuario_mv
                 WHERE CD_DOCUMENTO_CLINICO = :cd_documento_clinico
                   AND TP_STATUS = 'ABERTO'
                """
            ),
            {
                'usuario_mv': configuracao.usuario_mv,
                'cd_documento_clinico': row['cd_documento_clinico'],
            },
        )
        evolucao = session.execute(
            text(
                """
                UPDATE DBAMV.PRE_MED
                   SET SN_FECHADO = 'S',
                       FL_IMPRESSO = 'S',
                       DH_IMPRESSAO = SYSDATE
                 WHERE CD_PRE_MED = :cd_pre_med
                   AND SN_FECHADO = 'N'
                   AND FL_IMPRESSO = 'N'
                """
            ),
            {'cd_pre_med': cd_pre_med},
        )
        if documento.rowcount != 1 or evolucao.rowcount != 1:
            raise EvolucaoMvConflito(
                'O MV nao confirmou o cancelamento integral do rascunho.'
            )
        session.commit()
    except Exception:
        session.rollback()
        raise

    return obter_evolucao(session, cd_pre_med)


@router.delete('/{cd_pre_med}', response_model=EvolucaoMv)
def cancelar_evolucao_mv(
    cd_pre_med: int,
    chave: ChaveIdempotencia,
    session_oracle: SessionOracle,
    session_postgres: SessionPostgres,
    usuario: UsuarioTi,
):
    _validar_escrita_habilitada()
    atual = _obter_evolucao_ou_erro(session_oracle, cd_pre_med)
    auditoria, repetida = _reservar_auditoria(
        session_postgres,
        chave,
        'cancelar',
        atual.cd_atendimento,
        usuario,
        cd_pre_med,
    )
    if repetida:
        return _obter_evolucao_ou_erro(session_oracle, cd_pre_med)
    return _executar_com_auditoria(
        session_postgres,
        auditoria,
        lambda: _cancelar_rascunho(
            session_oracle,
            cd_pre_med,
            {atual.cd_atendimento},
        ),
    )


@router.post('/{cd_pre_med}/assinar', response_model=EvolucaoMv)
def assinar_evolucao_mv(
    cd_pre_med: int,
    chave: ChaveIdempotencia,
    session_oracle: SessionOracle,
    session_postgres: SessionPostgres,
    usuario: UsuarioTi,
):
    _validar_escrita_habilitada()
    atual = _obter_evolucao_ou_erro(session_oracle, cd_pre_med)
    auditoria, repetida = _reservar_auditoria(
        session_postgres,
        chave,
        'assinar',
        atual.cd_atendimento,
        usuario,
        cd_pre_med,
    )
    if repetida:
        return _obter_evolucao_ou_erro(session_oracle, cd_pre_med)
    return _executar_com_auditoria(
        session_postgres,
        auditoria,
        lambda: assinar_evolucao(
            session_oracle,
            cd_pre_med,
            _configuracao().usuario_mv,
            {atual.cd_atendimento},
        ),
    )
