from http import HTTPStatus

from fastapi import FastAPI

app = FastAPI(tittle='API Hospital Prontocardio')


@app.get('/', status_code=HTTPStatus.OK)
def read_root():
    return {'message': 'Olá Mundo!'}
