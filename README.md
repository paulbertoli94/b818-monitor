# Vodafone FWA Monitor

Piccola applicazione FastAPI per leggere le statistiche di traffico da un router Huawei LTE/FWA ed esporre una dashboard web su porta `8088`.

## Requisiti

- Docker
- Docker Compose

## Avvio locale con Docker Compose

Configura il file `.env`, poi avvia:

```bash
cp .env
# aggiorna ROUTER_HOST / ROUTER_PASSWORD se necessario
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

Se fai la build da Apple Silicon (`arm64`) e poi pubblichi direttamente con `docker build` + `docker push`, l'immagine risultante puo contenere solo `linux/arm64`. Un server Intel/AMD64 o Portainer su `linux/amd64` non riuscira quindi ad avviarla.

Per produzione pubblica sempre una build multi-arch con `buildx`.

1. Login al registry:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

2. Crea e seleziona un builder `buildx` se non esiste gia:

```bash
docker buildx create --name multiarch --use
docker buildx inspect --bootstrap
```

3. Build e push multi-arch:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/paulbertoli94/b818-monitor:latest \
  --push \
  --provenance=false \
  --sbom=false .
```

## Avvio con immagine pubblicata

Per usare direttamente l'immagine pubblicata:

```bash
docker compose -f docker-compose.prod.yml up -d
```

## Variabili ambiente (`.env`)

- `ROUTER_HOST`: indirizzo del router, ad esempio `192.168.8.1`
- `ROUTER_USER`: username del router, se richiesto
- `ROUTER_PASSWORD`: password del router
- `POLL_SECONDS`: intervallo di polling, ad esempio `1`
