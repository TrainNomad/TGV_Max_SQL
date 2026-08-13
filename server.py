import sqlite3
from typing import Any, Dict, List
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="TGV Max Visualizer API",
    description=(
        "API de recherche TGV Max avec support des métropoles et correspondances"
        " inter-gares."
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
def get_stations(
    q: str = Query(
        None, description="Recherche partielle de gare ou de ville"
    ),
):
  conn = get_db_connection()
  cursor = conn.cursor()

  if q and q.strip():
    search_term = f"%{q.strip()}%"
    query = """
        SELECT DISTINCT name FROM (
            SELECT origin_parent_station AS name FROM trips WHERE UPPER(origin_parent_station) LIKE UPPER(?)
            UNION
            SELECT destination_parent_station AS name FROM trips WHERE UPPER(destination_parent_station) LIKE UPPER(?)
            UNION
            SELECT origin_name AS name FROM trips WHERE UPPER(origin_name) LIKE UPPER(?)
            UNION
            SELECT destination_name AS name FROM trips WHERE UPPER(destination_name) LIKE UPPER(?)
        )
        ORDER BY name ASC
        LIMIT 20
        """
    cursor.execute(query, (search_term, search_term, search_term, search_term))
  else:
    query = """
        SELECT DISTINCT name FROM (
            SELECT origin_parent_station AS name FROM trips
            UNION
            SELECT destination_parent_station AS name FROM trips
        )
        ORDER BY name ASC
        """
    cursor.execute(query)

  stations = [row["name"] for row in cursor.fetchall() if row["name"]]
  conn.close()

  return {"count": len(stations), "stations": stations}


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
# 3. ROUTE PRINCIPALE DE RECHERCHE
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
      -- Filtre global de sécurité minimale (au moins 15 min dans tous les cas)
      AND DATETIME(t2.date || ' ' || t2.departure_time) >= DATETIME(t1.date || ' ' || t1.arrival_time, '+15 minutes')
      -- Max 3h d'escale
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
      # Même quai/gare : entre 15 et 120 min
      c["is_valid_layover"] = 15 <= layover <= 120
    else:
      # Changement de gare (ex: Montparnasse -> Gare de Lyon) : au moins 60 min jusqu'à 180 min
      c["is_valid_layover"] = 60 <= layover <= 180

    if c["is_valid_layover"]:
      valid_connections.append(c)

  cleaned_connections = cleanup_connections(valid_connections)

  # --- C. FUSION ET TRI FINAL ---
  all_results = direct_results + cleaned_connections
  all_results.sort(key=lambda x: x["train1_dep"])

  return {"count": len(all_results), "results": all_results[:30]}