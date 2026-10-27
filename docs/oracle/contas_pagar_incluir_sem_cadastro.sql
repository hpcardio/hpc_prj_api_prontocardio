-- Preserva os demais filtros da view; inclui fornecedores sem codigo.
CREATE OR REPLACE VIEW dbamv.HPC_V_CONTAS_A_PAGAR AS
    SELECT
        P.CD_PAGCON_PAG                                                 AS CODIGO_PAGAMENTO_PK,
        P.CD_PROCESSO                                                   AS CODIGO_DO_PROCESSO_PAGAMENTO,
        P.CD_CON_COR                                                    AS CODIGO_CONTA_CORRENTE,
        P.DT_PAGAMENTO                                                  AS DATA_DE_PAGAMENTO,
        P.VL_PAGO                                                       AS VALOR_PAGO,
        P.CD_MOEDA                                                      AS CODIGO_DA_MOEDA_PAGAMENTO,
        P.VL_MOEDA                                                      AS VALOR_MOEDA_PAGAMENTO,
        P.CD_BORDERO                                                    AS CODIGO_BORDERO,
        P.DT_BAIXA                                                      AS DATA_DA_BAIXA,
        P.TP_PAGAMENTO                                                  AS TIPO_DE_PAGAMENTO,
        P.DT_ESTORNO                                                    AS DATA_DE_ESTORNO,
        PR_PAGAMENTO.DS_PROCESSO                                        AS DESCRICAO_DO_PROCESSO_PAG,
        C.CD_CON_PAG                                                    AS CODIGO_DO_CONTAS_A_PAGAR,
        C.NR_DOCUMENTO                                                  AS NUMERO_DO_DOCUMENTO,
        C.DT_LANCAMENTO                                                 AS DATA_DE_LANCAMENTO,
        C.DT_EMISSAO                                                    AS DATA_DE_EMISSAO,
        DECODE(C.TP_VENCIMENTO, 'A', 'A Vista', 'A Prazo')              AS VENCIMENTO,
        C.DS_CON_PAG                                                    AS DESCRICAO_DA_CONTA,
        C.CD_FORNECEDOR                                                 AS CODIGO_DO_FORNECEDOR,
        C.VL_BRUTO_CONTA                                                AS VALOR_BRUTO,
        C.DS_FORNECEDOR                                                 AS NOME_FORNECEDOR,
        C.NR_SERIE                                                      AS SERIE_DA_NOTA,
        C.VL_DESCONTO                                                   AS VALOR_DESCONTO,
        C.VL_ACRESCIMO                                                  AS VALOR_ACRESCIMO,
        DECODE(
            C.TP_CON_PAG,
            'AF', 'Adiantamento Fornecedor',
            'AN', 'Adiantamento a Nao Fornecedor',
            'FU', 'Funcionario',
            'RP', 'Repasse',
            'AG', 'Agrupamento',
            'ES', 'Estoque',
            'DO', 'Documento'
        )                                                               AS TIPO_DA_CONTA,
        C.DS_OBSERVACAO                                                 AS OBSERVACAO,
        I.NR_PARCELA                                                    AS NUMERO_DA_PARCELA,
        I.VL_DUPLICATA                                                  AS VALOR_DA_DUPLICATA,
        TO_CHAR(I.DT_VENCIMENTO, 'MM/YYYY')                             AS COMP_VENCIMENTO,
        TO_CHAR(I.DT_VENCIMENTO, 'DD/MM/YYYY')                          AS DT_VENCIMENTO,
        I.DT_PREVISTA_PAG                                               AS DATA_PREVISTA_DE_PAGAMENTO,
        I.CD_ITCON_PAG                                                  AS CODIGO_PARCELA_PK,
        DECODE(
            I.TP_QUITACAO,
            'V', 'previsto',
            'C', 'comprometido',
            'P', 'parcialmente pago',
            'Q', 'quitado',
            'N', 'cancelado',
            'T', 'liquidacao total devolucao',
            'L', 'liquidacao parcial devolucao'
        )                                                               AS TIPO_DE_QUITACAO,
        CH.NR_CHEQUE                                                    AS NUMERO_DO_CHEQUE,
        CH.CD_CHEQUE                                                    AS CODIGO_DO_CHEQUE_PK,
        CH.DT_RETIRADA                                                  AS DT_RETIRADA,
        NVL(CH.NR_CHEQUE, P.CD_BORDERO)                                 AS CODIGO_DOCUMENTO_PAGAMENTO,
        D.CD_DESC_ACRES                                                 AS CODIGO_DESCONTO_ACRESCIMO_PK,
        D.VL_DESC_ACRES                                                 AS VALOR_DESCONTO_ACRESCIMO,
        DECODE(D.TP_DESC_ACRES, 'A', 'ACRESCIMO', 'DESCONTO')           AS TIPO_DESCONTO_ACRESCIMO,
        D.VL_MOEDA_DESC_ACRES                                           AS VALOR_MOEDA_DESCONTO_ACRESCIMO,
        D.DS_DESC_ACRES                                                 AS DESCRICAO_DESCONTO_ACRESCIMO,
        D.CD_PAG_DESC_ACRES                                             AS CODIGO_DESC_ACRES_PAGAMENTO_PK,
        NULL                                                            AS TIPO_DE_DOCUMENTO,
        C.CD_PROCESSO                                                   AS CODIGO_DO_PROCESSO_PROV,
        PR_PROVISIONAMENTO.DS_PROCESSO                                  AS DESCRICAO_DO_PROCESSO_PROV
    FROM DBAMV.ITCON_PAG I
    INNER JOIN DBAMV.CON_PAG C
        ON C.CD_CON_PAG = I.CD_CON_PAG
    INNER JOIN DBAMV.PROCESSO PR_PROVISIONAMENTO
        ON PR_PROVISIONAMENTO.CD_PROCESSO = C.CD_PROCESSO
    LEFT JOIN DBAMV.PAGCON_PAG P
        ON P.CD_ITCON_PAG = I.CD_ITCON_PAG
    LEFT JOIN DBAMV.PROCESSO PR_PAGAMENTO
        ON PR_PAGAMENTO.CD_PROCESSO = P.CD_PROCESSO
    LEFT JOIN DBAMV.FORNECEDOR F
        ON F.CD_FORNECEDOR = C.CD_FORNECEDOR
    LEFT JOIN DBAMV.CHEQUE CH
        ON CH.CD_CHEQUE = P.CD_CHEQUE
    LEFT JOIN DBAMV.PAG_DESC_ACRES D
        ON D.CD_PAGCON_PAG = P.CD_PAGCON_PAG
    LEFT JOIN DBAMV.ITCON_PAG IT_AGRUP
        ON IT_AGRUP.CD_CON_PAG_AGRUP = I.CD_CON_PAG
    LEFT JOIN DBAMV.PREVISAO PV
        ON PV.CD_PREVISAO = C.CD_PREVISAO
    LEFT JOIN DBAMV.ORD_COM OC
        ON OC.CD_ORD_COM = PV.CD_ORD_COM
    WHERE IT_AGRUP.CD_ITCON_PAG IS NULL
    -- Exclui titulos gerados por devolucao; preserva a conta original.
    AND C.CD_DEVOLUCAO IS NULL
    AND C.CD_CON_PAG_DEV IS NULL
    -- Cadastro especifico de devolucoes a pacientes neste ambiente.
    AND (C.CD_FORNECEDOR IS NULL OR C.CD_FORNECEDOR <> 1337)
    AND (
        OC.CD_ORD_COM IS NULL
        OR I.DT_VENCIMENTO IS NULL
        OR I.DT_VENCIMENTO >= TRUNC(SYSDATE)
    )
;
