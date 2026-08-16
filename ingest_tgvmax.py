import os
import sqlite3
import pandas as pd
import requests

# Helper pour déterminer le type de train selon l'axe
def detect_train_type(item):
    axe = str(item.get('axe', '')).upper().strip()
    if axe.startswith("OUIGO"):
        return "OUIGO"
    if axe.startswith("IC"):
        return "INTERCITÉS"
    return "TGV INOUI"

# 1. Chargement et résolution des gares
print("1. Chargement des gares...")
df_stations = pd.read_csv('stations.csv', sep=';', low_memory=False)

df_stations['name'] = df_stations['name'].fillna('Gare Inconnue')
df_stations['latitude'] = pd.to_numeric(df_stations['latitude'], errors='coerce').fillna(0.0)
df_stations['longitude'] = pd.to_numeric(df_stations['longitude'], errors='coerce').fillna(0.0)

id_to_name = df_stations.set_index('id')['name'].to_dict()
id_to_parent_id = df_stations.set_index('id')['parent_station_id'].to_dict()

def resolve_parent_id(s_id):
    p_id = id_to_parent_id.get(s_id)
    if pd.isna(p_id) or not p_id:
        return int(s_id)
    return int(p_id)

df_stations['parent_id'] = df_stations['id'].apply(resolve_parent_id)
id_to_parent_id = df_stations.set_index('id')['parent_id'].to_dict()
id_to_parent_name = {s_id: id_to_name.get(p_id, id_to_name.get(s_id)) for s_id, p_id in id_to_parent_id.items()}
id_to_lat = df_stations.set_index('id')['latitude'].to_dict()
id_to_lon = df_stations.set_index('id')['longitude'].to_dict()

df_stations_clean = df_stations.dropna(subset=['sncf_id', 'id']).copy()
iata_to_id = {str(row['sncf_id']).strip().upper(): int(row['id']) for _, row in df_stations_clean.iterrows()}

def time_to_minutes(t_str):
    if not t_str or ':' not in t_str:
        return 0
    h, m = t_str.split(':')
    return int(h) * 60 + int(m)

# 2. Téléchargement TGV Max
DATA_URL = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/exports/json?lang=fr&timezone=Europe%2FBerlin"
print("2. Téléchargement TGV Max...")

response = requests.get(DATA_URL, stream=True, timeout=60)
response.raise_for_status()
raw_data = response.json()

# 3. Traitement
print("3. Traitement des trajets...")
records = []

for item in raw_data:
    if item.get('od_happy_card') != 'OUI':
        continue

    orig_iata = str(item.get('origine_iata', '')).strip().upper()
    dest_iata = str(item.get('destination_iata', '')).strip().upper()

    orig_id = iata_to_id.get(orig_iata)
    dest_id = iata_to_id.get(dest_iata)

    if not orig_id or not dest_id:
        continue

    dep_time = item.get('heure_depart', '00:00')
    arr_time = item.get('heure_arrivee', '00:00')

    records.append({
        'date': item.get('date'),
        'origin_id': orig_id,
        'origin_parent_id': id_to_parent_id.get(orig_id, orig_id),
        'origin_name': id_to_name.get(orig_id, 'Inconnue'),
        'origin_parent_name': id_to_parent_name.get(orig_id, 'Inconnue'),
        'origin_lat': id_to_lat.get(orig_id, 0.0),
        'origin_lon': id_to_lon.get(orig_id, 0.0),
        
        'destination_id': dest_id,
        'destination_parent_id': id_to_parent_id.get(dest_id, dest_id),
        'destination_name': id_to_name.get(dest_id, 'Inconnue'),
        'destination_parent_name': id_to_parent_name.get(dest_id, 'Inconnue'),
        'dest_lat': id_to_lat.get(dest_id, 0.0),
        'dest_lon': id_to_lon.get(dest_id, 0.0),

        'departure_time': dep_time,
        'arrival_time': arr_time,
        'dep_min': time_to_minutes(dep_time),
        'arr_min': time_to_minutes(arr_time),
        'train_no': item.get('train_no'),
        'train_type': detect_train_type(item)
    })

df_trips = pd.DataFrame(records)

# 4. Enregistrement SQLite (Chemin absolu)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, 'tgvmax_compact.db')

print(f"4. Écriture dans SQLite ({db_path})...")
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute('PRAGMA journal_mode = WAL;')
cursor.execute('PRAGMA synchronous = NORMAL;')

cursor.execute('DROP TABLE IF EXISTS trips;')
cursor.execute('''
CREATE TABLE trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    origin_id INTEGER NOT NULL,
    origin_parent_id INTEGER NOT NULL,
    origin_name TEXT NOT NULL,
    origin_parent_name TEXT NOT NULL,
    origin_lat REAL,
    origin_lon REAL,
    destination_id INTEGER NOT NULL,
    destination_parent_id INTEGER NOT NULL,
    destination_name TEXT NOT NULL,
    destination_parent_name TEXT NOT NULL,
    dest_lat REAL,
    dest_lon REAL,
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    dep_min INTEGER NOT NULL,
    arr_min INTEGER NOT NULL,
    train_no TEXT,
    train_type TEXT NOT NULL
);
''')

df_trips.to_sql('trips', conn, if_exists='append', index=False)

# 5. Indexation optimisée
print("5. Création des index composites...")
cursor.execute('CREATE INDEX idx_search_direct ON trips(date, origin_parent_id, destination_parent_id, dep_min);')
cursor.execute('CREATE INDEX idx_search_transfer ON trips(date, origin_parent_id, dep_min, arr_min);')

conn.commit()
conn.close()
print("✅ Terminé !")