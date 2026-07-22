import asyncio
import json
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from config import POLL_INTERVAL_SECONDS
from feed_fetcher import fetch_latest_headlines
from classifier import classify_headline

app = FastAPI(title="Indian Market News Classifier")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

seen_headlines = set()
all_classified = []
active_connections: list[WebSocket] = []


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keeps connection alive; client doesn't need to send real data
    except WebSocketDisconnect:
        active_connections.remove(websocket)


async def broadcast(item: dict):
    dead = []
    for conn in active_connections:
        try:
            await conn.send_text(json.dumps(item))
        except Exception:
            dead.append(conn)
    for d in dead:
        active_connections.remove(d)


async def poll_feeds_loop():
    while True:
        new_items = fetch_latest_headlines()
        for item in new_items:
            headline = item["headline"]
            if not headline or headline in seen_headlines:
                continue
            seen_headlines.add(headline)

            result = classify_headline(headline)
            result["link"] = item.get("link", "")
            result["published"] = item.get("published", "")
            result["fetched_at"] = datetime.utcnow().isoformat()

            all_classified.append(result)
            await broadcast(result)
            print(f"Classified: {result['category']} -> {headline}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(poll_feeds_loop())


@app.get("/headlines")
def get_headlines():
    return all_classified


@app.get("/")
def root():
    return {"status": "running", "classified_count": len(all_classified)}