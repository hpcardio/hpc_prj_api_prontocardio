import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from hashlib import sha256
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app_prontocardio.database import get_session_postgres
from app_prontocardio.models import TokenRedefinicaoSenha, Usuario
from app_prontocardio.schema import (
    Message,
    PasswordResetConfirm,
    PasswordResetRequest,
    Token,
)
from app_prontocardio.security import (
    criar_token,
    gera_hash_senha,
    valida_senha_cru_x_senha_hash_db,
)
from app_prontocardio.settings import Settings

router = APIRouter(prefix='/autenticacao', tags=['autenticacao'])

SessionPostgres = Annotated[Session, Depends(get_session_postgres)]
OAuthForm = Annotated[OAuth2PasswordRequestForm, Depends()]
settings = Settings()


@router.post('/token', status_code=HTTPStatus.OK, response_model=Token)
def autenticar_usuario_para_acesso(
    form_data: OAuthForm, session: SessionPostgres
):
    """Gera token para usuário logado"""

    usuario_banco = session.scalar(
        select(Usuario).where(Usuario.email == form_data.username)
    )

    excessao_autenticacao = HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED, detail='Email ou Senha incorretos'
    )

    if not usuario_banco or not usuario_banco.ativo:
        raise excessao_autenticacao

    if not valida_senha_cru_x_senha_hash_db(
        form_data.password, usuario_banco.senha
    ):
        raise excessao_autenticacao

    token = criar_token(claim={'sub': usuario_banco.email})

    return {'access_token': token, 'token_type': 'Bearer'}


def _enviar_email_redefinicao(destinatario: str, token: str) -> None:
    if not settings.SMTP_HOST:
        return
    reset_url = (
        f"{settings.FRONTEND_BASE_URL.rstrip('/')}/autenticacao/redefinir-senha/?token={token}"
    )
    mensagem = EmailMessage()
    mensagem['Subject'] = 'Redefinição de senha · Gestão de Glosas'
    mensagem['From'] = settings.smtp_from_email
    mensagem['To'] = destinatario
    mensagem.set_content(
        'Recebemos uma solicitação para redefinir sua senha.\n\n'
        f'Acesse o link abaixo em até 30 minutos:\n{reset_url}\n\n'
        'Se você não fez esta solicitação, ignore esta mensagem.'
    )
    smtp_class = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
    with smtp_class(
        settings.SMTP_HOST, settings.SMTP_PORT, timeout=10
    ) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username and settings.SMTP_PASSWORD:
            smtp.login(settings.smtp_username, settings.SMTP_PASSWORD)
        smtp.send_message(mensagem)


@router.post(
    '/esqueci-senha', status_code=HTTPStatus.OK, response_model=Message
)
def solicitar_redefinicao_senha(
    payload: PasswordResetRequest, session: SessionPostgres
):
    usuario = session.scalar(
        select(Usuario).where(Usuario.email == str(payload.email).lower())
    )
    if usuario and usuario.ativo:
        session.execute(
            update(TokenRedefinicaoSenha)
            .where(TokenRedefinicaoSenha.usuario_id == usuario.id)
            .values(utilizado=True)
        )
        token = secrets.token_urlsafe(48)
        session.add(
            TokenRedefinicaoSenha(
                usuario_id=usuario.id,
                token_hash=sha256(token.encode()).hexdigest(),
                expira_em=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
        )
        session.commit()
        try:
            _enviar_email_redefinicao(usuario.email, token)
        except (OSError, smtplib.SMTPException):
            pass
    return {
        'message': 'Se o e-mail estiver cadastrado, enviaremos as instruções.'
    }


@router.get('/redefinir-senha', response_class=HTMLResponse)
def pagina_redefinicao_senha(token: str = '') -> str:
        token_seguro = token.replace("'", "&#39;").replace('"', '&quot;')
        return f"""<!doctype html>
<html lang=\"pt-BR\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Redefinir senha</title>
    <style>
        :root {{
            --bg: #f4f6fb;
            --card: #ffffff;
            --text: #1c2a39;
            --muted: #5d6a79;
            --primary: #0c7a7d;
            --primary-2: #0a676a;
            --danger: #b42318;
            --ok: #027a48;
            --border: #d8dee8;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
            background: radial-gradient(circle at 20% 20%, #eef9f9, var(--bg));
            color: var(--text);
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 16px;
        }}
        .card {{
            width: 100%;
            max-width: 460px;
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 14px;
            box-shadow: 0 20px 40px rgba(6, 24, 44, .08);
            padding: 24px;
        }}
        h1 {{ margin: 0 0 8px; font-size: 1.35rem; }}
        p {{ margin: 0 0 16px; color: var(--muted); }}
        label {{ display: block; margin: 12px 0 6px; font-weight: 600; }}
        input {{
            width: 100%;
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 10px 12px;
            font-size: .95rem;
        }}
        input:focus {{ outline: 2px solid #9cd9db; border-color: var(--primary); }}
        button {{
            width: 100%;
            margin-top: 16px;
            border: 0;
            border-radius: 10px;
            padding: 11px 14px;
            color: #fff;
            background: linear-gradient(135deg, var(--primary), var(--primary-2));
            font-weight: 700;
            cursor: pointer;
        }}
        button:disabled {{ opacity: .7; cursor: wait; }}
        .msg {{ margin-top: 12px; font-size: .93rem; }}
        .msg.error {{ color: var(--danger); }}
        .msg.ok {{ color: var(--ok); }}
    </style>
</head>
<body>
    <main class=\"card\">
        <h1>Redefinição de senha</h1>
        <p>Defina uma nova senha para continuar.</p>
        <form id=\"form\">
            <label for=\"token\">Token</label>
            <input id=\"token\" name=\"token\" value=\"{token_seguro}\" required />
            <label for=\"nova_senha\">Nova senha</label>
            <input id=\"nova_senha\" name=\"nova_senha\" type=\"password\" minlength=\"8\" maxlength=\"128\" required />
            <button id=\"btn\" type=\"submit\">Redefinir senha</button>
            <div id=\"msg\" class=\"msg\" aria-live=\"polite\"></div>
        </form>
    </main>

    <script>
        const form = document.getElementById('form');
        const msg = document.getElementById('msg');
        const btn = document.getElementById('btn');

        form.addEventListener('submit', async (ev) => {{
            ev.preventDefault();
            msg.textContent = '';
            msg.className = 'msg';
            btn.disabled = true;

            const body = {{
                token: document.getElementById('token').value.trim(),
                nova_senha: document.getElementById('nova_senha').value,
            }};

            try {{
                const resp = await fetch('/autenticacao/redefinir-senha', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(body),
                }});
                const data = await resp.json().catch(() => ({{}}));
                if (!resp.ok) {{
                    msg.classList.add('error');
                    msg.textContent = data.detail || 'Não foi possível redefinir a senha.';
                }} else {{
                    msg.classList.add('ok');
                    msg.textContent = data.message || 'Senha redefinida com sucesso.';
                    form.reset();
                }}
            }} catch (_) {{
                msg.classList.add('error');
                msg.textContent = 'Falha de conexão. Tente novamente.';
            }} finally {{
                btn.disabled = false;
            }}
        }});
    </script>
</body>
</html>"""


@router.post(
    '/redefinir-senha', status_code=HTTPStatus.OK, response_model=Message
)
def confirmar_redefinicao_senha(
    payload: PasswordResetConfirm, session: SessionPostgres
):
    token_hash = sha256(payload.token.encode()).hexdigest()
    registro = session.scalar(
        select(TokenRedefinicaoSenha).where(
            TokenRedefinicaoSenha.token_hash == token_hash,
            TokenRedefinicaoSenha.utilizado.is_(False),
        )
    )
    agora = datetime.now(timezone.utc)
    if registro and registro.expira_em.tzinfo is None:
        registro.expira_em = registro.expira_em.replace(tzinfo=timezone.utc)
    if not registro or registro.expira_em < agora:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, 'Link inválido ou expirado.'
        )
    usuario = session.get(Usuario, registro.usuario_id)
    if not usuario or not usuario.ativo:
        raise HTTPException(
            HTTPStatus.BAD_REQUEST, 'Link inválido ou expirado.'
        )
    usuario.senha = gera_hash_senha(payload.nova_senha)
    session.execute(
        update(TokenRedefinicaoSenha)
        .where(TokenRedefinicaoSenha.usuario_id == usuario.id)
        .values(utilizado=True)
    )
    session.commit()
    return {'message': 'Senha redefinida com sucesso.'}
