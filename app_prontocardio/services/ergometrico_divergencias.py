"""Read-only reconciliation of billed ergometry without a signed report."""

from sqlalchemy import text

# Billing codes intentionally exclude 40101010; exam orders include it.
BILLING_CODES = (
    '40101037', '40101045', '20101056', '18000002',
    '70051021', '22051014', '10210004', '20010028',
)
EXAM_CODES = ('40101010',) + BILLING_CODES


def _sql_codes(codes):
    return ', '.join("'" + code + "'" for code in codes)


CONSULTA_DIVERGENCIAS = text(f"""
WITH billed AS (
    SELECT rc.cd_atendimento,
           TRUNC(r.dt_competencia, 'MM') AS competencia,
           MAX(rc.cd_repasse_consolidado) AS repasse,
           MAX(rc.cd_remessa) KEEP (
               DENSE_RANK LAST ORDER BY rc.cd_repasse_consolidado
           ) AS remessa,
           MAX(NVL(rp.cd_prestador_repasse, rp.cd_prestador)) KEEP (
               DENSE_RANK LAST ORDER BY rc.cd_repasse_consolidado
           ) AS prestador
      FROM dbamv.repasse_consolidado rc
      JOIN dbamv.repasse r ON r.cd_repasse = rc.cd_repasse
      JOIN dbamv.repasse_prestador rp
        ON rp.cd_repasse = rc.cd_repasse
       AND rp.cd_prestador = rc.cd_prestador
     WHERE r.dt_competencia >= :data_inicio
       AND r.dt_competencia < :data_fim
       AND NVL(rc.cd_procedimento, rc.cd_pro_fat)
           IN ({_sql_codes(BILLING_CODES)})
     GROUP BY rc.cd_atendimento, TRUNC(r.dt_competencia, 'MM')
), missing_report AS (
    SELECT DISTINCT b.*, a.hr_atendimento AS atendimento_em,
           a.cd_paciente, p.nm_paciente, pr.nm_prestador,
           pr.ds_codigo_conselho AS crm,
           ex.exa_rx_cd_pro_fat AS procedimento, pf.ds_pro_fat
      FROM billed b
      JOIN dbamv.atendime a ON a.cd_atendimento = b.cd_atendimento
      JOIN dbamv.ped_rx pedido ON pedido.cd_atendimento = a.cd_atendimento
      JOIN dbamv.itped_rx item ON item.cd_ped_rx = pedido.cd_ped_rx
      JOIN dbamv.exa_rx ex ON ex.cd_exa_rx = item.cd_exa_rx
      LEFT JOIN dbamv.laudo_rx laudo ON laudo.cd_laudo = item.cd_laudo
      LEFT JOIN dbamv.paciente p ON p.cd_paciente = a.cd_paciente
      LEFT JOIN dbamv.prestador pr ON pr.cd_prestador = b.prestador
      LEFT JOIN dbamv.pro_fat pf ON pf.cd_pro_fat = ex.exa_rx_cd_pro_fat
     WHERE ex.exa_rx_cd_pro_fat IN ({_sql_codes(EXAM_CODES)})
       AND NVL(laudo.cd_prestador_assinatura, laudo.cd_prestador) IS NULL
), candidates AS (
    SELECT m.cd_atendimento AS original,
           a.cd_atendimento, a.hr_atendimento, l.cd_laudo,
           ROW_NUMBER() OVER (
               PARTITION BY m.cd_atendimento
               ORDER BY ABS(a.hr_atendimento - m.atendimento_em),
                        a.cd_atendimento
           ) AS position
      FROM missing_report m
      JOIN dbamv.atendime a ON a.cd_paciente = m.cd_paciente
       AND a.cd_atendimento <> m.cd_atendimento
       AND ABS(a.hr_atendimento - m.atendimento_em) <= 7
      JOIN dbamv.ped_rx p ON p.cd_atendimento = a.cd_atendimento
      JOIN dbamv.itped_rx i ON i.cd_ped_rx = p.cd_ped_rx
      JOIN dbamv.exa_rx e ON e.cd_exa_rx = i.cd_exa_rx
      JOIN dbamv.laudo_rx l ON l.cd_laudo = i.cd_laudo
     WHERE e.exa_rx_cd_pro_fat IN ({_sql_codes(EXAM_CODES)})
       AND NVL(l.cd_prestador_assinatura, l.cd_prestador) IS NOT NULL
)
SELECT m.cd_atendimento, m.atendimento_em AS dt_atendimento,
       m.nm_paciente, m.prestador AS cd_prestador, m.nm_prestador, m.crm,
       m.repasse AS cd_repasse_consolidado, m.remessa AS cd_remessa,
       m.competencia AS competencia_faturamento,
       m.procedimento AS cd_pro_fat, m.ds_pro_fat,
       'faturado_sem_laudo' AS motivo_divergencia,
       c.cd_atendimento AS possivel_cd_atendimento,
       c.hr_atendimento AS possivel_dt_atendimento,
       c.cd_laudo AS possivel_cd_laudo
  FROM missing_report m
  LEFT JOIN candidates c ON c.original = m.cd_atendimento AND c.position = 1
 ORDER BY m.nm_prestador, m.nm_paciente, m.cd_atendimento
""")
