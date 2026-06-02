from pwdlib import PasswordHash

pwd_context = PasswordHash.recommended()


def gera_hash_senha(senha_cru: str):
    return pwd_context.hash(senha_cru)


def valida_senha_cru_x_senha_hash_db(senha_cru: str, senha_hash_db: str):
    return pwd_context.verify(senha_cru, senha_hash_db)
