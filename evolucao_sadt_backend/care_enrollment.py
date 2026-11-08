from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from evolucao_sadt_backend.mv_context_token import MvContextClaims


VALID_LINES = {'LC-DAC', 'LC-IC', 'LC-PREVENCAO'}


class CareEnrollmentStore:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS linhas_cuidado_paciente (
                    cd_paciente INTEGER NOT NULL,
                    linha_cuidado TEXT NOT NULL,
                    ativa INTEGER NOT NULL,
                    data_ancora TEXT,
                    origem TEXT NOT NULL,
                    cd_atendimento INTEGER NOT NULL,
                    usuario_mv TEXT NOT NULL,
                    atualizado_em TEXT NOT NULL,
                    PRIMARY KEY (cd_paciente, linha_cuidado)
                )
                '''
            )
            connection.execute(
                '''
                CREATE TABLE IF NOT EXISTS linhas_cuidado_paciente_auditoria (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cd_paciente INTEGER NOT NULL,
                    linha_cuidado TEXT NOT NULL,
                    estado_json TEXT NOT NULL,
                    cd_atendimento INTEGER NOT NULL,
                    usuario_mv TEXT NOT NULL,
                    criado_em TEXT NOT NULL
                )
                '''
            )

    def upsert(
        self,
        patient_id: int,
        attendance_id: int,
        user: str,
        entries: list[dict[str, Any]],
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        changed = 0
        with self.connect() as connection:
            for entry in entries:
                line = str(entry.get('linha', '')).strip().upper()
                if line not in VALID_LINES:
                    raise ValueError(f'Linha de Cuidado inválida: {line or "vazia"}.')
                state = {
                    'linha': line,
                    'ativa': bool(entry.get('ativa')),
                    'data_ancora': entry.get('data_ancora') or None,
                    'origem': str(entry.get('origem') or 'MEDICO').strip().upper(),
                }
                current = connection.execute(
                    '''SELECT ativa, data_ancora, origem
                         FROM linhas_cuidado_paciente
                        WHERE cd_paciente = ? AND linha_cuidado = ?''',
                    (patient_id, line),
                ).fetchone()
                if current and (
                    bool(current['ativa']) == state['ativa']
                    and current['data_ancora'] == state['data_ancora']
                    and current['origem'] == state['origem']
                ):
                    continue
                connection.execute(
                    '''
                    INSERT INTO linhas_cuidado_paciente (
                        cd_paciente, linha_cuidado, ativa, data_ancora, origem,
                        cd_atendimento, usuario_mv, atualizado_em
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cd_paciente, linha_cuidado) DO UPDATE SET
                        ativa=excluded.ativa,
                        data_ancora=excluded.data_ancora,
                        origem=excluded.origem,
                        cd_atendimento=excluded.cd_atendimento,
                        usuario_mv=excluded.usuario_mv,
                        atualizado_em=excluded.atualizado_em
                    ''',
                    (
                        patient_id, line, int(state['ativa']), state['data_ancora'],
                        state['origem'], attendance_id, user, now,
                    ),
                )
                connection.execute(
                    '''INSERT INTO linhas_cuidado_paciente_auditoria (
                           cd_paciente, linha_cuidado, estado_json,
                           cd_atendimento, usuario_mv, criado_em
                       ) VALUES (?, ?, ?, ?, ?, ?)''',
                    (
                        patient_id, line,
                        json.dumps(state, ensure_ascii=False, sort_keys=True),
                        attendance_id, user, now,
                    ),
                )
                changed += 1
        return changed

    def list_for_patient(self, patient_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                '''SELECT linha_cuidado, ativa, data_ancora, origem
                     FROM linhas_cuidado_paciente
                    WHERE cd_paciente = ?
                    ORDER BY linha_cuidado''',
                (patient_id,),
            ).fetchall()
        return [
            {
                'linha': row['linha_cuidado'],
                'ativa': bool(row['ativa']),
                'data_ancora': row['data_ancora'],
                'origem': row['origem'],
            }
            for row in rows
        ]

    def audit_count(self) -> int:
        with self.connect() as connection:
            return int(connection.execute(
                'SELECT COUNT(*) FROM linhas_cuidado_paciente_auditoria'
            ).fetchone()[0])


def resolve_patient_from_attendance(
    attendance_id: int,
    claims: MvContextClaims,
    context_resolver: Callable[[int], Any],
) -> int:
    claims.require_attendance(attendance_id)
    context = context_resolver(attendance_id)
    patient_id = int(getattr(context, 'cd_paciente'))
    if patient_id <= 0:
        raise ValueError('Paciente do atendimento não identificado.')
    return patient_id


def default_enrollment_store() -> CareEnrollmentStore:
    default_dir = (
        Path(tempfile.gettempdir()) / 'evolucao_sadt_data'
        if os.name == 'nt'
        else Path('/app/evolucao_sadt_data')
    )
    data_dir = Path(os.getenv('EVOLUCAO_SADT_DATA_DIR', str(default_dir)))
    return CareEnrollmentStore(data_dir / 'linhas_cuidado_paciente.sqlite3')

