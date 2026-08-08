
from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, Float, String, func, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from typing import List, Optional
import os
import time
from types import SimpleNamespace
from fastapi.responses import HTMLResponse
from dataclasses import asdict

from dashboard_service import DashboardService


# 1. Database Setup
# Render provides the DATABASE_URL environment variable automatically
# We replace 'postgres://' with 'postgresql://' as required by modern SQLAlchemy
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local_dev.db")
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 2. Database Model (PostgreSQL Table Schema)
class LogEntry(Base):
    __tablename__ = "load_cell_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    weight = Column(Float, nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    timestamp = Column(String, nullable=False)

# Auto-create the table if it doesn't exist
Base.metadata.create_all(bind=engine)

# 3. Pydantic Models (For validating incoming Flutter JSON data)
class LogData(BaseModel):
    weight: float
    latitude: float
    longitude: float
    timestamp: str

class LogResponse(LogData):
    id: int
    class Config:
        from_attributes = True

# 4. FastAPI App Initialization
app = FastAPI(title="Load Cell API")

# In-process cache for /api/dashboard. Single-process assumption: fine for
# Render's free tier (1 worker). If you scale to multiple workers/instances
# later, swap this for something shared (Redis, etc.) since each process
# would otherwise keep its own cache.
_dashboard_cache = {"key": None, "payload": None, "computed_at": 0.0}
_CACHE_MAX_AGE_SECONDS = 30  # force a refresh at least this often regardless

# Dependency to get a database session per request
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 5. API Endpoints
@app.post("/api/logs", summary="Sync logs from Flutter")
def sync_logs(logs: List[LogData], db: Session = Depends(get_db)):
    """Receives an array of unsynced logs from the app and saves them to Postgres."""
    db_logs = [
        LogEntry(
            weight=log.weight, 
            latitude=log.latitude, 
            longitude=log.longitude, 
            timestamp=log.timestamp
        ) 
        for log in logs
    ]
    db.add_all(db_logs)
    db.commit()
    return {"status": "success", "inserted_count": len(logs)}

@app.get("/api/logs", response_model=List[LogResponse], summary="Get all logs for Map")
def get_logs(limit: int = 500, db: Session = Depends(get_db)):
    """Fetches recent data points to display on the Flutter map.

    Capped at `limit` (default 500, most recent first by id) instead of
    returning the entire table — this endpoint has no LIMIT today and will
    only get slower as the table grows. Pass ?limit=0 to get everything
    (kept as an escape hatch, not the default).
    """
    query = db.query(LogEntry).order_by(LogEntry.id.desc())
    if limit and limit > 0:
        query = query.limit(limit)
    logs = query.all()
    logs.reverse()  # keep chronological order like before
    return logs

@app.get("/api/dashboard", summary="Get processed data for the web dashboard")
def get_dashboard_data(
    max_chart_points: int = 200,
    moving_avg_window: int = 5,
    db: Session = Depends(get_db),
):
    """
    Processed view of the log data for the web dashboard, so the frontend
    doesn't have to re-fetch and re-crunch the entire raw table on every
    poll. Returns:
      - stats: latest/avg/min/max weight
      - chart: downsampled + smoothed weight-over-time series
      - map_points: GPS points with invalid (0,0 / out-of-range) fixes dropped
      - dropped_gps_points: count of points filtered out, for transparency

    Perf note: the frontend polls this every ~8s. Most of those polls hit
    a table that hasn't changed at all since the last one. Rather than
    re-fetch every row and re-run the downsample/smoothing math each time,
    we first run one cheap aggregate query (COUNT + MAX(id) — index-backed,
    tiny payload) to check whether anything actually changed, and only
    do the expensive full fetch + processing when it has.

    On a cache miss we used to run two more separate round trips: one to
    fetch the last 100 rows, one to compute all-time avg/min/max. Those are
    now combined into a single query (last-100-rows CROSS JOIN all-time
    aggregate), so a cache miss costs 2 round trips total instead of 3.
    """
    row_count, latest_id = db.query(
        func.count(LogEntry.id), func.max(LogEntry.id)
    ).one()
    cache_key = (row_count, latest_id, max_chart_points, moving_avg_window)

    cached = _dashboard_cache.get("key") == cache_key
    stale = (time.monotonic() - _dashboard_cache.get("computed_at", 0)) > _CACHE_MAX_AGE_SECONDS
    if cached and not stale:
        return _dashboard_cache["payload"]

    # Single round trip: last 100 rows (oldest-first) joined against one
    # all-time aggregate row. Works on both SQLite (local dev) and Postgres.
    rows = db.execute(text("""
        SELECT logs.id, logs.weight, logs.latitude, logs.longitude, logs.timestamp,
               agg.total_count, agg.avg_weight, agg.min_weight, agg.max_weight
        FROM (
            SELECT id, weight, latitude, longitude, timestamp
            FROM load_cell_logs
            ORDER BY id DESC
            LIMIT 100
        ) AS logs
        CROSS JOIN (
            SELECT COUNT(*) AS total_count,
                   AVG(weight) AS avg_weight,
                   MIN(weight) AS min_weight,
                   MAX(weight) AS max_weight
            FROM load_cell_logs
        ) AS agg
        ORDER BY logs.id ASC
    """)).mappings().all()

    # DashboardService expects attribute access (log.weight, log.latitude,
    # ...) since it's normally fed ORM rows — wrap the raw dict rows so it
    # doesn't need to know it's getting raw SQL results here.
    logs = [
        SimpleNamespace(
            id=r["id"],
            weight=r["weight"],
            latitude=r["latitude"],
            longitude=r["longitude"],
            timestamp=r["timestamp"],
        )
        for r in rows
    ]

    service = DashboardService(
        max_chart_points=max_chart_points,
        moving_avg_window=moving_avg_window,
    )

    if rows:
        first = rows[0]
        db_stats = (
            first["total_count"],
            first["avg_weight"],
            first["min_weight"],
            first["max_weight"],
        )
    else:
        db_stats = (0, None, None, None)

    # Pass the database stats into the service
    payload = service.build(logs, db_stats=db_stats)
    response_body = {
        "stats": asdict(payload.stats),
        "chart": asdict(payload.chart),
        "map_points": [asdict(p) for p in payload.map_points],
        "dropped_gps_points": payload.dropped_gps_points,
    }

    _dashboard_cache["key"] = cache_key
    _dashboard_cache["payload"] = response_body
    _dashboard_cache["computed_at"] = time.monotonic()

    return response_body

@app.get("/", response_class=HTMLResponse, summary="Serve Web Dashboard")
def serve_dashboard():
    """Reads the index.html file and serves it to the browser."""
    with open("index.html", "r") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)
