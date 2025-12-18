FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .

# uvicorn[standard] porta con sé uvloop/httptools: servono toolchain durante la build, poi rimossi
RUN apk add --no-cache build-base libffi-dev \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del build-base libffi-dev

COPY main.py .

EXPOSE 8088

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8088"]
