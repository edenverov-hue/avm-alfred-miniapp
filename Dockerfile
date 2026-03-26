FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn[standard]

# Kopiuj wszystko z kontekstu (build context = ~/alfred/)
COPY miniapp/api.py .
COPY miniapp/index.html .
COPY agents/gate_engine.py ./agents/
COPY agents/guardian.py ./agents/
COPY agents/thread_tracker.py ./agents/

# Dane vault/portal/memory montowane jako volume na /root/alfred/
RUN mkdir -p /root/alfred/vault /root/alfred/portal /root/alfred/.claude/memory

ENV PYTHONPATH=/app:/app/agents

EXPOSE 8765

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8765"]
