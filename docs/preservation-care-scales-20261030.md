# Divergência detectada durante o preflight Petrobras — 30/10/2026

Base oficial: `b2a7864c49bc150d2f18a13f56c13dd8fafb553e`, PR #175 mergeado.
Frontend aprovado: `db741f6f72b721f57c3ab47328d3bf3d7a4c5233`.

Durante a preparação, a API principal foi recriada por outra execução, às
13:57:10 UTC, usando novamente o Compose do checkout legado. Não foi uma troca
realizada por esta implantação. A imagem mudou de `9bf651e…` para `270470a…`.
A comparação de todos os fontes Python identificou mudanças em três arquivos:
WhatsApp voltou a uma versão anterior; o painel ganhou escalas assistenciais.

Este PR incorpora apenas a funcionalidade nova do painel, comparada
semanticamente e implementada em branch local a partir da main oficial:
`GET /painel-leitos-soulmv/paciente/{admission_id}/escalas-assistenciais`.
Mantém o período de sete dias, filtros de avaliação e projeção de resultados.
O erro HTTP não revela detalhes internos da consulta. Petrobras, NPR, contexto
MV, recibos e demais correções aprovadas em #175 permanecem preservados.

Validação: 202 testes locais passaram, incluindo três cenários novos de escalas;
lint do teste e das linhas alteradas, sintaxe Python 3.12 e diff-check aprovados.
Nenhuma consulta clínica real, mensagem ou recurso foi enviado nesses testes.

## Estado da implantação

As imagens dos SHAs aprovados foram construídas em releases limpos obtidos do
Git oficial, mas NÃO implantadas, pois removeriam a rota recém-adicionada.
Após merge deste PR, reconstruir a API com o novo SHA; nunca usar a imagem
anterior de b2a7864 para substituir essa funcionalidade.

O PostgreSQL oficial foi confirmado em `187.127.6.75`, banco `prontocardio_db`,
versão 18.4. Backup completo em formato custom, 183.492.566 bytes, catálogo
validado com 619 entradas, SHA-256:
`0f69b28e589000be6efad44fda8eca8efb010f32750c8322c4a8ce0eac0a454a`.
Diretório protegido no servdev2:
`/home/admpronto/apps/receitacerta-deploy-audits/petrobras-20261030-b2a7864`.
Arquivo: `prontocardio_db.before.dump`; validação: `backup.json` e `backup.catalog`.
A validação realizada é do catálogo; não se declara ensaio de restauração total.

O banco mantém o marcador Alembic legado `20260921_060`, ausente na cadeia
atual, embora já tenha as tabelas de prioridade/classificação de Contas a Pagar.
As tabelas Petrobras ainda não existem. Não executar `upgrade head` nem alterar
esse marcador cegamente; verificar/aplicar a migração aditiva aprovada de forma
controlada, mantendo evidência e histórico. Migrações automáticas continuam
`false`. Nenhuma migração foi aplicada nesta etapa.

O mesmo diretório contém Compose atual, diff/status do checkout e arquivo
recuperável dos fontes divergentes. Rollback da imagem atual:
`receitacerta-rollback:petrobras-20261030-main-current`.
Não foi preciso restaurar serviço, pois nenhum container foi substituído.

Após aprovação e merge: renovar preflight e backup se necessário, validar
configurações/volumes externos, migrar, implantar API/frontend, verificar saúde,
SHA e telas e só então atualizar symlinks. Preservar os checkouts divergentes;
não usar reset/clean nem desenvolver em produção. Uma alteração concorrente
posterior exige nova comparação, não sobrescrita automática.
