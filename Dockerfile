FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py blocklist.py discovery.py automatic_research.py automatic_sending.py serve.py ./
COPY Nextleaf-Digital-email-logo.png ./
COPY templates/ ./templates/
COPY static/ ./static/
RUN useradd --system --uid 10001 nextleaf && install -d -o nextleaf -g nextleaf /data
ENV OUTREACH_DB=/data/outreach.db PYTHONUNBUFFERED=1
USER nextleaf
EXPOSE 5050
CMD ["python", "serve.py"]
