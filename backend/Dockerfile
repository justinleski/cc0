FROM python:3.12-slim

WORKDIR /srv/cc0
ENV PYTHONPATH=/srv/cc0/backend

COPY pyproject.toml backend/
RUN python -c 'import tomllib; print(*tomllib.load(open("backend/pyproject.toml", "rb"))["project"]["dependencies"], sep="\n")' > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && useradd --create-home --uid 10001 cc0
COPY . backend/

USER cc0
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
