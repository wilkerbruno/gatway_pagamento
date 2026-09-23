FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8004
VOLUME ["/app/data"]
CMD ["gunicorn", "-b", "0.0.0.0:8004", "-w", "2", "app:app"]
