# Preservação da API principal antes do release Petrobras

Base oficial: `c9288215e4a8165f188a681c09f2d86ba3683974` (PR #174).
O proxy encaminha Follow-Up/Petrobras à API principal; a API dedicada de
Contas a Pagar não recebe essas rotas. A imagem principal atual não tem
label de revisão e contém correções ainda não presentes no Git.

## Comparação semântica e escopo

- Evolução SADT: conservar contexto assinado por atendimento e profissional,
  validade de cinco minutos, verificação de autoria nas escritas/formulários,
  identificação do médico e autoenrollment desabilitado por padrão.
- Recibos WhatsApp: conservar os sete parâmetros de corpo e os campos
  `parametro_1` a `parametro_7`. Aceitar também o array JSON `parametros`;
  rejeitar entradas conflitantes/incompletas. Manter a rota genérica de mídia,
  JPEG/PNG e demais funcionalidades existentes no Git oficial.
- Painel de leitos: conservar consulta NPR, excluindo frequência LIVRE e
  respeitando regras de geração/aprazamento presentes no runtime.
- Endurecimentos na reconciliação: verificar contexto/autoria também no replay
  idempotente de criação; erros de recibo/NPR não registram detalhes externos.
- Repasse Médico: diferenças de formatação e acesso equivalente a `dt_laudo`;
  nenhuma alteração necessária. Não incorporar o PR #117 antigo.
- Versões antigas de configurações/schemas/agenda/BIQ/financeiro/glosas/serviços
  do runtime correspondem a objetos ancestrais de `hospital/main`. Manter a
  versão oficial mais recente. Conservar as permissões e Contas a Pagar de #174.
- Os arquivos Python alterados no checkout legado foram comparados por hash
  com o container principal: todos os arquivos de aplicação coincidem.
- Mais 62 arquivos de código não Python e migrações do container foram
  conferidos: todos pertencem ao histórico oficial.

## Validação local

199 testes de regressão: contexto/autorizações, formulários, NPR, WhatsApp,
login, usuários, Contas a Pagar, Petrobras, identidade/performance Follow-Up,
glosas e recursos. Somente fixtures locais, SQLite e serviços simulados;
nenhuma mensagem, evolução clínica ou recurso real enviado.
Lint nas linhas alteradas e arquivos novos, sintaxe Python 3.12 e diff-check.
Não se declara lint global limpo: existem pendências anteriores em arquivos
legados fora deste escopo.

## Configuração, banco e implantação

Este PR não adiciona migração nem faz deploy. Preservar os segredos externos
`EVOLUCAO_SADT_CONTEXT_SECRET` e `EVOLUCAO_SADT_API_KEY`, o verificador de
sessão/dispositivo MV e volumes existentes. O novo contexto falha fechado
sem configuração. Não ativar automaticamente enrollment ou transmissão TISS.
As migrações Petrobras já mergeadas continuam dependendo de validação do
PostgreSQL oficial, backup e release canônico limpo antes da implantação.

Aguardar aprovação e merge deste PR; obter o release somente do Git oficial.
Preservar imagens atuais, Compose e volumes, validar saúde/SHA/banco e só então
mudar o symlink. Em falha, restaurar imagem e Compose anteriores; não descartar
checkouts divergentes nem reverter automaticamente migrações com dados.

Backups já preservados:
- servdev2: `/home/admpronto/apps/receitacerta-deploy-audits/petrobras-20261029T194559Z`
- servdev3: `/home/admpronto/apps/receitacerta-deploy-audits/petrobras-20261029T194630Z`
- rollback API principal: `receitacerta-rollback:petrobras-20261029t194559z-0`
- rollback API payables: `receitacerta-rollback:petrobras-20261029t194559z-1`
- rollback frontend: `receitacerta-rollback:petrobras-20261029t194630z-frontend`
