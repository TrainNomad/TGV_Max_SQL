#!/usr/bin/env python3
"""
Script de test de performance pour l'API TGV Max
Mesure les temps de réponse des différents endpoints
"""

import sqlite3
import time
import statistics
from typing import List, Tuple

DB_PATH = "tgvmax_compact.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA cache_size=-64000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn

def measure_time(func, *args, n=1) -> Tuple[float, any]:
    """Mesure le temps d'exécution d'une fonction"""
    times = []
    result = None
    
    for _ in range(n):
        start = time.perf_counter()
        result = func(*args)
        end = time.perf_counter()
        times.append((end - start) * 1000)  # en millisecondes
    
    return statistics.mean(times), statistics.stdev(times) if len(times) > 1 else 0, result

# ============================================================================
# TEST 1: Vérification de la structure de la base
# ============================================================================
def test_structure():
    print("=" * 80)
    print("TEST 1: Structure de la base de données")
    print("=" * 80)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Vérifier les colonnes de trips
    cursor.execute("PRAGMA table_info(trips);")
    columns = cursor.fetchall()
    print("\n✅ Colonnes de la table 'trips':")
    for col in columns:
        print(f"   - {col['name']}: {col['type']}")
    
    # Vérifier les index
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trips';")
    indexes = cursor.fetchall()
    print(f"\n✅ {len(indexes)} index trouvés:")
    for idx in indexes:
        print(f"   - {idx['name']}")
    
    # Statistiques générales
    cursor.execute("SELECT COUNT(*) as count, COUNT(DISTINCT date) as dates, COUNT(DISTINCT origin_name) as origins FROM trips;")
    stats = dict(cursor.fetchone())
    print(f"\n✅ Statistiques:")
    print(f"   - Trajets: {stats['count']:,}")
    print(f"   - Dates: {stats['dates']}")
    print(f"   - Gares de départ: {stats['origins']}")
    
    conn.close()

# ============================================================================
# TEST 2: Autocomplétion
# ============================================================================
def test_autocomplete():
    print("\n" + "=" * 80)
    print("TEST 2: Autocomplétion (/stations)")
    print("=" * 80)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    test_cases = [
        ("r", "Une lettre"),
        ("ren", "Trois lettres"),
        ("rennes", "Gare complète"),
        ("paris", "Autre gare"),
    ]
    
    for search, description in test_cases:
        def query():
            upper_q = search.upper()
            cursor.execute("""
                SELECT DISTINCT origin_name AS name 
                FROM trips 
                WHERE UPPER(origin_name) LIKE ?
                LIMIT 10
            """, (upper_q + "%",))
            return cursor.fetchall()
        
        avg_time, stdev, results = measure_time(query, n=3)
        print(f"  '{search}' ({description}): {avg_time:.2f}ms (σ={stdev:.2f}ms) → {len(results)} résultats")
    
    conn.close()

# ============================================================================
# TEST 3: Recherche directe
# ============================================================================
def test_direct_search():
    print("\n" + "=" * 80)
    print("TEST 3: Recherche directe (A→B) (/search)")
    print("=" * 80)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Trouver une date valide
    cursor.execute("SELECT DISTINCT date FROM trips LIMIT 1;")
    date = cursor.fetchone()['date']
    
    # Trouver deux gares valides
    cursor.execute("SELECT DISTINCT origin_name FROM trips LIMIT 1;")
    origin = cursor.fetchone()['origin_name']
    
    cursor.execute("SELECT DISTINCT destination_name FROM trips WHERE origin_name != ? LIMIT 1;", (origin,))
    destination = cursor.fetchone()['destination_name']
    
    print(f"\n  Recherche: {origin} → {destination} ({date})")
    
    def query():
        cursor.execute("""
            SELECT * FROM trips
            WHERE date = ?
              AND (UPPER(origin_name) = ? OR UPPER(origin_parent_station) = ?)
              AND (UPPER(destination_name) = ? OR UPPER(destination_parent_station) = ?)
            ORDER BY departure_time ASC
            LIMIT 50
        """, (date, origin.upper(), origin.upper(), destination.upper(), destination.upper()))
        return cursor.fetchall()
    
    avg_time, stdev, results = measure_time(query, n=5)
    print(f"  ✅ Trajets directs: {avg_time:.2f}ms (σ={stdev:.2f}ms) → {len(results)} résultats")
    
    # Correspondances
    def query_transfer():
        cursor.execute("""
            SELECT COUNT(*) as count FROM (
                SELECT t1.train_no, t2.train_no 
                FROM trips t1
                JOIN trips t2 
                  ON UPPER(t1.destination_parent_station) = UPPER(t2.origin_parent_station)
                 AND t1.date = t2.date
                WHERE t1.date = ?
                  AND (UPPER(t1.origin_name) = ? OR UPPER(t1.origin_parent_station) = ?)
                  AND (UPPER(t2.destination_name) = ? OR UPPER(t2.destination_parent_station) = ?)
                  AND DATETIME(t2.date || ' ' || t2.departure_time) >= DATETIME(t1.date || ' ' || t1.arrival_time, '+15 minutes')
                  AND DATETIME(t2.date || ' ' || t2.departure_time) <= DATETIME(t1.date || ' ' || t1.arrival_time, '+180 minutes')
                LIMIT 100
            )
        """, (date, origin.upper(), origin.upper(), destination.upper(), destination.upper()))
        return cursor.fetchone()['count']
    
    avg_time, stdev, transfers = measure_time(query_transfer, n=5)
    print(f"  ✅ Correspondances: {avg_time:.2f}ms (σ={stdev:.2f}ms) → {transfers} résultats")
    
    print(f"\n  📊 Total recherche A→B: {avg_time + avg_time:.2f}ms")
    
    conn.close()

# ============================================================================
# TEST 4: Explorer (routes)
# ============================================================================
def test_explorer():
    print("\n" + "=" * 80)
    print("TEST 4: Explorer destinations (/explorer)")
    print("=" * 80)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Trouver une date et une gare
    cursor.execute("SELECT DISTINCT date FROM trips LIMIT 1;")
    date = cursor.fetchone()['date']
    
    cursor.execute("SELECT origin_name FROM trips GROUP BY origin_name ORDER BY COUNT(*) DESC LIMIT 1;")
    station = cursor.fetchone()['origin_name']
    
    print(f"\n  Exploration depuis: {station} ({date})")
    
    # Trajets directs
    def query_direct():
        cursor.execute("""
            SELECT destination_name, destination_parent_station, dest_lat, dest_lon
            FROM trips
            WHERE date = ? 
              AND (UPPER(origin_name) = ? OR UPPER(origin_parent_station) = ?)
              AND UPPER(destination_parent_station) != ?
            ORDER BY departure_time ASC
            LIMIT 1000
        """, (date, station.upper(), station.upper(), station.upper()))
        return cursor.fetchall()
    
    avg_time, stdev, results = measure_time(query_direct, n=5)
    print(f"  ✅ Trajets directs: {avg_time:.2f}ms (σ={stdev:.2f}ms) → {len(results)} destinations")
    
    # Trajets avec correspondance
    def query_transfers():
        cursor.execute("""
            SELECT COUNT(DISTINCT t2.destination_name) as count FROM trips t1
            JOIN trips t2 
              ON UPPER(t1.destination_parent_station) = UPPER(t2.origin_parent_station)
             AND t1.date = t2.date
            WHERE t1.date = ?
              AND (UPPER(t1.origin_name) = ? OR UPPER(t1.origin_parent_station) = ?)
              AND UPPER(t2.destination_parent_station) != ?
              AND DATETIME(t2.date || ' ' || t2.departure_time) >= DATETIME(t1.date || ' ' || t1.arrival_time, '+15 minutes')
              AND DATETIME(t2.date || ' ' || t2.departure_time) <= DATETIME(t1.date || ' ' || t1.arrival_time, '+180 minutes')
        """, (date, station.upper(), station.upper(), station.upper()))
        return cursor.fetchone()['count']
    
    avg_time, stdev, transfers = measure_time(query_transfers, n=3)
    print(f"  ✅ Avec 1 correspondance: {avg_time:.2f}ms (σ={stdev:.2f}ms) → {transfers} destinations")
    
    conn.close()

# ============================================================================
# TEST 5: Vérification des index
# ============================================================================
def test_indexes():
    print("\n" + "=" * 80)
    print("TEST 5: Vérification des index")
    print("=" * 80)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Exemples de plans de requête
    cursor.execute("SELECT date FROM trips LIMIT 1;")
    date = cursor.fetchone()['date']
    
    cursor.execute("SELECT origin_name FROM trips LIMIT 1;")
    origin = cursor.fetchone()['origin_name']
    
    print(f"\n  Exemple de plan pour recherche directe:")
    cursor.execute("EXPLAIN QUERY PLAN SELECT * FROM trips WHERE date=? AND origin_name=? LIMIT 50;", (date, origin))
    plan = cursor.fetchall()
    for row in plan:
        print(f"    {row}")
    
    conn.close()

# ============================================================================
# MAIN
# ============================================================================
if __name__ == "__main__":
    print("\n")
    print("╔" + "=" * 78 + "╗")
    print("║" + " " * 20 + "TEST DE PERFORMANCE - TGV MAX API" + " " * 26 + "║")
    print("╚" + "=" * 78 + "╝")
    
    try:
        test_structure()
        test_autocomplete()
        test_direct_search()
        test_explorer()
        test_indexes()
        
        print("\n" + "=" * 80)
        print("✅ TOUS LES TESTS COMPLÉTÉS")
        print("=" * 80 + "\n")
        
    except Exception as e:
        print(f"\n❌ ERREUR: {e}")
        import traceback
        traceback.print_exc()