# Vodafone FWA Monitor

Piccola applicazione FastAPI per leggere le statistiche di traffico da un router Huawei LTE/FWA ed esporre una dashboard web su porta `8088`.

## Requisiti

- Docker
- Docker Compose

## Avvio locale con Docker Compose

Aggiorna le variabili nel file `docker-compose.yml` oppure passale da ambiente, poi avvia:

```bash
docker compose up --build
```

L'app sarà disponibile su:

```text
http://localhost:8088
```

## Build immagine Docker

Per buildare l'immagine localmente:

```bash
docker build -t fwa-monitor:latest .
```

## Push immagine su registry

L'esempio di produzione usa GHCR con immagine `ghcr.io/paulbertoli94/b818-monitor:latest`.

1. Login al registry:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

2. Build dell'immagine con il tag del registry:

```bash
docker build -t ghcr.io/paulbertoli94/b818-monitor:latest .
```

3. Push dell'immagine:

```bash
docker push ghcr.io/paulbertoli94/b818-monitor:latest
```

## Avvio con immagine pubblicata

Per usare direttamente l'immagine pubblicata:

```bash
docker compose -f docker-compose.prod.yml up -d
```

## Variabili ambiente

- `ROUTER_HOST`: indirizzo del router, ad esempio `192.168.8.1`
- `ROUTER_USER`: username del router, se richiesto
- `ROUTER_PASSWORD`: password del router
- `POLL_SECONDS`: intervallo di polling, ad esempio `1`
