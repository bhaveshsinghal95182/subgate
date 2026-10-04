FROM python:3.13-alpine
RUN apk add --no-cache ffmpeg
WORKDIR /app
COPY subgate.py /app/subgate.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/subgate.py"]
