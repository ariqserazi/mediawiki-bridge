FROM python:3.12-slim

WORKDIR /app
COPY app.py /app/app.py

RUN pip install --no-cache-dir fastapi uvicorn httpx

ENV PORT=8080
EXPOSE 8080

CMD ["python","-m","uvicorn","app:app","--host","0.0.0.0","--port","8080"]
