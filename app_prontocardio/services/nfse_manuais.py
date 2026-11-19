# ruff: noqa: E501
"""NFS-e de outros municípios cadastradas pelo PDF (DANFSe) na tela NFS-e do setor.

Ficam numa tabela própria (``contas_pagar_nfse_manuais``), com as mesmas
colunas que a fila usa de ``nfse_xml_tomador`` (tabela da extração do ISS
Fortaleza, gerida pelo dlt da DAG; o Receita Certa não grava nela). As
consultas da fila, dos boletos, da simulação e da importação leem
``FONTE_NFSE_RECEBIDAS``: a união das duas. Assim a nota de outro município
segue o mesmo caminho das de Fortaleza.

Ciclo: ``rascunho`` (PDF lido, aguardando conferência) -> ``ativa`` (na fila)
-> ``excluida`` (cadastro desfeito antes de ir para o MV).
"""

from decimal import Decimal

CAMPOS_NFSE = (
    'numero_nfse', 'codigo_verificacao_nfse', 'competencia_nfse', 'codigo_natureza_operacao',
    'prestador_cnpj', 'prestador_razao_social', 'prestador_nome_fantasia', 'prestador_inscricao_municipal',
    'prestador_endereco', 'prestador_numero_endereco', 'prestador_complemento_endereco', 'prestador_bairro',
    'prestador_cep', 'prestador_uf', 'prestador_email', 'prestador_telefone',
    'tomador_cnpj', 'tomador_razao_social', 'tomador_endereco', 'tomador_numero_endereco', 'tomador_bairro',
    'tomador_cep', 'tomador_uf', 'tomador_email',
    'discriminacao', 'codigo_item_lista_servico', 'codigo_cnae', 'codigo_tributacao', 'codigo_municipio_servico',
    'valor_servicos', 'valor_deducoes', 'valor_pis', 'valor_cofins', 'valor_inss', 'valor_ir', 'valor_csll',
    'outras_retencoes', 'iss_retido', 'valor_iss', 'valor_iss_retido', 'base_calculo', 'aliquota',
    'desconto_incondicionado', 'desconto_condicionado', 'valor_liquido_nfse',
)
# Só existem no XML do ISS; nas notas por PDF vêm nulas.
CAMPOS_SO_XML = ('cancelamento_data_hora', 'motivo_cancelamento_nfse', 'xml_campos', 'xml_documento')

# Campos que o usuário confere/edita na tela antes de confirmar.
CAMPOS_EDITAVEIS = (
    'numero_nfse', 'chave_acesso', 'data_emissao', 'competencia_nfse',
    'prestador_cnpj', 'prestador_razao_social', 'prestador_municipio', 'prestador_codigo_municipio', 'prestador_inscricao_municipal',
    'tomador_cnpj', 'tomador_razao_social', 'discriminacao', 'codigo_tributacao', 'codigo_municipio_servico',
    'valor_servicos', 'desconto_incondicionado', 'desconto_condicionado', 'iss_retido', 'valor_iss', 'valor_iss_retido',
    'aliquota', 'valor_ir', 'valor_csll', 'valor_inss', 'outras_retencoes', 'valor_liquido_nfse',
)
VALORES = ('valor_servicos', 'desconto_incondicionado', 'desconto_condicionado', 'valor_iss', 'valor_iss_retido',
           'valor_ir', 'valor_csll', 'valor_inss', 'outras_retencoes', 'valor_liquido_nfse')
TOLERANCIA = Decimal('0.02')

DDL_NFSE_MANUAIS = (
    f"""CREATE TABLE IF NOT EXISTS api_prontocardio.contas_pagar_nfse_manuais (
        id bigserial PRIMARY KEY,
        status varchar(12) NOT NULL CHECK (status IN ('rascunho', 'ativa', 'excluida')),
        origem_leitura varchar(12) NOT NULL CHECK (origem_leitura IN ('danfse', 'manual')),
        chave_acesso varchar(60),
        prestador_municipio varchar(120),
        prestador_codigo_municipio varchar(10),
        {', '.join(f'{c} text' for c in CAMPOS_NFSE)},
        data_hora timestamptz,
        leitura jsonb,
        nome_arquivo varchar(255) NOT NULL,
        arquivo bytea NOT NULL,
        sha256 char(64) NOT NULL,
        criado_por integer NOT NULL,
        criado_em timestamptz NOT NULL DEFAULT now(),
        confirmado_por integer,
        confirmado_em timestamptz,
        excluido_por integer,
        excluido_em timestamptz,
        motivo_exclusao text
    )""",
    # Uma NFS-e ativa por prestador + número.
    """CREATE UNIQUE INDEX IF NOT EXISTS contas_pagar_nfse_manuais_ativa
        ON api_prontocardio.contas_pagar_nfse_manuais (prestador_cnpj, numero_nfse) WHERE status = 'ativa'""",
)


def _colunas(manual: bool) -> str:
    partes = [f'{c}::text AS {c}' for c in CAMPOS_NFSE]
    partes += [(f'NULL::text AS {c}' if manual else f'{c}::text AS {c}') for c in CAMPOS_SO_XML]
    partes.append('data_hora')
    partes.append("'pdf'::text AS origem_nfse" if manual else "'iss'::text AS origem_nfse")
    partes.append('id AS nfse_manual_id' if manual else 'NULL::bigint AS nfse_manual_id')
    return ', '.join(partes)


# Use como "FROM {FONTE_NFSE_RECEBIDAS} n".
FONTE_NFSE_RECEBIDAS = (
    f"(SELECT {_colunas(False)} FROM api_prontocardio.nfse_xml_tomador"
    f" UNION ALL SELECT {_colunas(True)} FROM api_prontocardio.contas_pagar_nfse_manuais WHERE status = 'ativa')"
)


def validar(dados: dict, *, cnpjs_do_grupo: set[str], cnpj_valido) -> list[str]:
    """Erros que impedem confirmar o cadastro (a tela mostra e o usuário corrige)."""
    erros = []
    obrigatorios = {'numero_nfse': 'número da NFS-e', 'data_emissao': 'data de emissão', 'prestador_cnpj': 'CNPJ do prestador',
                    'prestador_razao_social': 'nome do prestador', 'tomador_cnpj': 'CNPJ do tomador',
                    'discriminacao': 'descrição do serviço', 'valor_servicos': 'valor do serviço', 'valor_liquido_nfse': 'valor líquido'}
    faltando = [nome for campo, nome in obrigatorios.items() if dados.get(campo) in (None, '')]
    if faltando:
        erros.append('Preencha: ' + ', '.join(faltando) + '.')
    if dados.get('prestador_cnpj') and not cnpj_valido(dados['prestador_cnpj']):
        erros.append('O CNPJ do prestador não é válido.')
    if dados.get('tomador_cnpj') and dados['tomador_cnpj'] not in cnpjs_do_grupo:
        erros.append('O tomador não é uma empresa do grupo no MV (confira o CNPJ do tomador).')
    if dados.get('prestador_cnpj') and dados.get('prestador_cnpj') == dados.get('tomador_cnpj'):
        erros.append('Prestador e tomador não podem ser o mesmo CNPJ.')
    valores = {k: Decimal(str(dados.get(k) or 0)) for k in VALORES}
    if valores['valor_servicos'] <= 0 or valores['valor_liquido_nfse'] <= 0:
        erros.append('Valor do serviço e valor líquido devem ser maiores que zero.')
    retido = (valores['valor_iss_retido'] if str(dados.get('iss_retido')) == '1' else 0) + valores['valor_ir'] + valores['valor_csll'] + valores['valor_inss'] + valores['outras_retencoes']
    esperado = valores['valor_servicos'] - valores['desconto_incondicionado'] - valores['desconto_condicionado'] - retido
    if valores['valor_servicos'] > 0 and abs(esperado - valores['valor_liquido_nfse']) > TOLERANCIA:
        erros.append(f'O valor líquido (R$ {valores["valor_liquido_nfse"]:.2f}) não fecha com serviço − descontos − retenções (R$ {esperado:.2f}).')
    if str(dados.get('iss_retido')) == '1' and valores['valor_iss_retido'] <= 0:
        erros.append('ISS marcado como retido, mas sem valor.')
    return erros
