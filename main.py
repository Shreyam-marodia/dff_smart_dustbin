from fastapi import FastAPI, Depends
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, Float, String
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from typing import List
import os
from fastapi.responses import HTMLResponse

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
def get_logs(db: Session = Depends(get_db)):
    """Fetches all data points to display on the Flutter map."""
    return db.query(LogEntry).all()

@app.get("/", response_class=HTMLResponse, summary="Serve Web Dashboard")
def serve_dashboard():
    """Reads the index.html file and serves it to the browser."""
    with open("index.html", "r") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)