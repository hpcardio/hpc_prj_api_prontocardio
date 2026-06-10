FROM python:3.12-slim
ENV POETRY_VIRTUALENVS_CREATE=false

# Setup Oracle Instant Client
COPY setup_instant_client.sh /tmp/setup_instant_client.sh
RUN chmod +x /tmp/setup_instant_client.sh && /tmp/setup_instant_client.sh && rm /tmp/setup_instant_client.sh
ENV LD_LIBRARY_PATH=/opt/oracle/instantclient_19_23:${LD_LIBRARY_PATH}
ENV PATH=/opt/oracle/instantclient_19_23:${PATH}

WORKDIR /app
COPY . .

RUN pip install poetry

RUN poetry config installer.max-workers 10
RUN poetry install --no-interaction --no-ansi --without dev --no-root

EXPOSE 8000
CMD ["sh", "-c", "poetry run uvicorn --host 0.0.0.0 --port ${PORT:-8000} app_prontocardio.app:app"]