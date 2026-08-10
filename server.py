from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
from typing import List, Optional

app = FastAPI(
    title="TGV Max Visualizer API",
    description="API de recherche de billets TGV Max avec calcul rapide de correspondances",
    version="1.0.0"
)

# Autoriser le Frontend à interroger l'API (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # En production, vous pourrez restreindre au domaine de votre site web
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "tgvmax_compact.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/")
def read_root():
    return {"status": "ok", "message": "API TGV Max opérationnelle"}

# -------------------------------------------------------------------
# 1. Route pour l'autocomplétion des gares dans le Frontend
# -------------------------------------------------------------------
@app.get("/stations")
def get_stations():
    """Retourne la liste des gares distinctes disponibles dans la base."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = """
    SELECT DISTINCT origin_name as name FROM trips
    UNION
    SELECT DISTINCT destination_name as name FROM trips
    ORDER BY name ASC
    """
    cursor.execute(query)
    stations = [row["name"] for row in cursor.fetchall()]
    conn.close()
    return {"count": len(stations), "stations": stations}

# -------------------------------------------------------------------
# 2. Recherche de trajets DRECTS
# -------------------------------------------------------------------
@app.get("/search/direct")
def search_direct(
    origin: str = Query(..., description="Gare de départ (ex: PARIS (TOUTES GARES) ou RENNES)"),
    destination: str = Query(..., description="Gare d'arrivée (ex: LYON (TOUTES GARES))"),
    date: str = Query(..., description="Date au format YYYY-MM-DD")
):
    """Recherche des trajets TGV Max directs."""
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
    SELECT date, origin_name, destination_name, departure_time, arrival_time, train_no
    FROM trips
    WHERE UPPER(origin_name) = UPPER(?)
      AND UPPER(destination_name) = UPPER(?)
      AND date = ?
    ORDER BY departure_time ASC
    """
    cursor.execute(query, (origin.strip(), destination.strip(), date.strip()))
    rows = cursor.fetchall()
    conn.close()

    results = [dict(row) for row in rows]
    return {"type": "direct", "count": len(results), "results": results}

# -------------------------------------------------------------------
# 3. Moteur de Recherche avec CORRESPONDANCES (1 escale)
# -------------------------------------------------------------------
@app.get("/search/connections")
def search_connections(
    origin: str = Query(..., description="Gare de départ"),
    destination: str = Query(..., description="Gare d'arrivée finale"),
    date: str = Query(..., description="Date au format YYYY-MM-DD"),
    min_layover_mins: int = Query(20, description="Temps d'escale min en minutes"),
    max_layover_mins: int = Query(180, description="Temps d'escale max en minutes")
):
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
    SELECT 
        t1.origin_name AS orig,
        t1.destination_name AS transfer_station,
        t2.destination_name AS dest,
        t1.date AS date,
        t1.train_no AS train1_no,
        t1.departure_time AS train1_dep,
        t1.arrival_time AS train1_arr,
        t2.train_no AS train2_no,
        t2.departure_time AS train2_dep,
        t2.arrival_time AS train2_arr,
        CAST((
            STRFTIME('%s', DATETIME(t2.date || ' ' || t2.departure_time)) - 
            STRFTIME('%s', DATETIME(t1.date || ' ' || t1.arrival_time))
        ) / 60 AS INTEGER) AS layover_minutes
    FROM trips t1
    JOIN trips t2 
      ON UPPER(t1.destination_name) = UPPER(t2.origin_name) 
     AND t1.date = t2.date
    WHERE UPPER(t1.origin_name) LIKE UPPER(?)
      AND UPPER(t2.destination_name) LIKE UPPER(?)
      AND t1.date = ?
      AND DATETIME(t2.date || ' ' || t2.departure_time) >= DATETIME(t1.date || ' ' || t1.arrival_time, '+' || ? || ' minutes')
      AND DATETIME(t2.date || ' ' || t2.departure_time) <= DATETIME(t1.date || ' ' || t1.arrival_time, '+' || ? || ' minutes')
    ORDER BY t1.departure_time ASC
    """

    cursor.execute(query, (
        f"%{origin.strip()}%", 
        f"%{destination.strip()}%", 
        date.strip(),
        str(min_layover_mins),
        str(max_layover_mins)
    ))
    rows = cursor.fetchall()
    conn.close()

    results = [dict(row) for row in rows]
    return {
        "type": "connection",
        "count": len(results),
        "results": results
    }
        