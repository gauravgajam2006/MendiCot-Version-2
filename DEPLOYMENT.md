# Deployment

This backend is designed to run as a **Web Service** on platforms like Render.

## Overview
- **Render service type:** Web Service
- **Runtime:** Python (3.11.x)
- **Persistence:** Currently, there is NO database or Redis persistence. Active games, sessions, and WebSocket connections are stored entirely in process memory and will be lost on restart, redeploy, or free-instance spin-down.

## Build and Start Commands
- **Build command:**
  `pip install -r requirements.txt`
- **Start command:**
  `uvicorn mendicot.api.routes:app --host 0.0.0.0 --port $PORT --workers 1`

**Warning**: Production must run with **exactly one worker** (`--workers 1`) due to the in-memory architecture. Do not use `--reload` in production.
Render will supply the `$PORT` environment variable to the start command.

## Configuration
Use the `ALLOWED_ORIGINS` environment variable to configure CORS. For example:
`ALLOWED_ORIGINS=https://your-production-frontend.vercel.app`

## Health Check
- **Health-check path:** `/health`
This endpoint is lightweight and dependency-free.

## WebSockets
WebSockets (WSS) are provided through Render's HTTPS proxy. The WebSocket endpoint does not currently validate the `Origin` header directly, relying on the REST API's CORS validation for initial session token issuance.
