FROM python:3.13-slim
WORKDIR /app
RUN useradd --uid 1000 --create-home triage && mkdir /app/data && chown triage:triage /app/data
COPY --chown=triage:triage server.py ./
COPY --chown=triage:triage public ./public
USER triage
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 3000
CMD ["python", "server.py"]
