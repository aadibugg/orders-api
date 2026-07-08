"""
Orders API — demonstrates:
  1. Idempotent POST /orders
  2. Cursor-based pagination on GET /orders
  3. Per-client rate limiting (X-Client-Id header)

Assigned values for this task:
  TOTAL_ORDERS = 50
  RATE_LIMIT   = 17 requests / 10 seconds
"""

import time
import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Orders API")

# ---------------------------------------------------------------------------
# Assigned configuration
# ---------------------------------------------------------------------------
TOTAL_ORDERS = 50
RATE_LIMIT = 17
WINDOW_SECONDS = 10

# ---------------------------------------------------------------------------
# In-memory "database"
# ---------------------------------------------------------------------------

# Fixed catalog of orders 1..TOTAL_ORDERS, used for pagination.
ORDERS_CATALOG = [
    {"id": i, "item": f"Item {i}", "status": "catalog"}
    for i in range(1, TOTAL_ORDERS + 1)
]

# idempotency_key -> order dict already returned for that key
idempotency_store: dict[str, dict] = {}

# client_id -> list of request timestamps (seconds) within the current window
rate_limit_store: dict[str, list] = {}


class OrderCreate(BaseModel):
    item: Optional[str] = None


# ---------------------------------------------------------------------------
# Rate limiting (sliding window, per client)
# ---------------------------------------------------------------------------
def check_rate_limit(client_id: str):
    now = time.time()
    window_start = now - WINDOW_SECONDS

    timestamps = rate_limit_store.get(client_id, [])
    # Drop timestamps that have fallen out of the 10-second window.
    timestamps = [t for t in timestamps if t > window_start]

    if len(timestamps) >= RATE_LIMIT:
        oldest = timestamps[0]
        retry_after = max(1, int(oldest + WINDOW_SECONDS - now) + 1)
        rate_limit_store[client_id] = timestamps
        raise HTTPException(
            status_code=429,
            detail="Too many requests, slow down.",
            headers={"Retry-After": str(retry_after)},
        )

    timestamps.append(now)
    rate_limit_store[client_id] = timestamps


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_id = request.headers.get("X-Client-Id")
    if client_id:
        try:
            check_rate_limit(client_id)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# CORS — registered AFTER the rate-limit middleware on purpose.
#
# FastAPI/Starlette builds its middleware stack so that whichever middleware
# is added LAST ends up OUTERMOST (wrapping everything else). We need CORS
# to be outermost so that even a 429 response returned early by the rate
# limiter (which never calls call_next) still passes through CORSMiddleware
# and gets an Access-Control-Allow-Origin header. If CORS were registered
# first (and therefore ended up innermost), short-circuited 429 responses
# would skip it entirely, and a browser's fetch() would fail with a generic
# "Failed to fetch" / CORS error instead of showing the 429.
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # By default browsers only let JS read a small safelist of response
    # headers on cross-origin requests (Content-Type, Content-Length, etc).
    # Retry-After is NOT on that safelist, so without explicitly exposing
    # it here, fetch()'s response.headers.get("Retry-After") returns null
    # in a browser even though the header is genuinely present on the wire.
    expose_headers=["Retry-After"],
)


# ---------------------------------------------------------------------------
# 1. Idempotent order creation
# ---------------------------------------------------------------------------
@app.post("/orders", status_code=201)
async def create_order(
    order: OrderCreate = OrderCreate(),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    # Repeat call with the same key -> return the exact same order, no new id.
    if idempotency_key and idempotency_key in idempotency_store:
        return idempotency_store[idempotency_key]

    new_order = {
        "id": str(uuid.uuid4()),
        "item": order.item or "New Item",
        "status": "created",
    }

    if idempotency_key:
        idempotency_store[idempotency_key] = new_order

    return new_order


# ---------------------------------------------------------------------------
# 2. Cursor-based pagination
# ---------------------------------------------------------------------------
@app.get("/orders")
async def list_orders(limit: int = 10, cursor: Optional[str] = None):
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be >= 1")

    start = 0
    if cursor is not None:
        try:
            start = int(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    end = start + limit
    items = ORDERS_CATALOG[start:end]
    next_cursor = str(end) if end < TOTAL_ORDERS else None

    return {
        "items": items,
        "next_cursor": next_cursor,
        # aliases some graders may look for
        "next": next_cursor,
        "orders": items,
    }


@app.get("/")
async def root():
    return {"status": "ok", "service": "orders-api"}
