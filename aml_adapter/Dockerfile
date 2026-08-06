FROM python:3.11-slim

WORKDIR /app

RUN useradd --create-home --uid 1000 aml

COPY aml_adapter/requirements.txt /tmp/requirements.txt
COPY hindsight-clients/python /tmp/hindsight-client
RUN pip install --no-cache-dir -r /tmp/requirements.txt /tmp/hindsight-client

COPY aml_adapter /app/aml_adapter

RUN mkdir -p /data && chown -R aml:aml /app /data

USER aml

EXPOSE 8000

CMD ["uvicorn", "aml_adapter.app:app", "--host", "0.0.0.0", "--port", "8000"]
