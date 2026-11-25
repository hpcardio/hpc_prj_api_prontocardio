import os
import json
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from evolucao_sadt_backend.mv_context_token import MvContextClaims


ExamCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16)]


class DraftData(BaseModel):
    model_config = ConfigDict(extra='forbid')

    exames: list[ExamCode] = Field(max_length=200)
    justificativa: str = Field(max_length=8000)
    formato: Literal['ans', 'sadt', 'ipm', 'issec', 'funsa', 'fusex', 'fusma']
    carater: Literal['eletivo', 'urgencia']
    linha_codigo: str = Field(max_length=40)
    linha_nome: str = Field(max_length=200)
    ipm_finalidade: Literal['procedimentos', 'nova_internacao']
    ipm_opme: bool
    ipm_opme_descricao: str = Field(max_length=8000)


class DraftInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    revisao: int = Field(ge=0)
    id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    dados: DraftData


class DraftRecord(DraftInput):
    id: str
    operacoes: dict[str, Literal['conferir']] = Field(default_factory=dict)


@contextmanager
def _database():
    default = Path(tempfile.gettempdir()) / 'evolucao_sadt_data' if os.name == 'nt' else Path('/app/evolucao_sadt_data')
    directory = Path(os.getenv('EVOLUCAO_SADT_DATA_DIR', str(default)))
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / 'sadt_drafts.sqlite3', timeout=15)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute("""
            CREATE TABLE IF NOT EXISTS sadt_drafts (
                atendimento INTEGER NOT NULL,
                paciente INTEGER NOT NULL,
                usuario TEXT NOT NULL,
                prestador INTEGER NOT NULL,
                id TEXT PRIMARY KEY NOT NULL,
                revisao INTEGER NOT NULL,
                dados TEXT NOT NULL,
                encerrado INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS sadt_active_draft_owner
            ON sadt_drafts(atendimento, paciente, usuario, prestador) WHERE encerrado = 0
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS sadt_draft_operations (
                draft_id TEXT NOT NULL REFERENCES sadt_drafts(id),
                kind TEXT NOT NULL,
                reference TEXT,
                PRIMARY KEY(draft_id, kind)
            )
        """)
        yield db


_KEY = 'atendimento = ? AND paciente = ? AND usuario = ? AND prestador = ? AND encerrado = 0'


def _key(claims: MvContextClaims, patient: int) -> tuple[int, int, str, int]:
    return (claims.cd_atendimento, patient, claims.cd_usuario, claims.cd_prestador)


def read_draft(claims: MvContextClaims, patient: int) -> DraftRecord | None:
    with _database() as db:
        row = db.execute('SELECT id, revisao, dados FROM sadt_drafts WHERE ' + _KEY, _key(claims, patient)).fetchone()
        if row is None:
            return None
        operations = db.execute('SELECT kind FROM sadt_draft_operations WHERE draft_id = ?', (row['id'],))
        return DraftRecord(
            id=row['id'], revisao=row['revisao'], dados=DraftData.model_validate_json(row['dados']),
            operacoes={operation['kind']: 'conferir' for operation in operations},
        )


def save_draft(claims: MvContextClaims, patient: int, draft: DraftInput) -> DraftRecord:
    key = _key(claims, patient)
    data = draft.dados.model_dump_json()
    with _database() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT id, revisao, dados FROM sadt_drafts WHERE ' + _KEY, key).fetchone()
        if draft.revisao != (row['revisao'] if row else 0) or draft.id != (row['id'] if row else None):
            raise HTTPException(status_code=409, detail='A solicitação foi atualizada em outra estação.')
        kinds = {operation['kind'] for operation in db.execute('SELECT kind FROM sadt_draft_operations WHERE draft_id = ?', (row['id'],))} if row else set()
        if row and ('anexo' in kinds or ('prescricao' in kinds and DraftData.model_validate_json(row['dados']).exames != draft.dados.exames)):
            raise HTTPException(status_code=409, detail='Confira a operação pendente no MV antes de alterar a solicitação.')
        revision = draft.revisao + 1
        identifier = row['id'] if row else uuid4().hex
        if row:
            db.execute('UPDATE sadt_drafts SET revisao = ?, dados = ? WHERE ' + _KEY, (revision, data, *key))
        else:
            db.execute('INSERT INTO sadt_drafts (atendimento,paciente,usuario,prestador,id,revisao,dados) VALUES (?, ?, ?, ?, ?, ?, ?)', (*key, identifier, revision, data))
    return DraftRecord(id=identifier, revisao=revision, dados=draft.dados, operacoes={kind: 'conferir' for kind in kinds})


Operation = tuple[str, Literal['anexo', 'prescricao']]


def begin_operation(
    claims: MvContextClaims, patient: int, draft_id: str, revision: int,
    kind: Literal['anexo', 'prescricao'], codes: list[str],
    *, expected_guide: tuple[str, str, str] | None = None,
) -> Operation:
    with _database() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT id, revisao, dados FROM sadt_drafts WHERE ' + _KEY, _key(claims, patient)).fetchone()
        if not row or row['id'] != draft_id or row['revisao'] != revision:
            raise HTTPException(status_code=409, detail='Reabra a revisão atual da solicitação antes de enviar ao MV.')
        data = DraftData.model_validate_json(row['dados'])
        if data.exames != codes or (expected_guide and (data.formato, data.carater, data.justificativa) != expected_guide):
            raise HTTPException(status_code=409, detail='Salve as alterações da solicitação antes de enviar ao MV.')
        insert = db.execute(
            'INSERT OR IGNORE INTO sadt_draft_operations(draft_id, kind) VALUES (?, ?)', (draft_id, kind),
        )
        if insert.rowcount != 1:
            raise HTTPException(status_code=409, detail='Já existe um envio para conferir no MV. Não envie novamente.')
    return draft_id, kind


def store_reference(operation: Operation, reference: dict) -> None:
    with _database() as db:
        db.execute(
            'UPDATE sadt_draft_operations SET reference = ? WHERE draft_id = ? AND kind = ?',
            (json.dumps(reference), *operation),
        )


def clear_operation(operation: Operation) -> None:
    # Only used after a known rollback, never after an ambiguous commit.
    with _database() as db:
        db.execute('DELETE FROM sadt_draft_operations WHERE draft_id = ? AND kind = ?', operation)


def get_operation_reference(claims: MvContextClaims, patient: int, kind: str, draft_id: str | None = None, revision: int | None = None) -> dict | None:
    with _database() as db:
        row = db.execute(
            """SELECT o.reference FROM sadt_draft_operations o JOIN sadt_drafts d ON o.draft_id = d.id
                 WHERE d.atendimento = ? AND d.paciente = ? AND d.usuario = ? AND d.prestador = ?
                   AND d.encerrado = 0 AND o.kind = ?
                   AND (? IS NULL OR d.id = ?) AND (? IS NULL OR d.revisao = ?)""",
            (*_key(claims, patient), kind, draft_id, draft_id, revision, revision),
        ).fetchone()
        return json.loads(row['reference']) if row and row['reference'] else None


def close_draft(claims: MvContextClaims, patient: int, draft_id: str, revision: int, checked: set[str]) -> None:
    with _database() as db:
        db.execute('BEGIN IMMEDIATE')
        operations = db.execute('SELECT kind FROM sadt_draft_operations WHERE draft_id = ?', (draft_id,))
        if {operation['kind'] for operation in operations} != checked:
            raise HTTPException(status_code=409, detail='Uma operação no MV precisa ser conferida antes de encerrar.')
        cursor = db.execute('UPDATE sadt_drafts SET encerrado = 1 WHERE ' + _KEY + ' AND id = ? AND revisao = ?', (*_key(claims, patient), draft_id, revision))
        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail='A solicitação mudou em outra estação.')
