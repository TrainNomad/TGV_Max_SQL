import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="TGV Max Visualizer API",
    description="API TGV Max ultra-rapide optimisée pour les recherches directes et correspondances.",
    version="3.0.0",
)

# Configuration CORS pour autoriser l'accès depuis n'importe quelle application frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "tgvmax_compact.db"

def get_db_connection():
    """Établit une connexion SQLite optimisée"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Optimisations de mémoire et de lecture en mode WAL
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-64000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


@app.get("/")
def read_root():
    return {"status": "ok", "message": "API TGV Max opérationnelle (v3.0)"}


# -------------------------------------------------------------------
# 1. SANTE DU SERVEUR / HEALTH CHECK
# -------------------------------------------------------------------
@app.get("/health")
def health_check():
    """
    Sert à vérifier l'état du serveur et la disponibilité de la base SQLite.
    Utile pour Docker, Render, Fly.io, Vercel ou des outils de monitoring.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trips;")
        count = cursor.fetchone()[0]
        conn.close()
        return {
            "status": "healthy",
            "database": DB_PATH,
            "trips_count": count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Base de données indisponible: {str(e)}")


# -------------------------------------------------------------------
# 2. AUTOCOMPLÉTION DES GARES ET VILLES
# -------------------------------------------------------------------
@app.get("/stations")
def get_stations(q: str = Query(None, description="Recherche partielle de gare ou ville")):
    """Retourne la liste des métropoles et gares correspondant au terme tapé"""
    if not q or not q.strip():
        return {"results": []}

    conn = get_db_connection()
    cursor = conn.cursor()
    search_pattern = q.strip().upper() + "%"

    try:
        # Recherche par métropole (parent station)
        query_cities = """
            SELECT DISTINCT origin_parent_name AS name, origin_parent_id AS id 
            FROM trips 
            WHERE UPPER(origin_parent_name) LIKE ?
            ORDER BY name ASC
            LIMIT 5
        """
        cursor.execute(query_cities, (search_pattern,))
        cities = [
            {
                "type": "city",
                "label": row["name"],
                "id": row["id"],
                "search_val": f"{row['name']} (toutes les gares)",
            }
            for row in cursor.fetchall()
        ]

        # Recherche par gare spécifique
        query_stations = """
            SELECT DISTINCT origin_name AS name, origin_parent_name AS parent, origin_id AS id 
            FROM trips 
            WHERE UPPER(origin_name) LIKE ?
            ORDER BY name ASC
            LIMIT 10
        """
        cursor.execute(query_stations, (search_pattern,))
        stations = [
            {
                "type": "station",
                "label": row["name"],
                "parent": row["parent"],
                "id": row["id"],
                "search_val": row["name"],
            }
            for row in cursor.fetchall()
        ]

        conn.close()
        return {"results": cities + stations}

    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Erreur autocomplétion: {str(e)}")


# -------------------------------------------------------------------
# 3. DÉDUPLICATION DES CORRESPONDANCES
# -------------------------------------------------------------------
def cleanup_connections(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Supprime les doublons inutiles entre deux trains identiques"""
    unique = {}
    for r in results:
        key = f"{r['train1_no']}|{r['transfer_station_arr']}|{r['transfer_station_dep']}|{r['train2_no']}"
        if key not in unique:
            unique[key] = r

    par_trains = {}
    for r in unique.values():
        train_key = f"{r['train1_no']}_{r['train2_no']}"
        if train_key not in par_trains:
            par_trains[train_key] = []
        par_trains[train_key].append(r)

    final = []
    for alts in par_trains.values():
        final.append(alts[0])

    final.sort(key=lambda x: (x["train1_dep"], x["layover_minutes"]))
    return final


# -------------------------------------------------------------------
# 4. RECHERCHE TRAJET (A -> B) - DIRECTS ET CORRESPONDANCES
# -------------------------------------------------------------------
@app.get("/search")
def search_all(
    origin: str = Query(..., description="Gare ou Ville de départ"),
    destination: str = Query(..., description="Gare ou Ville d'arrivée"),
    date: str = Query(..., description="Date au format YYYY-MM-DD"),
):
    """Recherche des trajets directs et avec 1 correspondance à une date donnée"""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        origin_clean = origin.strip().upper()
        dest_clean = destination.strip().upper()
        date_clean = date.strip()

        # 1. TRAJETS DIRECTS
        query_direct = """
        SELECT 
            date, 
            origin_name AS orig, 
            destination_name AS dest, 
            origin_lat, origin_lon, dest_lat, dest_lon,
            departure_time AS train1_dep, 
            arrival_time AS train1_arr, 
            train_no AS train1_no
            type AS train1_type
        FROM trips
        WHERE date = ?
          AND (UPPER(origin_name) = ? OR UPPER(origin_parent_name) = ?)
          AND (UPPER(destination_name) = ? OR UPPER(destination_parent_name) = ?)
        ORDER BY dep_min ASC
        LIMIT 50
        """
        cursor.execute(query_direct, (date_clean, origin_clean, origin_clean, dest_clean, dest_clean))
        direct_rows = cursor.fetchall()

        direct_results = [
            {
                "is_direct": True,
                "orig": d["orig"],
                "dest": d["dest"],
                "orig_lat": d["origin_lat"],
                "orig_lon": d["origin_lon"],
                "dest_lat": d["dest_lat"],
                "dest_lon": d["dest_lon"],
                "date": d["date"],
                "train1_no": d["train1_no"],
                "train1_type": d["train1_type"],
                "train1_dep": d["train1_dep"],
                "train1_arr": d["train1_arr"],
                "transfer_station_arr": None,
                "transfer_station_dep": None,
                "train2_no": None,
                "train2_type": None,
                "train2_dep": None,
                "train2_arr": None,
                "layover_minutes": 0,
                "is_valid_layover": True,
            }
            for d in direct_rows
        ]

        # 2. TRAJETS AVEC 1 CORRESPONDANCE
        query_connections = """
        SELECT 
            t1.origin_name AS orig,
            t1.origin_lat AS orig_lat, t1.origin_lon AS orig_lon,
            t1.destination_name AS transfer_station_arr,
            t1.dest_lat AS transfer_lat, t1.dest_lon AS transfer_lon,
            t2.origin_name AS transfer_station_dep,
            t2.destination_name AS dest,
            t2.dest_lat AS dest_lat, t2.dest_lon AS dest_lon,
            t1.date AS date,
            t1.train_no AS train1_no,
            t1.type AS train1_type,
            t1.departure_time AS train1_dep,
            t1.arrival_time AS train1_arr,
            t2.train_no AS train2_no,
            t2.type AS train2_type,
            t2.departure_time AS train2_dep,
            t2.arrival_time AS train2_arr,
            (t2.dep_min - t1.arr_min) AS layover_minutes
        FROM trips t1
        JOIN trips t2 
          ON t1.destination_parent_id = t2.origin_parent_id
         AND t1.date = t2.date
        WHERE t1.date = ?
          AND (UPPER(t1.origin_name) = ? OR UPPER(t1.origin_parent_name) = ?)
          AND (UPPER(t2.destination_name) = ? OR UPPER(t2.destination_parent_name) = ?)
          AND t2.dep_min >= (t1.arr_min + 15)
          AND t2.dep_min <= (t1.arr_min + 180)
        ORDER BY t1.dep_min ASC
        LIMIT 100
        """
        cursor.execute(query_connections, (date_clean, origin_clean, origin_clean, dest_clean, dest_clean))
        conn_rows = [dict(row) for row in cursor.fetchall()]

        valid_connections = []
        for c in conn_rows:
            c["is_direct"] = False
            # c["train1_type"] = "TGV INOUI"
            # c["train2_type"] = "TGV INOUI"
            is_same_station = c["transfer_station_arr"] == c["transfer_station_dep"]
            layover = c["layover_minutes"]
            c["is_valid_layover"] = (15 <= layover <= 120) if is_same_station else (60 <= layover <= 180)

            if c["is_valid_layover"]:
                valid_connections.append(c)

        cleaned_connections = cleanup_connections(valid_connections)
        all_results = direct_results + cleaned_connections
        all_results.sort(key=lambda x: x["train1_dep"])

        conn.close()
        return {"count": len(all_results), "results": all_results[:30]}

    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Erreur recherche: {str(e)}")

# -------------------------------------------------------------------
# 5. EXPLORER - DESTINATIONS ACCESSIBLES DEPUIS UNE GARE
# -------------------------------------------------------------------
@app.get("/explorer")
def explore_destinations(
    from_station: Optional[str] = Query(None, alias="from", description="Gare de départ"),
    origin: Optional[str] = Query(None, description="Alternative au paramètre 'from'"),
    date: str = Query(..., description="Date au format YYYY-MM-DD"),
):
    """Calcule toutes les villes/destinations atteignables en direct ou 1 correspondance"""
    departure_query = from_station or origin
    if not departure_query:
        raise HTTPException(status_code=400, detail="Paramètre 'from' ou 'origin' requis")

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        date_clean = date.strip()
        station = departure_query.strip().upper()

        best_journeys = {}

        # 1. Trajets directs
        query_direct = """
        SELECT 
            destination_name AS to_name,
            destination_parent_name AS to_id,
            dest_lat, dest_lon,
            departure_time AS dep_str,
            arrival_time AS arr_str,
            dep_min, arr_min,
            train_no
        FROM trips
        WHERE date = ? 
          AND (UPPER(origin_name) = ? OR UPPER(origin_parent_name) = ?)
          AND UPPER(destination_parent_name) != ?
        ORDER BY dep_min ASC
        LIMIT 500
        """
        cursor.execute(query_direct, (date_clean, station, station, station))
        
        for row in cursor.fetchall():
            dest_id = row["to_id"]
            duration_min = row["arr_min"] - row["dep_min"]
            if duration_min < 0:
                duration_min += 24 * 60

            journey = {
                "dest_lat": row["dest_lat"],
                "dest_lon": row["dest_lon"],
                "duration": duration_min,
                "dep_str": row["dep_str"],
                "arr_str": row["arr_str"],
                "transfers": 0,
                "legs": [{
                    "from_name": station,
                    "to_name": row["to_name"],
                    "to_id": dest_id,
                    "dep_str": row["dep_str"],
                    "arr_str": row["arr_str"],
                    "train_no": row["train_no"],
                    "lat": row["dest_lat"],
                    "lon": row["dest_lon"],
                }],
            }

            if dest_id not in best_journeys or duration_min < best_journeys[dest_id]["duration"]:
                best_journeys[dest_id] = journey

        # 2. Trajets avec 1 correspondance
        query_transfers = """
        SELECT 
            t1.departure_time AS train1_dep,
            t1.arrival_time AS train1_arr,
            t1.train_no AS train1_no,
            t1.destination_name AS transfer_arr,
            t2.origin_name AS transfer_dep,
            t2.destination_name AS to_name,
            t2.destination_parent_name AS to_id,
            t2.dest_lat, t2.dest_lon,
            t2.departure_time AS train2_dep,
            t2.arrival_time AS train2_arr,
            t2.train_no AS train2_no,
            t1.dep_min AS start_dep,
            t2.arr_min AS end_arr,
            (t2.dep_min - t1.arr_min) AS layover_minutes
        FROM trips t1
        JOIN trips t2 
          ON t1.destination_parent_id = t2.origin_parent_id
         AND t1.date = t2.date
        WHERE t1.date = ?
          AND (UPPER(t1.origin_name) = ? OR UPPER(t1.origin_parent_name) = ?)
          AND UPPER(t2.destination_parent_name) != ?
          AND t2.dep_min >= (t1.arr_min + 15)
          AND t2.dep_min <= (t1.arr_min + 180)
        ORDER BY t1.dep_min ASC
        LIMIT 500
        """
        cursor.execute(query_transfers, (date_clean, station, station, station))

        for row in cursor.fetchall():
            dest_id = row["to_id"]
            is_same_station = row["transfer_arr"] == row["transfer_dep"]
            layover = row["layover_minutes"]

            is_valid = (15 <= layover <= 120) if is_same_station else (60 <= layover <= 180)
            if not is_valid:
                continue

            duration_min = row["end_arr"] - row["start_dep"]
            if duration_min < 0:
                duration_min += 24 * 60

            journey = {
                "dest_lat": row["dest_lat"],
                "dest_lon": row["dest_lon"],
                "duration": duration_min,
                "dep_str": row["train1_dep"],
                "arr_str": row["train2_arr"],
                "transfers": 1,
                "legs": [
                    {
                        "from_name": station,
                        "to_name": row["transfer_arr"],
                        "to_id": None,
                        "dep_str": row["train1_dep"],
                        "arr_str": row["train1_arr"],
                        "train_no": row["train1_no"],
                        "lat": None,
                        "lon": None,
                    },
                    {
                        "from_name": row["transfer_dep"],
                        "to_name": row["to_name"],
                        "to_id": dest_id,
                        "dep_str": row["train2_dep"],
                        "arr_str": row["train2_arr"],
                        "train_no": row["train2_no"],
                        "lat": row["dest_lat"],
                        "lon": row["dest_lon"],
                    },
                ],
            }

            if dest_id not in best_journeys:
                best_journeys[dest_id] = journey
            elif best_journeys[dest_id]["transfers"] == 1 and duration_min < best_journeys[dest_id]["duration"]:
                best_journeys[dest_id] = journey

        journeys = list(best_journeys.values())
        journeys.sort(key=lambda x: (x["transfers"], x["duration"]))

        conn.close()
        return {"journeys": journeys, "count": len(journeys)}

    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Erreur explorer: {str(e)}")