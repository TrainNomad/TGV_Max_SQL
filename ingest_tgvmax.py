import sqlite3
import pandas as pd
import requests
from datetime import datetime

# 1. Chargement et résolution des gares
print("1. Chargement des gares depuis stations.csv...")
df_stations = pd.read_csv('stations.csv', sep=';', low_memory=False)

# Nettoyage des valeurs manquantes
df_stations['name'] = df_stations['name'].fillna('Gare Inconnue')
df_stations['latitude'] = pd.to_numeric(df_stations['latitude'], errors='coerce').fillna(0.0)
df_stations['longitude'] = pd.to_numeric(df_stations['longitude'], errors='coerce').fillna(0.0)

# Mappage id -> infos complètes
id_to_name = df_stations.set_index('id')['name'].to_dict()
id_to_parent = df_stations.set_index('id')['parent_station_id'].to_dict()
id_to_lat = df_stations.set_index('id')['latitude'].to_dict()
id_to_lon = df_stations.set_index('id')['longitude'].to_dict()

# Résolution des noms des stations parentes
def get_parent_name(parent_id):
    if pd.isna(parent_id):
        return None
    parent_id = int(parent_id)
    return id_to_name.get(parent_id)

df_stations['parent_station'] = df_stations['parent_station_id'].apply(get_parent_name)
df_stations['parent_station'] = df_stations['parent_station'].fillna(df_stations['name'])

# Dictionnaire IATA -> ID
df_stations_clean = df_stations.dropna(subset=['sncf_id', 'id']).copy()
iata_to_id = {}
for _, row in df_stations_clean.iterrows():
    iata_code = str(row['sncf_id']).strip().upper()
    iata_to_id[iata_code] = int(row['id'])

print(f"Stations chargées: {len(df_stations)}")

# 2. Téléchargement des trajets TGV Max
DATA_URL = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/exports/json?lang=fr&timezone=Europe%2FBerlin"
print("2. Téléchargement des données TGV Max...")

try:
    response = requests.get(DATA_URL, stream=True, timeout=60)
    response.raise_for_status()
    raw_data = response.json()
    print(f"Enregistrements bruts reçus : {len(raw_data)}")
except Exception as e:
    print(f"❌ Erreur téléchargement : {e}")
    exit(1)

# 3. Traitement et DÉNORMALISATION des données
print("3. Nettoyage et dénormalisation des trajets...")
records = []

for item in raw_data:
    # Filtre TGV Max
    if item.get('od_happy_card') != 'OUI':
        continue

    orig_iata = str(item.get('origine_iata', '')).strip().upper()
    dest_iata = str(item.get('destination_iata', '')).strip().upper()

    origin_id = iata_to_id.get(orig_iata)
    destination_id = iata_to_id.get(dest_iata)

    # Exclure si gares non trouvées
    if origin_id is None or destination_id is None:
        continue

    # 🔑 DÉNORMALISATION : Stocker DIRECTEMENT toutes les données utiles
    records.append({
        'date': item.get('date'),
        'origin_id': origin_id,
        'origin_name': id_to_name.get(origin_id, 'Unknown'),
        'origin_parent_station': df_stations[df_stations['id'] == origin_id]['parent_station'].values[0] if origin_id in id_to_name else 'Unknown',
        'origin_lat': id_to_lat.get(origin_id, 0.0),
        'origin_lon': id_to_lon.get(origin_id, 0.0),
        'destination_id': destination_id,
        'destination_name': id_to_name.get(destination_id, 'Unknown'),
        'destination_parent_station': df_stations[df_stations['id'] == destination_id]['parent_station'].values[0] if destination_id in id_to_name else 'Unknown',
        'dest_lat': id_to_lat.get(destination_id, 0.0),
        'dest_lon': id_to_lon.get(destination_id, 0.0),
        'departure_time': item.get('heure_depart'),
        'arrival_time': item.get('heure_arrivee'),
        'train_no': item.get('train_no')
    })

df_trips = pd.DataFrame(records)
print(f"Trajets à insérer : {len(df_trips)}")

if len(df_trips) == 0:
    print("❌ Aucun trajet trouvé!")
    exit(1)

# 4. Génération de la base SQLite OPTIMISÉE
print("4. Création de la base SQLite...")
conn = sqlite3.connect('tgvmax_compact.db')
cursor = conn.cursor()

# Configuration SQLite
cursor.execute('PRAGMA journal_mode = WAL;')
cursor.execute('PRAGMA synchronous = NORMAL;')
cursor.execute('PRAGMA cache_size = -64000;')
cursor.execute('PRAGMA temp_store = MEMORY;')

# Suppression des anciennes tables
cursor.execute('DROP TABLE IF EXISTS trips;')
cursor.execute('DROP INDEX IF EXISTS idx_direct_search;')
cursor.execute('DROP INDEX IF EXISTS idx_transfer_source;')
cursor.execute('DROP INDEX IF EXISTS idx_transfer_dest;')
cursor.execute('DROP INDEX IF EXISTS idx_date_origin;')

# ✅ NOUVELLE TABLE : Dénormalisée pour performance
cursor.execute('''
CREATE TABLE trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    origin_id INTEGER NOT NULL,
    origin_name TEXT NOT NULL,
    origin_parent_station TEXT NOT NULL,
    origin_lat REAL NOT NULL,
    origin_lon REAL NOT NULL,
    destination_id INTEGER NOT NULL,
    destination_name TEXT NOT NULL,
    destination_parent_station TEXT NOT NULL,
    dest_lat REAL NOT NULL,
    dest_lon REAL NOT NULL,
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    train_no TEXT
);
''')

# Insertion des données
df_trips.to_sql('trips', conn, if_exists='append', index=False)
print(f"✅ {len(df_trips)} trajets insérés")

# 5. Création des INDEX OPTIMISÉS pour les vrais patterns de requête
print("5. Création des index...")

# Index 1 : Recherche directe (A -> B)
cursor.execute('''
CREATE INDEX idx_direct_search 
ON trips (date, origin_name, destination_name, departure_time);
''')

# Index 2 : Recherche directe par métropole (parent station)
cursor.execute('''
CREATE INDEX idx_direct_parent_search 
ON trips (date, origin_parent_station, destination_parent_station, departure_time);
''')

# Index 3 : Correspondances - départ (pour trouver les 1er trajets)
cursor.execute('''
CREATE INDEX idx_transfer_source 
ON trips (date, origin_name, departure_time);
''')

# Index 4 : Correspondances - arrivée (pour trouver les trajets de correspondance)
cursor.execute('''
CREATE INDEX idx_transfer_dest 
ON trips (date, origin_parent_station, departure_time, arrival_time);
''')

# Index 5 : Par date et métropole de départ (pour explorer)
cursor.execute('''
CREATE INDEX idx_explorer_source 
ON trips (date, origin_name, destination_parent_station);
''')

# Index 6 : Par date et métropole de destination
cursor.execute('''
CREATE INDEX idx_explorer_parent_dest 
ON trips (date, origin_parent_station, destination_name);
''')

conn.commit()

# 6. Statistiques et vérification
cursor.execute("SELECT COUNT(*) FROM trips;")
count = cursor.fetchone()[0]
print(f"✅ {count} trajets dans la base")

cursor.execute("SELECT COUNT(DISTINCT date) FROM trips;")
dates = cursor.fetchone()[0]
print(f"✅ {dates} dates disponibles")

cursor.execute("SELECT COUNT(DISTINCT origin_name) FROM trips;")
origins = cursor.fetchone()[0]
print(f"✅ {origins} gares de départ")

conn.close()
print("\n✅ Base SQLite générée et optimisée avec succès!")