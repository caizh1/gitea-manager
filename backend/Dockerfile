FROM 10.10.5.21:5001/library/python:3.11-slim

WORKDIR /app

COPY requirements.txt .
COPY wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels/ -r requirements.txt && rm -rf /tmp/wheels

COPY . .

RUN mkdir -p /app/data/backups

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "600", "app:create_app()"]
