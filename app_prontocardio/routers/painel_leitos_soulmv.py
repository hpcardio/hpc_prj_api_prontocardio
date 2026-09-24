from datetime import datetime, timedelta
from decimal import Decimal
import base64
import json
import re
import threading
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app_prontocardio.database import oracle_engine, oracle_readonly_engine
from app_prontocardio.panel_cache import SingleFlightTTLCache


def fetch_all(sql: str, binds: dict | None = None) -> list[dict]:
    # Cada consulta recebe uma sessão curta do pool Oracle já configurado na
    # API institucional, evitando compartilhamento de estado entre requisições.
    with Session(oracle_engine) as session:
        rows = session.execute(text(sql), binds or {}).mappings()
        normalized_rows: list[dict] = []
        for row in rows:
            item = dict(row)
            # O driver deste ambiente devolve aliases sem aspas em minúsculas;
            # as consultas do painel usam os nomes Oracle em maiúsculas.
            item.update({str(key).upper(): value for key, value in item.items()})
            normalized_rows.append(item)
        return normalized_rows


def fetch_all_readonly(sql: str, binds: dict | None = None) -> list[dict]:
    with Session(oracle_readonly_engine) as session:
        rows = session.execute(text(sql), binds or {}).mappings()
        normalized_rows: list[dict] = []
        for row in rows:
            item = dict(row)
            item.update({str(key).upper(): value for key, value in item.items()})
            normalized_rows.append(item)
        return normalized_rows


router = APIRouter(
    prefix="/painel-leitos-soulmv",
    tags=["painel-leitos-soulmv"],
)

_cache_lock = threading.Lock()
_cached_payload: dict | None = None
_cached_at: datetime | None = None
_cache_seconds = 12
_pacs_viewer_cache: dict[tuple[int, int], tuple[datetime, str]] = {}
_pacs_viewer_cache_seconds = 180
_clinical_query_cache_seconds = 60
_clinical_query_cache = SingleFlightTTLCache[tuple, list[dict]](
    ttl_seconds=_clinical_query_cache_seconds,
    stale_seconds=_clinical_query_cache_seconds,
    wait_seconds=5,
)
PACS_BASE_URL = "https://2361prd-pacs-portal.cloudmv.com.br:433/backend"


class PacsError(RuntimeError):
    """Erro controlado de integração com o PACS."""


class MVPacsClient:
    def __init__(self, username: str, password: str, timeout_seconds: int = 30) -> None:
        self.base_url = PACS_BASE_URL
        self.timeout_seconds = timeout_seconds
        self.token: str | None = None
        self.user: dict | None = None
        self._login(username, password)

    def _post(self, resource: str, payload: dict, headers: dict | None = None) -> str:
        body = {"base64Data": base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")}
        request = Request(
            f"{self.base_url}{resource}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise PacsError("Não foi possível consultar o PACS neste momento.") from exc

    def _login(self, username: str, password: str) -> None:
        response = self._post("/User/UserService.svc/ValidarPacienteSeguro", {
            "TipoUsuario": "3", "IdUnidade": 0, "Usuario": "", "NomeUsuario": username, "Senha": password,
        })
        try:
            payload = json.loads(response)
            patient = json.loads(payload["data"])
            self.token = payload["token"]
            self.user = {"TipoUsuario": "3", "NomeUsuario": username, "IDUsuario": patient["IDUsuario"]}
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise PacsError("O PACS não autorizou a visualização deste exame.") from exc

    def _headers(self) -> dict:
        return {
            "Token-Header": self.token or "",
            "Cookie": "; ".join([f"idceToken={json.dumps(self.token)}", f"urlBaseServico={json.dumps(self.base_url)}", f"usuarioLogado={json.dumps(self.user)}"]),
        }

    def viewer_url(self, exam_id: int) -> str:
        for page in range(1, 101):
            start = ((page - 1) * 20) + 1
            response = self._post("/ExameService.svc/CarregarMaisExameSeguro", {
                "Fim": start + 20, "Qtde": 20, "Inicio": start, "Ordenacao": "0", "Paciente": {}, "Tipo": ["1", "2", "3"], "Usuario": self.user,
            }, self._headers())
            try:
                exams = json.loads(response)
            except json.JSONDecodeError as exc:
                raise PacsError("O PACS retornou uma resposta inválida.") from exc
            if not exams:
                break
            for exam in exams:
                if int(exam.get("IdLaudo") or 0) != exam_id:
                    continue
                patient = exam.get("Paciente") or {}
                url = self._post("/ExameService.svc/GetViewerURL", {
                    "idUnidade": patient.get("IdUnidade"), "acNumber": exam.get("Numero"),
                    "prontuario": self.user.get("NomeUsuario") if self.user else "",
                    "idUsuario": self.user.get("IDUsuario") if self.user else "",
                    "nomeUsuario": self.user.get("NomeUsuario") if self.user else "",
                    "TipoUsuario": "3",
                }, self._headers()).strip().strip('"')
                if url.startswith(("https://", "http://")):
                    return url
                raise PacsError("O PACS não disponibilizou o visualizador para este exame.")
        raise PacsError("Imagem não encontrada no PACS para este exame.")


SQL_LEITOS = """
select
    ui.cd_unid_int,
    ui.ds_unid_int,
    ui.ds_localizacao,
    l.cd_leito,
    l.ds_leito,
    l.ds_enfermaria,
    l.tp_ocupacao,
    l.tp_sexo as tp_sexo_leito,
    l.sn_extra,
    a.cd_atendimento,
    a.dt_atendimento,
    p.nm_paciente,
    p.tp_sexo as tp_sexo_paciente,
    p.dt_nascimento
from dbamv.leito l
join dbamv.unid_int ui
  on ui.cd_unid_int = l.cd_unid_int
left join dbamv.atendime a
  on a.cd_leito = l.cd_leito
 and a.dt_alta is null
 and nvl(a.sn_internado, 'S') = 'S'
left join dbamv.paciente p
  on p.cd_paciente = a.cd_paciente
where l.tp_situacao = 'A'
  and nvl(ui.sn_ativo, 'S') = 'S'
order by ui.ds_unid_int, l.ds_leito
"""

SQL_PACIENTE = """
select
    a.cd_atendimento,
    initcap(p.nm_paciente) as nm_paciente,
    p.tp_sexo,
    p.dt_nascimento,
    initcap(co.nm_convenio) as nm_convenio,
    a.dt_atendimento,
    a.cd_prestador,
    initcap(pr.nm_prestador) as nm_prestador,
    c.cd_cid || '-' || c.ds_cid as cid,
    initcap(l.ds_leito) as ds_leito,
    u.ds_unid_int,
    l.ds_enfermaria
from dbamv.atendime a
join dbamv.paciente p on p.cd_paciente = a.cd_paciente
left join dbamv.convenio co on co.cd_convenio = a.cd_convenio
left join dbamv.prestador pr on pr.cd_prestador = a.cd_prestador
left join dbamv.cid c on c.cd_cid = a.cd_cid
left join dbamv.leito l on l.cd_leito = a.cd_leito
left join dbamv.unid_int u on u.cd_unid_int = l.cd_unid_int
where a.cd_atendimento = :admission_id
"""

# Resolve o atendimento pela ocupação atual, para que um painel fixo em um
# leito acompanhe automaticamente o paciente que estiver nele.
SQL_ATENDIMENTO_ATUAL_POR_LEITO = """
select
    a.cd_atendimento,
    initcap(l.ds_leito) as ds_leito,
    initcap(u.ds_unid_int) as ds_unid_int
from dbamv.leito l
join dbamv.unid_int u
  on u.cd_unid_int = l.cd_unid_int
left join dbamv.atendime a
  on a.cd_leito = l.cd_leito
 and a.dt_alta is null
 and nvl(a.sn_internado, 'S') = 'S'
where (
      upper(trim(l.ds_leito)) = upper(trim(:bed_code))
   or upper(trim(l.ds_leito)) = upper(trim(:bed_code)) || ' (ISO)'
)
  and (:unit_name is null or upper(trim(u.ds_unid_int)) = upper(trim(:unit_name)))
  and l.tp_situacao = 'A'
  and nvl(u.sn_ativo, 'S') = 'S'
order by case when a.cd_atendimento is null then 1 else 0 end, a.dt_atendimento desc nulls last
fetch first 1 rows only
"""

SQL_ALERGIAS = """
select distinct
    initcap(nvl(s.ds_substancia, 'Substância não identificada')) as substancia
from dbamv.pw_doc_alergia_pac ap
left join dbamv.substancia s
  on s.cd_substancia = ap.cd_substancia
where ap.cd_paciente = (
    select cd_paciente
    from dbamv.atendime
    where cd_atendimento = :admission_id
)
  and nvl(ap.sn_ativo, 'S') = 'S'
order by substancia
"""

# Protocolos assistenciais ativos exibidos no Resumo Clínico do Soul MV.
# A consulta usa o atendimento, preserva a mensagem configurada no protocolo e
# ignora casos já finalizados ou descartados.
SQL_PROTOCOLOS_ASSISTENCIAIS = """
select
    caso.cd_caso_protocolo,
    alerta.ds_alerta_protocolo,
    alerta.ds_mensagem,
    alerta.ds_sigla_protocolo,
    caso.dt_inicio,
    caso.cd_usuario_aceitacao,
    etapa.ds_etapa
from dbamv.pw_caso_protocolo caso
join dbamv.pw_alerta_protocolo alerta
  on alerta.cd_alerta_protocolo = caso.cd_alerta_protocolo
left join dbamv.pw_etapa_protocolo etapa
  on etapa.cd_etapa_protocolo = caso.cd_etapa_protocolo
where caso.cd_atendimento = :admission_id
  and caso.dt_fim is null
  and caso.cd_usuario_finalizacao is null
  and caso.cd_usuario_descarte is null
  and nvl(alerta.sn_ativo, 'S') = 'S'
order by caso.dt_inicio desc nulls last, caso.cd_caso_protocolo desc
fetch first 50 rows only
"""

SQL_SINAIS_VITAIS = """
select
    c.cd_coleta_sinal_vital,
    c.data_coleta,
    s.cd_sinal_vital,
    s.ds_sinal_vital,
    i.valor
from dbamv.coleta_sinal_vital c
join dbamv.itcoleta_sinal_vital i
  on i.cd_coleta_sinal_vital = c.cd_coleta_sinal_vital
join dbamv.sinal_vital s
  on s.cd_sinal_vital = i.cd_sinal_vital
where c.cd_atendimento = :admission_id
  and c.data_coleta >= :start_date
  and nvl(c.sn_finalizado, 'S') = 'S'
  and nvl(i.sn_ativo, 'S') = 'S'
  and i.valor is not null
order by c.data_coleta, c.cd_coleta_sinal_vital, i.nr_ordem
fetch first 2500 rows only
"""

SQL_BALANCO_HIDRICO_RESUMO = """
select
    sum(qtd_lancamentos) as qtd_lancamentos,
    max(ultima_atualizacao) as ultima_atualizacao
from (
    select
        count(*) as qtd_lancamentos,
        max(nvl(dh_coleta, dh_registro)) as ultima_atualizacao
    from dbamv.balanco_hidrico
    where cd_atendimento = :admission_id
    union all
    select
        count(*) as qtd_lancamentos,
        max(nvl(dh_criacao, cast(dt_referencia as timestamp))) as ultima_atualizacao
    from dbamv.pw_balanco_hidrico
    where cd_atendimento = :admission_id
)
"""

SQL_BALANCO_HIDRICO_HISTORICO = """
select
    h.cd_balanco_hidrico,
    h.dt_referencia as data_referencia,
    h.dh_criacao as data_registro,
    f.cd_balanco_hidrico_fechamento,
    nvl(sum(i.qt_tip_presc), 0) as volume_total,
    count(i.cd_itbalanco_hidrico_fech) as qtd_itens
from dbamv.pw_balanco_hidrico h
left join dbamv.pw_balanco_hidrico_fechamento f
    on f.cd_balanco_hidrico = h.cd_balanco_hidrico
left join dbamv.pw_itbalanco_hidrico_fech i
    on i.cd_balanco_hidrico_fechamento = f.cd_balanco_hidrico_fechamento
where h.cd_atendimento = :admission_id
group by h.cd_balanco_hidrico, h.dt_referencia, h.dh_criacao, f.cd_balanco_hidrico_fechamento
order by h.dt_referencia desc, h.dh_criacao desc
fetch first 50 rows only
"""

SQL_BALANCO_HIDRICO_ITENS = """
select
    h.cd_balanco_hidrico,
    coalesce(nullif(trim(tp.ds_tip_presc), ''), 'Item de balanço hídrico') as descricao,
    i.qt_tip_presc as quantidade,
    up.ds_unidade as unidade
from dbamv.pw_balanco_hidrico h
join dbamv.pw_balanco_hidrico_fechamento f
    on f.cd_balanco_hidrico = h.cd_balanco_hidrico
join dbamv.pw_itbalanco_hidrico_fech i
    on i.cd_balanco_hidrico_fechamento = f.cd_balanco_hidrico_fechamento
left join dbamv.tip_presc tp
    on tp.cd_tip_presc = i.cd_tip_presc
left join dbamv.uni_pro up
    on up.cd_uni_pro = tp.cd_uni_pro
where h.cd_atendimento = :admission_id
order by h.dt_referencia desc, i.cd_itbalanco_hidrico_fech
"""

# Lançamentos horários que alimentam a grade analítica do próprio Soul MV.
# TP_CALCULO: G = ganho, P = perda; itens com SN_SOMA_TOTAL_BALANCO = N
# permanecem visíveis, mas não entram no cálculo do saldo (mesma regra do PEP).
SQL_BALANCO_HIDRICO_LANCAMENTOS = """
select
    h.cd_balanco_hidrico,
    h.dt_referencia as data_referencia,
    g.nm_grupo_balanco_hidrico as grupo,
    g.nr_ordem as ordem_grupo,
    i.ds_tip_presc as descricao,
    i.nr_ordem as ordem_item,
    i.vl_coleta as quantidade,
    i.tp_calculo as tipo_calculo,
    i.sn_soma_total_balanco as soma_no_total,
    nvl(i.dh_coleta, i.dh_registro) as data_hora
from dbamv.pw_balanco_hidrico h
join dbamv.pw_grupo_balanco_hidrico g
    on g.cd_balanco_hidrico = h.cd_balanco_hidrico
join dbamv.pw_itbalanco_hidrico i
    on i.cd_grupo_balanco_hidrico = g.cd_grupo_balanco_hidrico
where h.cd_atendimento = :admission_id
  and h.dt_referencia >= (
      select max(dt_referencia) - 30
      from dbamv.pw_balanco_hidrico
      where cd_atendimento = :admission_id
  )
order by h.dt_referencia desc, h.cd_balanco_hidrico desc, g.nr_ordem, i.nr_ordem, data_hora
"""

# Retorna os resultados do campo "Balanço" para cada dia já fechado. No Soul
# MV, G representa ganho e P/O representam perdas (incluindo diurese).
SQL_BALANCO_HIDRICO_ACUMULADO = """
select
    h.dt_referencia as data_referencia,
    sum(case when upper(trim(i.tp_calculo)) = 'G' then nvl(i.vl_coleta, 0) else 0 end)
    - sum(case when upper(trim(i.tp_calculo)) in ('P', 'O') then nvl(i.vl_coleta, 0) else 0 end) as balanco_diario
from dbamv.pw_balanco_hidrico h
join dbamv.pw_grupo_balanco_hidrico g
    on g.cd_balanco_hidrico = h.cd_balanco_hidrico
join dbamv.pw_itbalanco_hidrico i
    on i.cd_grupo_balanco_hidrico = g.cd_grupo_balanco_hidrico
where h.cd_atendimento = :admission_id
  and lower(trim(g.nm_grupo_balanco_hidrico)) = 'balanço hídrico'
  and nvl(i.sn_soma_total_balanco, 'S') <> 'N'
  and nvl(i.dh_coleta, i.dh_registro) is not null
  and exists (
      select 1
      from dbamv.pw_balanco_hidrico_fechamento f
      where f.cd_balanco_hidrico = h.cd_balanco_hidrico
  )
group by h.dt_referencia
order by h.dt_referencia
"""

SQL_EVOLUCOES = """
select
    ev.cd_pre_med,
    ev.dt_pre_med,
    ev.hr_pre_med,
    initcap(nvl(ev.nm_prestador, ev.nm_usuario)) as nm_responsavel,
    tp.nm_tip_presta as nm_tipo_profissional,
    ev.ds_evolucao
from dbamv.hpc_v_evolucao ev
left join dbamv.prestador pr
  on pr.cd_prestador = ev.cd_prestador
left join dbamv.tip_presta tp
  on tp.cd_tip_presta = pr.cd_tip_presta
where ev.cd_atendimento = :admission_id
order by ev.dt_pre_med desc, ev.hr_pre_med desc
fetch first 50 rows only
"""

SQL_PRESCRICOES = """
select
    pm.cd_pre_med,
    pm.dt_pre_med,
    pm.hr_pre_med,
    initcap(pr.nm_prestador) as nm_prestador,
    coalesce(nullif(trim(tp.ds_tip_presc), ''), nullif(trim(ip.ds_itpre_med), '')) as descricao,
    nullif(trim(ip.ds_itpre_med), '') as observacao,
    ip.qt_itpre_med as quantidade,
    up.ds_unidade as unidade,
    tf.ds_tip_fre as frequencia,
    case when upper(trim(ip.cd_tip_esq)) = 'MAV' then 'S' else 'N' end as sn_mav,
    case when nvl(ip.tp_situacao, 'N') = 'S' then 'Suspensa' else 'Ativa' end as situacao
from dbamv.pre_med pm
join dbamv.itpre_med ip on ip.cd_pre_med = pm.cd_pre_med
left join dbamv.prestador pr on pr.cd_prestador = pm.cd_prestador
left join dbamv.tip_presc tp on tp.cd_tip_presc = ip.cd_tip_presc
left join dbamv.uni_pro up on up.cd_uni_pro = ip.cd_uni_pro
left join dbamv.tip_fre tf on tf.cd_tip_fre = ip.cd_tip_fre
where pm.cd_atendimento = :admission_id
  and nvl(ip.tp_situacao, 'N') <> 'S'
  and coalesce(nullif(trim(tp.ds_tip_presc), ''), nullif(trim(ip.ds_itpre_med), '')) is not null
order by pm.dt_pre_med desc, pm.hr_pre_med desc, ip.nr_ordem
fetch first 100 rows only
"""

SQL_IMAGENS = """
select
    'imagem' as tipo,
    exa.ds_exa_rx as titulo,
    null as campo,
    coalesce(item.dt_realizado, pedido.dt_pedido) as data_laudo,
    to_char(item.cd_itped_rx) as identificador,
    null as conteudo,
    null as referencia_pacs,
    case when item.sn_realizado = 'S' then 'Disponível' else 'Pendente' end as situacao
from dbamv.ped_rx pedido
join dbamv.itped_rx item
  on item.cd_ped_rx = pedido.cd_ped_rx
join dbamv.exa_rx exa
  on exa.cd_exa_rx = item.cd_exa_rx
where pedido.cd_atendimento = :admission_id
  -- ECG e radiografias simples são exames de rotina e não compõem o
  -- acompanhamento do painel assistencial.
  and upper(exa.ds_exa_rx) not like 'ECG%'
  and upper(exa.ds_exa_rx) not like '%ELETROCARDIOGRAMA%'
  and upper(exa.ds_exa_rx) not like 'RX%'
  and upper(exa.ds_exa_rx) not like '%RAIO X%'
  and upper(exa.ds_exa_rx) not like '%RAIO-X%'
order by coalesce(item.dt_realizado, pedido.dt_pedido) desc nulls last
fetch first 30 rows only
"""

SQL_LAUDOS_LABORATORIO = """
select
    'laboratorio' as tipo,
    nm_exa_lab as titulo,
    nm_campo as campo,
    dt_laudo as data_laudo,
    to_char(id) as identificador,
    null as conteudo,
    null as referencia_pacs,
    case when ds_resultado is null then 'Pendente' else 'Disponível' end as situacao
from dbamv.v_hpc_exames_laboratorio
where cd_paciente = (
    select cd_paciente
    from dbamv.atendime
    where cd_atendimento = :admission_id
)
order by data_laudo desc nulls last
fetch first 50 rows only
"""

SQL_LAUDOS_LABORATORIO_COM_RESULTADO = SQL_LAUDOS_LABORATORIO.replace(
    "null as conteudo,",
    "substr(ds_resultado, 1, 1200) as conteudo,",
)

# Resumo enxuto para a tela inicial: evita ler todo o histórico e seus textos
# completos quando o painel só apresenta os primeiros cards laboratoriais.
SQL_LAUDOS_LABORATORIO_RESUMO = """
select
    'laboratorio' as tipo,
    nm_exa_lab as titulo,
    nm_campo as campo,
    dt_laudo as data_laudo,
    to_char(id) as identificador,
    substr(ds_resultado, 1, 220) as conteudo,
    null as referencia_pacs,
    case when ds_resultado is null then 'Pendente' else 'Disponível' end as situacao
from dbamv.v_hpc_exames_laboratorio
where cd_paciente = (
    select cd_paciente
    from dbamv.atendime
    where cd_atendimento = :admission_id
)
  and ds_resultado is not null
order by dt_laudo desc nulls last
fetch first 12 rows only
"""

SQL_HISTORICO_LABORATORIO = """
select
    nm_exa_lab as titulo,
    nm_campo as campo,
    substr(ds_resultado, 1, 500) as resultado,
    dt_laudo as data_laudo
from dbamv.v_hpc_exames_laboratorio
where cd_paciente = (
    select cd_paciente
    from dbamv.atendime
    where cd_atendimento = :admission_id
)
  and dt_laudo >= :start_date
  and ds_resultado is not null
order by dt_laudo desc nulls last, nm_exa_lab, nm_campo
fetch first 500 rows only
"""

SQL_LAUDO_LABORATORIO_DETALHE = """
select substr(ds_resultado, 1, 3500) as conteudo
from dbamv.v_hpc_exames_laboratorio
where id = :report_id
  and cd_paciente = (
      select cd_paciente
      from dbamv.atendime
      where cd_atendimento = :admission_id
  )
fetch first 1 rows only
"""

SQL_PACS_CREDENTIALS = """
select
    login.id_exame_pedido as exame_pacs,
    login.nr_prontuario_hospitalar as usuario,
    login.ds_regiao_examinada as senha
from idce.exame_pedido_multi_login login
where login.cd_atendimento_his = :admission_id
  and login.cd_item_pedido_his = :exam_id
fetch first 1 rows only
"""

SQL_LAUDO_IMAGEM_DETALHE = """
select laudo.ds_laudo as conteudo
from dbamv.itped_rx item
join dbamv.ped_rx pedido
  on pedido.cd_ped_rx = item.cd_ped_rx
left join dbamv.laudo_rx laudo
  on laudo.cd_laudo = item.cd_laudo
where pedido.cd_atendimento = :admission_id
  and item.cd_itped_rx = :exam_id
fetch first 1 rows only
"""


def _plain(value):
    if isinstance(value, Decimal):
        return int(value)
    return value


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value).strip() if value is not None else None)


def _cached_clinical_rows(cache_name: str, admission_id: int, sql: str, binds: dict | None = None, readonly: bool = False) -> list[dict]:
    cache_key = (cache_name, admission_id, tuple(sorted((binds or {}).items())))

    def load_rows() -> list[dict]:
        fetcher = fetch_all_readonly if readonly else fetch_all
        return fetcher(sql, {"admission_id": admission_id, **(binds or {})})

    return _clinical_query_cache.get_or_load(cache_key, load_rows)


def _lookback_days(periodo: str) -> int:
    return {"24h": 1, "48h": 2, "7d": 7, "all": 3650}.get(periodo, 1)


def _serialize_vital_signs(rows: list[dict]) -> list[dict]:
    signal_fields = {
        1: "temperature",
        2: "heartRate",
        3: "respiratory",
        4: "systolic",
        5: "diastolic",
        11: "saturation",
        13: "glucose",
    }
    readings: dict[str, dict[str, object]] = {}
    for row in rows:
        collected_at = row.get("DATA_COLETA")
        collection_id = str(row.get("CD_COLETA_SINAL_VITAL") or "")
        if not collection_id or not isinstance(collected_at, datetime):
            continue
        field = signal_fields.get(int(row.get("CD_SINAL_VITAL") or 0))
        if not field:
            continue
        reading = readings.setdefault(collection_id, {
            "id": collection_id,
            "date": collected_at.isoformat(),
        })
        reading[field] = float(row["VALOR"])
    return sorted(readings.values(), key=lambda reading: str(reading["date"]))


def _filter_by_period(rows: list[dict], periodo: str) -> list[dict]:
    if periodo == "all":
        return rows
    start = (datetime.now() - timedelta(days=_lookback_days(periodo))).replace(hour=0, minute=0, second=0, microsecond=0)
    return [row for row in rows if isinstance(row.get("DATA_LAUDO"), datetime) and row["DATA_LAUDO"] >= start]


def _report_content(value):
    if value is None or isinstance(value, bytes):
        return None
    text = str(value).strip()
    return text[:3500] or None


def _rtf_to_text(value):
    if value is None or isinstance(value, bytes):
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if not raw.lstrip().startswith(r"{\rtf"):
        return raw
    destinations = {
        "fonttbl", "colortbl", "stylesheet", "info", "header", "footer", "pict",
        "object", "datastore", "themedata", "xmlopen", "generator",
    }
    output: list[str] = []
    groups = [{"skip": False, "at_start": False, "starred": False}]
    index = 0

    def write(text: str) -> None:
        if not groups[-1]["skip"]:
            output.append(text)

    while index < len(raw):
        character = raw[index]
        if character == "{":
            groups.append({"skip": groups[-1]["skip"], "at_start": True, "starred": False})
            index += 1
            continue
        if character == "}":
            if len(groups) > 1:
                groups.pop()
            index += 1
            continue
        if character != "\\":
            groups[-1]["at_start"] = False
            write(character)
            index += 1
            continue

        index += 1
        if index >= len(raw):
            break
        control = raw[index]
        if control in "\\{}":
            groups[-1]["at_start"] = False
            write(control)
            index += 1
            continue
        if control == "*":
            if groups[-1]["at_start"]:
                groups[-1]["starred"] = True
            index += 1
            continue
        if control == "'" and index + 2 < len(raw):
            groups[-1]["at_start"] = False
            try:
                write(bytes.fromhex(raw[index + 1:index + 3]).decode("cp1252"))
            except (UnicodeDecodeError, ValueError):
                pass
            index += 3
            continue
        if control.isalpha():
            end = index
            while end < len(raw) and raw[end].isalpha():
                end += 1
            word = raw[index:end].lower()
            number_end = end
            if number_end < len(raw) and raw[number_end] in "+-":
                number_end += 1
            while number_end < len(raw) and raw[number_end].isdigit():
                number_end += 1
            argument = raw[end:number_end]
            if groups[-1]["at_start"] and (groups[-1]["starred"] or word in destinations):
                groups[-1]["skip"] = True
            groups[-1]["at_start"] = False
            if word in {"par", "line"}:
                write("\n")
            elif word == "tab":
                write("\t")
            elif word == "u" and argument:
                try:
                    codepoint = int(argument)
                    write(chr(codepoint if codepoint >= 0 else codepoint + 65536))
                except (ValueError, OverflowError):
                    pass
            index = number_end
            if index < len(raw) and raw[index] == " ":
                index += 1
            continue
        groups[-1]["at_start"] = False
        index += 1

    text = "".join(output).replace("\r", "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", text).strip()
    return cleaned[:12000] or None


def _serialize_evolutions(rows: list[dict]) -> list[dict]:
    def professional_type(value) -> str:
        normalized = unicodedata.normalize("NFD", str(value or "").upper())
        normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
        if "TECN" in normalized and "ENFERM" in normalized:
            return "Técnico de enfermagem"
        if "ENFERM" in normalized:
            return "Enfermeiro(a)"
        if "MEDIC" in normalized:
            return "Médico(a)"
        return "Profissional assistencial"

    return [
        {
            "id": _plain(row.get("CD_PRE_MED")),
            "date": _iso(row.get("DT_PRE_MED")),
            "time": _iso(row.get("HR_PRE_MED")),
            "author": str(row.get("NM_RESPONSAVEL") or "Não informado").strip(),
            "professionalType": professional_type(row.get("NM_TIPO_PROFISSIONAL")),
            "text": str(row.get("DS_EVOLUCAO") or "Sem descrição disponível.").strip(),
        }
        for row in rows
    ]


def _serialize_protocols(rows: list[dict]) -> list[dict]:
    """Consolida ocorrências repetidas de um mesmo alerta pelo registro mais recente."""
    protocols: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        title = str(row.get("DS_ALERTA_PROTOCOLO") or "Protocolo assistencial").strip()
        key = unicodedata.normalize("NFKD", title).casefold()
        if key in seen:
            continue
        seen.add(key)
        message = str(row.get("DS_MENSAGEM") or "").strip() or None
        stage = str(row.get("DS_ETAPA") or "").strip() or None
        protocols.append({
            "id": _plain(row.get("CD_CASO_PROTOCOLO")),
            "title": title,
            "message": message,
            "abbreviation": str(row.get("DS_SIGLA_PROTOCOLO") or "").strip() or None,
            "startedAt": _iso(row.get("DT_INICIO")),
            "acceptedBy": str(row.get("CD_USUARIO_ACEITACAO") or "").strip() or None,
            "stage": stage,
        })
    return protocols


def _serialize_reports(rows: list[dict]) -> list[dict]:
    return [
        {
            "type": str(row.get("TIPO") or "imagem").strip(),
            "title": str(row.get("TITULO") or "Exame sem identificação").strip(),
            "field": str(row.get("CAMPO") or "").strip() or None,
            "date": _iso(row.get("DATA_LAUDO")),
            "identifier": str(row.get("IDENTIFICADOR") or "").strip(),
            "status": str(row.get("SITUACAO") or "Pendente").strip(),
            "content": _rtf_to_text(row.get("CONTEUDO")) if str(row.get("TIPO") or "").strip() == "imagem" else _report_content(row.get("CONTEUDO")),
            "pacsReference": str(row.get("REFERENCIA_PACS") or "").strip() or None,
            "viewerAvailable": str(row.get("TIPO") or "").strip() == "imagem" and bool(row.get("IDENTIFICADOR")),
        }
        for row in rows
    ]


def _serialize_laboratory_history(rows: list[dict]) -> list[dict]:
    return [
        {
            "title": str(row.get("TITULO") or "Exame sem identificação").strip(),
            "field": str(row.get("CAMPO") or "Resultado").strip() or "Resultado",
            "result": _report_content(row.get("RESULTADO")),
            "reportedAt": _iso(row.get("DATA_LAUDO")),
        }
        for row in rows
        if _report_content(row.get("RESULTADO")) and row.get("DATA_LAUDO")
    ]


def _unit_id(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    ascii_value = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return "".join(char.lower() for char in ascii_value if char.isalnum())


def _initials(name: str | None) -> str | None:
    if not name:
        return None
    parts = [part for part in name.strip().split() if part]
    if not parts:
        return None
    selected = parts[:1] if len(parts) == 1 else [parts[0], parts[-1]]
    return " ".join(f"{part[0].upper()}." for part in selected)


def _age(birth_date, now: datetime) -> int | None:
    if not birth_date:
        return None
    years = now.year - birth_date.year
    if (now.month, now.day) < (birth_date.month, birth_date.day):
        years -= 1
    return max(0, years)


def _stay_days(admission_date, now: datetime) -> int | None:
    if not admission_date:
        return None
    return max(0, (now.date() - admission_date.date()).days)


def _build_payload(rows: list[dict]) -> dict:
    now = datetime.now()
    grouped: dict[str, dict] = {}

    for row in rows:
        unit_name = str(row.get("DS_UNID_INT") or "Unidade sem nome").strip()
        unit_id = _unit_id(unit_name)
        unit = grouped.setdefault(
            unit_id,
            {
                "id": unit_id,
                "name": unit_name,
                "floor": str(row.get("DS_LOCALIZACAO") or "Localização não informada").strip(),
                "beds": [],
            },
        )

        occupied = str(row.get("TP_OCUPACAO") or "V").upper() == "O"
        patient_gender = str(row.get("TP_SEXO_PACIENTE") or "").upper()
        if patient_gender not in {"M", "F"}:
            patient_gender = None

        unit["beds"].append(
            {
                "code": str(row.get("DS_LEITO") or row.get("CD_LEITO") or "").strip(),
                "status": "occupied" if occupied else "available",
                "patient": _initials(row.get("NM_PACIENTE")) if occupied else None,
                "age": _age(row.get("DT_NASCIMENTO"), now) if occupied else None,
                "stay": _stay_days(row.get("DT_ATENDIMENTO"), now) if occupied else None,
                "gender": patient_gender if occupied else None,
                "extra": str(row.get("SN_EXTRA") or "N").upper() == "S",
                "bedId": _plain(row.get("CD_LEITO")),
                "admissionId": _plain(row.get("CD_ATENDIMENTO")) if occupied else None,
            }
        )

    units = list(grouped.values())
    total = sum(len(unit["beds"]) for unit in units)
    occupied = sum(
        1 for unit in units for bed in unit["beds"] if bed["status"] == "occupied"
    )
    return {
        "source": "soulmv",
        "updatedAt": now.isoformat(),
        "totals": {"units": len(units), "beds": total, "occupied": occupied},
        "units": units,
    }


@router.get("/situacao")
def situacao_leitos():
    global _cached_payload, _cached_at
    now = datetime.now()
    with _cache_lock:
        if _cached_payload and _cached_at and (now - _cached_at).total_seconds() < _cache_seconds:
            return _cached_payload

    try:
        payload = _build_payload(fetch_all(SQL_LEITOS))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar leitos no Soul MV: {exc}") from exc

    with _cache_lock:
        _cached_payload = payload
        _cached_at = now
    return payload


@router.get("/health")
def health_leitos():
    try:
        rows = fetch_all("select count(1) as qtd from dbamv.leito where tp_situacao = 'A'")
        return {
            "ok": True,
            "source": "soulmv",
            "activeBeds": _plain(rows[0].get("QTD") or rows[0].get("qtd") or 0),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha na conexão com o Soul MV: {exc}") from exc


@router.get("/painel-por-leito")
def painel_por_leito(leito: str, unidade: str | None = None):
    """Retorna o paciente que ocupa agora o leito informado.

    O endpoint também responde explicitamente quando o leito está livre, sem
    reutilizar dados do último atendimento daquele local.
    """
    bed_code = str(leito or "").strip()
    unit_name = str(unidade or "").strip() or None
    if not bed_code:
        raise HTTPException(status_code=400, detail="Informe o leito para carregar o painel.")
    try:
        rows = fetch_all(SQL_ATENDIMENTO_ATUAL_POR_LEITO, {
            "bed_code": bed_code,
            "unit_name": unit_name,
        })
        if not rows:
            raise HTTPException(status_code=404, detail="Leito não encontrado na unidade informada.")
        row = rows[0]
        current_bed = str(row.get("DS_LEITO") or bed_code).strip()
        current_unit = str(row.get("DS_UNID_INT") or unit_name or "Não informado").strip()
        admission_id = _plain(row.get("CD_ATENDIMENTO"))
        if not admission_id:
            return {
                "source": "soulmv",
                "updatedAt": datetime.now().isoformat(),
                "bed": {"code": current_bed, "unit": current_unit, "occupied": False},
                "patient": None,
            }
        patient_payload = paciente(int(admission_id))
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "bed": {"code": current_bed, "unit": current_unit, "occupied": True},
            "patient": patient_payload["patient"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar ocupação do leito no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}")
def paciente(admission_id: int):
    try:
        rows = fetch_all(SQL_PACIENTE, {"admission_id": admission_id})
        if not rows:
            raise HTTPException(status_code=404, detail="Atendimento ativo não encontrado no Soul MV.")
        row = rows[0]
        allergies = fetch_all(SQL_ALERGIAS, {"admission_id": admission_id})
        now = datetime.now()
        return {
            "source": "soulmv",
            "updatedAt": now.isoformat(),
            "patient": {
                "admissionId": _plain(row.get("CD_ATENDIMENTO")),
                "name": str(row.get("NM_PACIENTE") or "Paciente não identificado").strip(),
                "gender": str(row.get("TP_SEXO") or "Não informado").strip(),
                "birthDate": row.get("DT_NASCIMENTO"),
                "convenio": str(row.get("NM_CONVENIO") or "Não informado").strip(),
                "admissionDate": row.get("DT_ATENDIMENTO"),
                "provider": str(row.get("NM_PRESTADOR") or "Não informado").strip(),
                "diagnosis": str(row.get("CID") or "Não informado").strip(),
                "bed": str(row.get("DS_LEITO") or "Não informado").strip(),
                "unit": str(row.get("DS_UNID_INT") or "Não informado").strip(),
                "ward": str(row.get("DS_ENFERMARIA") or "").strip(),
                "allergies": [
                    str(allergy.get("SUBSTANCIA") or "").strip()
                    for allergy in allergies
                    if str(allergy.get("SUBSTANCIA") or "").strip()
                ],
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar o paciente no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/protocolos")
def protocolos_assistenciais(admission_id: int):
    try:
        rows = _cached_clinical_rows("protocolos-assistenciais", admission_id, SQL_PROTOCOLOS_ASSISTENCIAIS)
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "protocols": _serialize_protocols(rows),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar protocolos assistenciais no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/sinais-vitais")
def sinais_vitais(admission_id: int, periodo: str = "24h"):
    if periodo not in {"24h", "48h", "7d", "all"}:
        raise HTTPException(status_code=400, detail="Período inválido.")
    try:
        start_date = datetime.now() - timedelta(days=_lookback_days(periodo))
        rows = _cached_clinical_rows(
            "sinais-vitais",
            admission_id,
            SQL_SINAIS_VITAIS,
            {"start_date": start_date},
        )
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "readings": _serialize_vital_signs(rows),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar sinais vitais no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/registros-clinicos")
def registros_clinicos(admission_id: int):
    try:
        evolucoes = fetch_all(SQL_EVOLUCOES, {"admission_id": admission_id})
        imagens = _cached_clinical_rows("imagens", admission_id, SQL_IMAGENS)
        laudos_laboratorio = _cached_clinical_rows("laboratorios", admission_id, SQL_LAUDOS_LABORATORIO, readonly=True)
        laudos_imagens = imagens + laudos_laboratorio
        laudos_imagens.sort(key=lambda row: row.get("DATA_LAUDO") or datetime.min, reverse=True)
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "evolutions": _serialize_evolutions(evolucoes),
            "reports": _serialize_reports(laudos_imagens),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar registros clínicos no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/evolucoes")
def evolucoes_clinicas(admission_id: int):
    try:
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "evolutions": _serialize_evolutions(fetch_all(SQL_EVOLUCOES, {"admission_id": admission_id})),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar evoluções no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/prescricoes")
def prescricoes_clinicas(admission_id: int):
    try:
        rows = fetch_all(SQL_PRESCRICOES, {"admission_id": admission_id})
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "prescriptions": [
                {
                    "id": _plain(row.get("CD_PRE_MED")), "date": _iso(row.get("DT_PRE_MED")),
                    "time": _iso(row.get("HR_PRE_MED")), "provider": str(row.get("NM_PRESTADOR") or "Não informado").strip(),
                    "description": str(row.get("DESCRICAO") or "Item sem descrição").strip(),
                    "observation": str(row.get("OBSERVACAO") or "").strip() or None,
                    "quantity": str(row.get("QUANTIDADE") or "—"), "unit": str(row.get("UNIDADE") or "—").strip(),
                    "frequency": str(row.get("FREQUENCIA") or "—").strip(), "status": str(row.get("SITUACAO") or "Ativa").strip(),
                    "isMav": str(row.get("SN_MAV") or "N").strip().upper() == "S",
                } for row in rows
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar prescrições no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/laudos-imagens")
def laudos_imagens_clinicos(admission_id: int, incluir_laboratoriais: bool = True, incluir_imagens: bool = True, incluir_resultados: bool = False, resumo: bool = False, periodo: str = "24h"):
    try:
        all_images = _cached_clinical_rows("imagens", admission_id, SQL_IMAGENS) if incluir_imagens else []
        imagens = _filter_by_period(all_images, periodo) if incluir_imagens else []
        images_outside_period = bool(incluir_imagens and not imagens and all_images and periodo != "all")
        if images_outside_period:
            imagens = all_images[:12]
        laboratory_sql = SQL_LAUDOS_LABORATORIO_RESUMO if resumo and incluir_resultados else (SQL_LAUDOS_LABORATORIO_COM_RESULTADO if incluir_resultados else SQL_LAUDOS_LABORATORIO)
        cache_name = "laboratorios-resumo" if resumo and incluir_resultados else ("laboratorios-com-resultado" if incluir_resultados else "laboratorios")
        laboratorios = _filter_by_period(_cached_clinical_rows(cache_name, admission_id, laboratory_sql, readonly=True), periodo) if incluir_laboratoriais else []
        reports = imagens + laboratorios
        reports.sort(key=lambda row: row.get("DATA_LAUDO") or datetime.min, reverse=True)
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "reports": _serialize_reports(reports),
            "imagesOutsidePeriod": images_outside_period,
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar laudos e imagens no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/laboratorio-historico")
def historico_laboratorio(admission_id: int, periodo: str = "7d"):
    if periodo not in {"24h", "48h", "7d", "all"}:
        raise HTTPException(status_code=400, detail="Período inválido.")
    try:
        lookback = _lookback_days(periodo)
        start_date = datetime.now() - timedelta(days=lookback)
        rows = _cached_clinical_rows(
            "historico-laboratorio",
            admission_id,
            SQL_HISTORICO_LABORATORIO,
            {"start_date": start_date},
            readonly=True,
        )
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "results": _serialize_laboratory_history(rows),
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar histórico laboratorial no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/laboratorio/{report_id}")
def laudo_laboratorio_detalhe(admission_id: int, report_id: int):
    try:
        rows = fetch_all_readonly(SQL_LAUDO_LABORATORIO_DETALHE, {"admission_id": admission_id, "report_id": report_id})
        if not rows:
            raise HTTPException(status_code=404, detail="Resultado laboratorial não encontrado para este atendimento.")
        return {"source": "soulmv", "content": _report_content(rows[0].get("CONTEUDO"))}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar o resultado laboratorial no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/balanco-hidrico")
def balanco_hidrico(admission_id: int):
    try:
        rows = fetch_all(SQL_BALANCO_HIDRICO_RESUMO, {"admission_id": admission_id})
        row = rows[0] if rows else {}
        records = int(row.get("QTD_LANCAMENTOS") or 0)
        history = fetch_all(SQL_BALANCO_HIDRICO_HISTORICO, {"admission_id": admission_id})
        detail_rows = fetch_all(SQL_BALANCO_HIDRICO_ITENS, {"admission_id": admission_id})
        lancamentos = fetch_all(SQL_BALANCO_HIDRICO_LANCAMENTOS, {"admission_id": admission_id})
        accumulated_rows = fetch_all(SQL_BALANCO_HIDRICO_ACUMULADO, {"admission_id": admission_id})
        accumulated_net = sum(float(item.get("BALANCO_DIARIO") or 0) for item in accumulated_rows)
        details_by_balance: dict[str, list[dict[str, object]]] = {}
        for detail in detail_rows:
            balance_id = str(detail.get("CD_BALANCO_HIDRICO") or "")
            details_by_balance.setdefault(balance_id, []).append({
                "label": str(detail.get("DESCRICAO") or "Item de balanço hídrico").strip(),
                "amount": float(detail.get("QUANTIDADE") or 0),
                "unit": str(detail.get("UNIDADE") or "").strip() or None,
            })

        # Consolida somente registros que vêm do lançamento horário do PEP. Não há
        # inferência: tanto a hora quanto ganho/perda são fornecidos pelo Soul MV.
        timelines_by_balance: dict[str, dict[str, object]] = {}
        for lancamento in lancamentos:
            balance_id = str(lancamento.get("CD_BALANCO_HIDRICO") or "")
            if not balance_id:
                continue
            timeline = timelines_by_balance.setdefault(balance_id, {
                "referenceDate": _iso(lancamento.get("DATA_REFERENCIA")),
                "groups": {},
                "hours": {},
            })
            group_name = str(lancamento.get("GRUPO") or "Balanço hídrico").strip()
            group = timeline["groups"].setdefault(group_name, {
                "name": group_name,
                "order": int(lancamento.get("ORDEM_GRUPO") or 0),
                "items": {},
            })
            item_name = str(lancamento.get("DESCRICAO") or "Item de balanço hídrico").strip()
            item = group["items"].setdefault(item_name, {
                "label": item_name,
                "order": int(lancamento.get("ORDEM_ITEM") or 0),
                "hours": {},
                "total": 0.0,
                "includedInBalance": str(lancamento.get("SOMA_NO_TOTAL") or "").upper() != "N",
                "calculationType": str(lancamento.get("TIPO_CALCULO") or "").upper() or None,
            })
            amount = float(lancamento.get("QUANTIDADE") or 0)
            moment = lancamento.get("DATA_HORA")
            hour = str(moment.hour).zfill(2) if isinstance(moment, datetime) else "--"
            item["hours"][hour] = float(item["hours"].get(hour, 0)) + amount
            item["total"] = float(item["total"]) + amount

            if group_name.casefold() == "balanço hídrico" and item["includedInBalance"] and hour != "--":
                hour_summary = timeline["hours"].setdefault(hour, {"hour": hour, "gains": 0.0, "losses": 0.0})
                if item["calculationType"] == "G":
                    hour_summary["gains"] += amount
                elif item["calculationType"] == "P":
                    hour_summary["losses"] += amount

        timeline_payload_by_balance: dict[str, dict[str, object]] = {}
        for balance_id, timeline in timelines_by_balance.items():
            groups = []
            for group in sorted(timeline["groups"].values(), key=lambda value: (value["order"], value["name"])):
                items = [
                    {
                        "label": item["label"],
                        "hours": [{"hour": hour, "amount": amount} for hour, amount in sorted(item["hours"].items())],
                        "total": item["total"],
                        "includedInBalance": item["includedInBalance"],
                        "calculationType": item["calculationType"],
                    }
                    for item in sorted(group["items"].values(), key=lambda value: (value["order"], value["label"]))
                ]
                groups.append({"name": group["name"], "items": items})
            hourly = []
            for hour, summary in sorted(timeline["hours"].items()):
                gains = float(summary["gains"])
                losses = float(summary["losses"])
                hourly.append({"hour": hour, "gains": gains, "losses": losses, "net": gains - losses})
            timeline_payload_by_balance[balance_id] = {
                "referenceDate": timeline["referenceDate"],
                "groups": groups,
                "hours": hourly,
                "gains": sum(item["gains"] for item in hourly),
                "losses": sum(item["losses"] for item in hourly),
                "net": sum(item["net"] for item in hourly),
            }
        return {
            "source": "soulmv",
            "updatedAt": datetime.now().isoformat(),
            "balance": {
                "hasData": records > 0,
                "records": records,
                "lastUpdatedAt": _iso(row.get("ULTIMA_ATUALIZACAO")),
                "accumulated": {
                    "net": accumulated_net,
                    "days": len(accumulated_rows),
                },
                "history": [
                    {
                        "id": _plain(item.get("CD_BALANCO_HIDRICO")),
                        "date": _iso(item.get("DATA_REFERENCIA")),
                        "registeredAt": _iso(item.get("DATA_REGISTRO")),
                        "isClosed": bool(item.get("CD_BALANCO_HIDRICO_FECHAMENTO")),
                        "totalVolume": float(item.get("VOLUME_TOTAL") or 0),
                        "items": details_by_balance.get(str(item.get("CD_BALANCO_HIDRICO") or ""), []),
                        "timeline": timeline_payload_by_balance.get(str(item.get("CD_BALANCO_HIDRICO") or "")),
                    }
                    for item in history
                ],
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar o balanço hídrico no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/laudos-imagens/{exam_id}/laudo")
def laudo_imagem_detalhe(admission_id: int, exam_id: int):
    try:
        rows = fetch_all(SQL_LAUDO_IMAGEM_DETALHE, {"admission_id": admission_id, "exam_id": exam_id})
        content = _report_content(rows[0].get("CONTEUDO")) if rows else None
        return {"source": "soulmv", "content": content}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Falha ao consultar o laudo do exame no Soul MV: {exc}") from exc


@router.get("/paciente/{admission_id}/laudos-imagens/{exam_id}/visualizador")
def visualizador_pacs(admission_id: int, exam_id: int):
    try:
        cache_key = (admission_id, exam_id)
        now = datetime.now()
        with _cache_lock:
            cached = _pacs_viewer_cache.get(cache_key)
            if cached and (now - cached[0]).total_seconds() < _pacs_viewer_cache_seconds:
                return {"viewerUrl": cached[1], "cached": True}
        rows = fetch_all(SQL_PACS_CREDENTIALS, {"admission_id": admission_id, "exam_id": exam_id})
        if not rows:
            raise HTTPException(status_code=404, detail="Exame não está disponível para visualização no PACS.")
        credentials = rows[0]
        viewer_url = MVPacsClient(
            str(credentials.get("USUARIO") or ""),
            str(credentials.get("SENHA") or ""),
        ).viewer_url(int(credentials.get("EXAME_PACS") or exam_id))
        with _cache_lock:
            _pacs_viewer_cache[cache_key] = (now, viewer_url)
        return {"viewerUrl": viewer_url, "cached": False}
    except HTTPException:
        raise
    except PacsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Falha ao preparar o visualizador do PACS.") from exc
