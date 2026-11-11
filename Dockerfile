FROM python:3.13-slim
WORKDIR /app
# PostgreSQL driver, used only when DATABASE_URL is set (SQLite needs nothing).
RUN pip install --no-cache-dir --disable-pip-version-check "psycopg[binary]==3.3.8"
RUN useradd --uid 1000 --create-home triage && mkdir /app/data && chown triage:triage /app/data
COPY --chown=triage:triage server.py ./
COPY --chown=triage:triage public ./public
USER triage
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 3000
CMD ["python", "server.py"]
