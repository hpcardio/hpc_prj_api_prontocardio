"""Read-only MV supplier position. Company comes from the original CON_PAG."""
from datetime import date

from sqlalchemy import text

from app_prontocardio.services.payables_mv import read_payables_rows
from app_prontocardio.services.supplier_updates import aggregate_position

SQL = text('''
SELECT cp.cd_multi_empresa AS company, me.ds_multi_empresa AS company_name,
       v.codigo_do_fornecedor AS supplier, v.nome_fornecedor AS name,
       f.nr_cgc_cpf AS document, v.codigo_do_contas_a_pagar AS account,
       v.codigo_parcela_pk AS parcel, v.numero_do_documento AS invoice,
       v.numero_da_parcela AS installment, TO_DATE(v.dt_vencimento, 'DD/MM/YYYY') AS due,
       NVL(v.valor_da_duplicata, 0) AS amount, v.tipo_de_quitacao AS status,
       v.codigo_pagamento_pk AS payment, NVL(v.valor_pago, 0) AS paid,
       v.data_de_pagamento AS paid_at, v.data_de_estorno AS reversed_at,
       v.classificacao AS classification, v.observacao AS observations
  FROM dbamv.HPC_V_CONTAS_A_PAGAR v
  JOIN dbamv.con_pag cp ON cp.cd_con_pag = v.codigo_do_contas_a_pagar
  JOIN dbamv.multi_empresas me ON me.cd_multi_empresa = cp.cd_multi_empresa
  LEFT JOIN dbamv.fornecedor f ON f.cd_fornecedor = v.codigo_do_fornecedor
 WHERE (:company IS NULL OR cp.cd_multi_empresa = :company)
   AND (:supplier IS NULL OR v.codigo_do_fornecedor = :supplier)
 ORDER BY cp.cd_multi_empresa, v.codigo_do_fornecedor, v.codigo_parcela_pk
''').execution_options(prontoglosa_fetch_rows=1000)


def read_position(oracle, company=None, supplier=None, start=None, end=None):
    # For synthetic identifiers, filter after grouping using account/name identity.
    numeric_supplier = int(supplier) if supplier and str(supplier).isdigit() else None
    rows = read_payables_rows(oracle, SQL, {'company': company, 'supplier': numeric_supplier})
    result = aggregate_position(rows, start or date(date.today().year, 1, 1), end or date.today())
    return [r for r in result if supplier is None or r['supplier'] == str(supplier)]
