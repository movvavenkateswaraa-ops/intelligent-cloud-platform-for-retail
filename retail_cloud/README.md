# Intelligent Cloud Platform for Retail

A simple base project: a central cloud platform that connects multiple retail stores,
tracks inventory and sales in one place, and adds AI insights.

## Features
- **Multi-store**: every store has its own API key and reports sales through a REST API
- **Central inventory** per store and product
- **Dashboard**: revenue per store, top products, forecasts
- **AI demand forecast**: linear trend, next 7 days per store/product, with days-of-cover and reorder quantity
- **AI stock-transfer advice**: move surplus stock from one store to a store that will run short
- **AI anomaly detection**: flags unusual sales spikes or drops (z-score)
- **Cloud-ready**: `/health` endpoint, environment-based config, Dockerfile with gunicorn

Demo data (3 stores, 6 products, 45 days of sales) is created on first run. The store API
keys are printed in the console at that moment.

## Run locally
```
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

## Run with Docker (cloud deployment)
```
docker build -t retail-cloud .
docker run -p 8080:8080 -v retail-data:/data -e ADMIN_KEY=change-me retail-cloud
```
The same image can be deployed to AWS App Runner / ECS, Azure Container Apps or Google Cloud Run.

## Environment variables
| Variable | Default | Purpose |
|---|---|---|
| `PORT` | 5000 | HTTP port |
| `DATABASE_PATH` | `platform.db` next to app.py | SQLite file location |
| `ADMIN_KEY` | `admin123` | Admin key (change it in production) |

## API
| Method & path | Auth | Description |
|---|---|---|
| `GET /health` | none | Health check |
| `GET /api/products` | none | List products |
| `POST /api/products` | `X-Admin-Key` | Add product `{"sku","name","category","price"}` |
| `POST /api/stores` | `X-Admin-Key` | Add store `{"name","city"}`; returns its API key |
| `GET /api/inventory` | `X-API-Key` | The calling store's stock |
| `POST /api/stock` | `X-API-Key` | Receive stock `{"sku","quantity"}` |
| `POST /api/sales` | `X-API-Key` | Record a sale `{"sku","quantity"}` |
| `GET /api/insights` | none | Summary, forecasts, transfer advice, anomalies |

Example:
```
curl -X POST http://127.0.0.1:5000/api/sales \
  -H "X-API-Key: <store key from console>" -H "Content-Type: application/json" \
  -d '{"sku":"SKU-001","quantity":2}'
```

## Notes
This is a base project. SQLite keeps it simple; for real cloud use switch to PostgreSQL/MySQL,
add HTTPS, hashed API keys and user login.
