import sqlite3
from datetime import datetime
from typing import Any, Dict, List
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="TGV Max Visualizer API",
    description=(
        "API de recherche TGV Max avec support des métropoles, "
        "correspondances inter-gares et carte interactive (Explore)."
    ),
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    return {"status": "ok", "message": "API TGV Max opérationnelle (v2.0)"}


# -------------------------------------------------------------------
# 1. AUTOCOMPLÉTION (Gares précises + Villes Métropoles)
# -------------------------------------------------------------------
@app.get("/stations")
def get_stations(q: str = Query(None, description="Recherche partielle")):
    conn = get_db_connection()
    cursor = conn.cursor()

    if not q or not q.strip():
        return {"stations": [], "results": []}

    search_term = f"%{q.strip()}%"

    # 1. Recherche des VILLES (parent_station)
    query_cities = """
        SELECT DISTINCT origin_parent_station AS name 
        FROM trips 
        WHERE UPPER(origin_parent_station) LIKE UPPER(?)
        ORDER BY name ASC
        LIMIT 5
    """
    cursor.execute(query_cities, (search_term,))
    cities = [
        {
            "type": "city",
            "label": row["name"],
            "search_val": f"{row['name']} (toutes les gares)",
        }
        for row in cursor.fetchall()
    ]

    # 2. Recherche des GARES PRÉCISES (origin_name)
    query_stations = """
        SELECT DISTINCT origin_name AS name, origin_parent_station AS parent 
        FROM trips 
        WHERE UPPER(origin_name) LIKE UPPER(?)
        ORDER BY name ASC
        LIMIT 10
    """
    cursor.execute(query_stations, (search_term,))
    stations = [
        {
            "type": "station",
            "label": row["name"],
            "parent": row["parent"],
            "search_val": row["name"],
        }
        for row in cursor.fetchall()
    ]

    conn.close()

    # On combine : d'abord les Villes, puis les Gares
    return {"results": cities + stations}


# -------------------------------------------------------------------
# 2. NETTOYAGE ET DÉDUPLICATION DES CORRESPONDANCES
# -------------------------------------------------------------------
def cleanup_connections(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
        # Filtre de viabilité
        valides = [a for a in alts if a["is_valid_layover"]]
        if valides:
            final.append(valides[0])
        else:
            final.append(alts[0])

    final.sort(key=lambda x: (x["train1_dep"], x["layover_minutes"]))
    return final


# -------------------------------------------------------------------
# 3. RECHERCHE TRAJET (Point A -> Point B)
# -------------------------------------------------------------------
@app.get("/search")
def search_all(
    origin: str = Query(..., description="Gare ou Ville de départ"),
    destination: str = Query(..., description="Gare ou Ville d'arrivée"),
    date: str = Query(..., description="Date au format YYYY-MM-DD"),
):
    conn = get_db_connection()
    cursor = conn.cursor()
    orig_term = f"%{origin.strip()}%"
    dest_term = f"%{destination.strip()}%"

    # --- A. TRAJETS DIRECTS ---
    query_direct = """
    SELECT 
        date, 
        origin_name AS orig, 
        destination_name AS dest, 
        departure_time AS train1_dep, 
        arrival_time AS train1_arr, 
        train_no AS train1_no
    FROM trips
    WHERE (UPPER(origin_name) LIKE UPPER(?) OR UPPER(origin_parent_station) LIKE UPPER(?))
      AND (UPPER(destination_name) LIKE UPPER(?) OR UPPER(destination_parent_station) LIKE UPPER(?))
      AND date = ?
    ORDER BY departure_time ASC
    """
    cursor.execute(
        query_direct, (orig_term, orig_term, dest_term, dest_term, date.strip())
    )
    direct_rows = [dict(row) for row in cursor.fetchall()]

    direct_results = []
    for d in direct_rows:
        direct_results.append({
            "is_direct": True,
            "orig": d["orig"],
            "dest": d["dest"],
            "date": d["date"],
            "train1_no": d["train1_no"],
            "train1_dep": d["train1_dep"],
            "train1_arr": d["train1_arr"],
            "transfer_station_arr": None,
            "transfer_station_dep": None,
            "train2_no": None,
            "train2_dep": None,
            "train2_arr": None,
            "layover_minutes": 0,
            "is_valid_layover": True,
        })

    # --- B. CORRESPONDANCES (Même gare OU changement de gare dans même ville) ---
    query_connections = """
    SELECT 
        t1.origin_name AS orig,
        t1.destination_name AS transfer_station_arr,
        t2.origin_name AS transfer_station_dep,
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
      ON UPPER(t1.destination_parent_station) = UPPER(t2.origin_parent_station)
     AND t1.date = t2.date
    WHERE (UPPER(t1.origin_name) LIKE UPPER(?) OR UPPER(t1.origin_parent_station) LIKE UPPER(?))
      AND (UPPER(t2.destination_name) LIKE UPPER(?) OR UPPER(t2.destination_parent_station) LIKE UPPER(?))
      AND t1.date = ?
      AND DATETIME(t2.date || ' ' || t2.departure_time) >= DATETIME(t1.date || ' ' || t1.arrival_time, '+15 minutes')
      AND DATETIME(t2.date || ' ' || t2.departure_time) <= DATETIME(t1.date || ' ' || t1.arrival_time, '+180 minutes')
    """
    cursor.execute(
        query_connections,
        (orig_term, orig_term, dest_term, dest_term, date.strip()),
    )
    conn_rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Évaluation dynamique du délai de correspondance
    valid_connections = []
    for c in conn_rows:
        c["is_direct"] = False
        is_same_station = c["transfer_station_arr"] == c["transfer_station_dep"]
        layover = c["layover_minutes"]

        if is_same_station:
            c["is_valid_layover"] = 15 <= layover <= 120
        else:
            c["is_valid_layover"] = 60 <= layover <= 180

        if c["is_valid_layover"]:
            valid_connections.append(c)

    cleaned_connections = cleanup_connections(valid_connections)

    # Fusion et tri final
    all_results = direct_results + cleaned_connections
    all_results.sort(key=lambda x: x["train1_dep"])

    return {"count": len(all_results), "results": all_results[:30]}


# -------------------------------------------------------------------
# 4. ROUTE EXPLORE (Point A -> TOUTES les destinations)
# -------------------------------------------------------------------
@app.get("/api/explore")
def explore_destinations(
    from_station: str = Query(
        ..., alias="from", description="Gare ou Ville de départ (ex: Rennes)"
    ),
    date: str = Query(..., description="Date au format YYYY-MM-DD"),
):
    conn = get_db_connection()
    cursor = conn.cursor()

    # Nettoyage des paramètres si transmission de plusieurs valeurs séparées par virgule
    stations = [s.strip() for s in from_station.split(",") if s.strip()]
    placeholders = ",".join(["?"] * len(stations))
    upper_stations = [s.upper() for s in stations]

    # Récupération de TOUTES les destinations au départ de la gare sélectionnée
    query = f"""
        SELECT 
            destination_name AS to_name,
            destination_parent_station AS to_id,
            MIN(departure_time) AS first_dep,
            arrival_time AS arr_time,
            train_no,
            dest_lat,
            dest_lon
        FROM trips
        WHERE date = ? 
          AND (UPPER(origin_name) IN ({placeholders}) OR UPPER(origin_parent_station) IN ({placeholders}))
        GROUP BY destination_parent_station, destination_name
        ORDER BY first_dep ASC
    """

    params = [date.strip()] + upper_stations + upper_stations
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    journeys = []
    for row in rows:
        dep_str = row["first_dep"]
        arr_str = row["arr_time"]

        # Calcul de la durée en minutes
        try:
            t1 = datetime.strptime(dep_str, "%H:%M")
            t2 = datetime.strptime(arr_str, "%H:%M")
            duration_min = int((t2 - t1).total_seconds() / 60)
            if duration_min < 0:
                duration_min += 24 * 60
        except Exception:
            duration_min = 0

        journeys.append({
            "dest_lat": row["dest_lat"] or 0,
            "dest_lon": row["dest_lon"] or 0,
            "duration": duration_min,
            "dep_str": dep_str,
            "arr_str": arr_str,
            "transfers": 0,
            "legs": [{
                "from_name": from_station,
                "to_name": row["to_name"],
                "to_id": row["to_id"],
                "dep_str": dep_str,
                "arr_str": arr_str,
                "train_no": row["train_no"],
                "lat": row["dest_lat"] or 0,
                "lon": row["dest_lon"] or 0,
            }],
        })

    return {"journeys": journeys}