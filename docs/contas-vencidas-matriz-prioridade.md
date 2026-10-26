# Contas vencidas: criticidade, matriz e orçamento

## Comportamento

- Cadastro administrativo passa a permitir níveis 5 (extrema), 4 (alta), 3 (moderada), 2 (baixa), 1 (não crítica), com justificativa e identificação da última classificação.
- Críticos antigos sem nível ficam pendentes de revisão: conservam prioridade equivalente ao nível 5 na fila, sem receber classificação automática. São demonstrados separadamente da matriz. Não críticos sem nível mantêm a classificação 1.
- Fila ordenada por nível decrescente, maior atraso e valor vencido. Mantidos todos os pagamentos, títulos, ações, filtros anteriores, paginação e contratos booleanos antigos.
- Filtros adicionais: nível, críticos pendentes, faixa do título e somente vencidos. A opção padrão conserva acesso aos saldos correntes existentes; cartões sem atraso ficam identificados. Faixa filtra os títulos por seus próprios vencimentos, não pelo mais antigo do fornecedor.
- Matriz 5 × 5 em Gestão, com quantidade, saldo, totais, alternância de destaque e links aos títulos do quadrante. Títulos quitados, vencendo hoje ou futuros não entram. Saldos consideram os pagamentos manuais pelo mesmo cálculo já existente.
- Na navegação do quadrante, o título é filtrado, mas os cartões do fornecedor e os indicadores gerais preservam o escopo anterior. Aviso explícito evita interpretar saldo integral do fornecedor como saldo do quadrante.
- Orçamento persistido por competência e compartilhado entre usuários. Percentuais por criticidade: 40/30/20/7/3; por atraso: 30/25/20/15/10. Rateio em centavos pelo maior resto, com desempate estável por quadrante; a soma sempre corresponde ao orçamento.
- Exemplo de R$ 350.000: Q1 = R$ 42.000, Q25 = R$ 1.050. Não existe orçamento predefinido nem redistribuição automática de sobras. Filtrar a dívida não altera o orçamento global. Consulta de mês anterior mostra o orçamento daquele mês comparado à posição atual, explicitamente identificada.
- Planejamento não executa, agenda, aprova ou registra pagamentos. Cobertura é potencial. Não é apresentado um “realizado” financeiro deduzido dos saldos.
- Acompanhamento acrescenta gráfico das posições registradas. Não interpola semanas ausentes e não soma janelas sobrepostas de novos vencidos. A base atual não oferece movimentos completos para uma reconciliação de pagamentos, renegociações e outros ajustes; o gráfico não inventa essa decomposição.
- Excel preserva as 19 primeiras colunas e acrescenta nível, classificação e faixa de atraso do título. Filtros e tratamento de textos literais são preservados.

## Repositórios e dependência

- Frontend: `hpcardio/hpc_prj_app_glosas`, branch oficial `main`, remote `hospital`.
- API: `hpcardio/hpc_prj_api_prontocardio`, branch oficial `main`, remote `hospital`.
- Feature em ambos: `feat/contas-vencidas-matriz-prioridade`.
- Desenvolvimento em worktrees locais limpos derivados do Git oficial atualizado. Não houve cópia de código de servidores nem acesso ao Railway.
- Implantar API e migração antes do frontend. A migração `20261026_061` sucede `20261015_060`.

## Banco, configuração e containers

A migração é aditiva: quatro colunas opcionais em `api_prontocardio.contas_pagar_tratamentos`, restrição de nível e nova tabela `api_prontocardio.contas_pagar_orcamentos`. Não altera valores, pagamentos, credenciais, volumes ou dados do Oracle. Não classifica automaticamente os registros críticos existentes. Mantém os dados operacionais atuais.

Nenhuma alteração de Compose, Dockerfile, variáveis, secrets, certificados ou volumes é necessária. A release depende da migração no PostgreSQL oficial; não há acesso direto ao banco pelo frontend.

Testes locais usam sessões simuladas para a API e SQLite temporário para o frontend. A migração foi gerada em modo SQL offline; isso não substitui sua validação e execução controlada no PostgreSQL oficial após aprovação. Nenhum banco de produção foi consultado ou migrado durante o desenvolvimento.

## Validação e implantação

Os testes cobrem regressão do frontend, pagamentos e exportação existentes, limites das cinco faixas, críticos legados, granularidade por título, conservação dos centavos, orçamento negativo/inválido, autenticação, acesso à tela, filtros, links dos quadrantes e falha isolada da matriz. Verificar os resultados registrados no PR.

O PR exige aprovação explícita e merge antes de qualquer deploy. Após merge, registrar os SHAs resultantes dos dois repositórios; os SHAs de feature não autorizam a implantação.

Antes da implantação, verificar no servidor o estado limpo, ausência de commits locais, remote oficial atualizado, ancestralidade dos SHAs aprovados e os metadados Compose, mounts, volumes e imagem. Interromper diante de checkout legado/sujo, código fora do Git ou bind mount de fonte. Preservar divergências com backup, diff e hashes, sem reset/clean.

Criar releases novos identificados pelos SHAs oficiais. Conferir host e banco esperados do PostgreSQL sem expor credenciais; preservar configurações externas e volumes. Criar tag de rollback da imagem atual antes de reconstruir somente API/frontend necessários. Imagens novas devem conter o SHA na tag e em `org.opencontainers.image.revision`. Atualizar symlink canônico somente depois de saúde e smoke tests aprovados.

## Rollback

Registrar antes do deploy os caminhos dos releases anteriores, tags das imagens e localização dos backups recuperáveis. Não há imagem ou backup de produção criado por esta feature antes de sua aprovação.

Em falha, restaurar imagens e releases anteriores e validar saúde, metadados, mounts e SHAs. Preferir manter a migração aditiva, compatível com o código anterior, para conservar classificações e orçamentos. O downgrade remove esses novos dados e só deve ocorrer com backup recuperável e decisão explícita; não faz parte do rollback normal da aplicação.

Somente declarar implantação concluída após confirmar saúde, SHAs Git/imagens aprovados, checkouts limpos, release canônico e configurações persistentes preservadas. Checkouts antigos só devem ser arquivados no fluxo de limpeza controlada após homologação.
