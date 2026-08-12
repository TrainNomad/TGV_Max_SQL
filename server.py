from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
from typing import List, Dict, Any

app = FastAPI(
    title="TGV Max Visualizer API",
    description="API de recherche de billets TGV Max avec calcul de trajets directs et correspondances",
    version="1.1.0"
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
    return {"status": "ok", "message": "API TGV Max opérationnelle"}

@app.get("/stations")
def get_stations(q: str = Query(None, description="Recherche partielle de la gare (autocomplétion)")):
    conn = get_db_connection()
    cursor = conn.cursor()

    if q and q.strip():
        # Si un terme de recherche 'q' est fourni, filtre avec LIKE %q%
        search_term = f"%{q.strip()}%"
        query = """
        SELECT DISTINCT name FROM (
            SELECT origin_name AS name FROM trips WHERE UPPER(origin_name) LIKE UPPER(?)
            UNION
            SELECT destination_name AS name FROM trips WHERE UPPER(destination_name) LIKE UPPER(?)
        )
        ORDER BY name ASC
        LIMIT 20
        """
        cursor.execute(query, (search_term, search_term))
    else:
        # Si aucun paramètre 'q' n'est fourni, renvoie toutes les stations (ou une liste vide selon votre besoin)
        query = """
        SELECT DISTINCT name FROM (
            SELECT origin_name AS name FROM trips
            UNION
            SELECT destination_name AS name FROM trips
        )
        ORDER BY name ASC
        """
        cursor.execute(query)

    stations = [row["name"] for row in cursor.fetchall()]
    conn.close()
    
    return {"count": len(stations), "stations": stations}
# -------------------------------------------------------------------
# FONCTION DE DÉDUPLICATION & NETTOYAGE
# -------------------------------------------------------------------
def cleanup_connections(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # 1. Dédupliquer par clé exacte
    unique = {}
    for r in results:
        key = f"{r['train1_no']}|{r['transfer_station']}|{r['train2_no']}"
        if key not in unique:
            unique[key] = r

    # 2. Regrouper par paire de trains (train1 + train2)
    par_trains = {}
    for r in unique.values():
        train_key = f"{r['train1_no']}_{r['train2_no']}"
        if train_key not in par_trains:
            par_trains[train_key] = []
        par_trains[train_key].append(r)

    # 3. Conserver l'alternative optimale
    final = []
    gares_bizarres = ['AEROPORT', 'CDG', 'MASSY', 'MARNE', 'LYON']

    for alts in par_trains.values():
        # Filtre sur le temps d'escale viable (15 à 120 minutes)
        valides = [a for a in alts if 15 <= a['layover_minutes'] <= 120]
        if not valides:
            final.append(alts[0])
            continue

        # Priorise les gares classiques si alternatives
        non_bizarres = [
            a for a in valides 
            if not any(g in a['transfer_station'].upper() for g in gares_bizarres)
        ]

        if non_bizarres:
            final.append(non_bizarres[0])
        else:
            final.append(valides[0])

    # 4. Tri par heure de départ du 1er train puis durée totale
    final.sort(key=lambda x: (x['train1_dep'], x['layover_minutes']))
    return final

# -------------------------------------------------------------------
# ROUTE GLOBALE DE RECHERCHE (Directs + Correspondances dédupliqués)
# -------------------------------------------------------------------
@app.get("/search")
def search_all(
    origin: str = Query(..., description="Gare de départ"),
    destination: str = Query(..., description="Gare d'arrivée"),
    date: str = Query(..., description="Date au format YYYY-MM-DD"),
    min_layover_mins: int = Query(15, description="Temps d'escale minimum"),
    max_layover_mins: int = Query(120, description="Temps d'escale maximum")
):
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Requête Directs
    query_direct = """
    SELECT date, origin_name AS orig, destination_name AS dest, 
           departure_time AS train1_dep, arrival_time AS train1_arr, train_no AS train1_no
    FROM trips
    WHERE UPPER(origin_name) LIKE UPPER(?)
      AND UPPER(destination_name) LIKE UPPER(?)
      AND date = ?
    ORDER BY departure_time ASC
    """
    cursor.execute(query_direct, (f"%{origin.strip()}%", f"%{destination.strip()}%", date.strip()))
    direct_rows = [dict(row) for row in cursor.fetchall()]

    # Formatage des trajets directs pour l'unification
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
            "transfer_station": None,
            "train2_no": None,
            "train2_dep": None,
            "train2_arr": None,
            "layover_minutes": 0
        })

    # 2. Requête Correspondances
    query_connections = """
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
    """
    cursor.execute(query_connections, (
        f"%{origin.strip()}%", 
        f"%{destination.strip()}%", 
        date.strip(),
        str(min_layover_mins),
        str(max_layover_mins)
    ))
    conn_rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Traitement des correspondances avec le filtre de déduplication
    for c in conn_rows:
        c["is_direct"] = False

    cleaned_connections = cleanup_connections(conn_rows)

    # Fusion des trajets directs et des correspondances nettoyées (Max 20 résultats)
    all_results = direct_results + cleaned_connections
    all_results.sort(key=lambda x: x["train1_dep"])

    return {
        "count": len(all_results),
        "results": all_results[:20]
    }