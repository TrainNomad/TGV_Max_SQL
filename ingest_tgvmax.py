import sqlite3
import pandas as pd
import requests

# 1. Chargement et résolution des gares
print("1. Chargement des gares depuis stations.csv...")
df_stations = pd.read_csv('stations.csv', sep=';', low_memory=False)

# Mappage id -> name pour trouver le nom de la gare parente
id_to_name = df_stations.set_index('id')['name'].to_dict()

# Résolution des stations parentes
df_stations['parent_station'] = df_stations['parent_station_id'].map(id_to_name)
df_stations['parent_station'] = df_stations['parent_station'].fillna(df_stations['name'])

# Nettoyage et conversion des coordonnées
df_stations['latitude'] = pd.to_numeric(df_stations['latitude'], errors='coerce').fillna(0.0)
df_stations['longitude'] = pd.to_numeric(df_stations['longitude'], errors='coerce').fillna(0.0)

# Dictionnaire de correspondance IATA -> ID de station
df_stations_clean = df_stations.dropna(subset=['sncf_id']).copy()
iata_to_id = {}
for _, row in df_stations_clean.iterrows():
    iata_code = str(row['sncf_id']).strip().upper()
    iata_to_id[iata_code] = int(row['id'])

# 2. Téléchargement des trajets TGV Max
DATA_URL = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/exports/json?lang=fr&timezone=Europe%2FBerlin"
print("2. Téléchargement des données TGV Max...")

response = requests.get(DATA_URL, stream=True)
response.raise_for_status()
raw_data = response.json()

print(f"Total d'enregistrements bruts reçus : {len(raw_data)}")

# 3. Traitement et extraction des identifiants uniquement
print("3. Nettoyage et préparation des trajets...")
records = []

for item in raw_data:
    if item.get('od_happy_card') != 'OUI':
        continue

    orig_iata = str(item.get('origine_iata', '')).strip().upper()
    dest_iata = str(item.get('destination_iata', '')).strip().upper()

    origin_id = iata_to_id.get(orig_iata)
    destination_id = iata_to_id.get(dest_iata)

    # Exclure les enregistrements dont les gares ne sont pas identifiées
    if origin_id is None or destination_id is None:
        continue

    records.append({
        'date': item.get('date'),
        'origin_id': origin_id,
        'destination_id': destination_id,
        'departure_time': item.get('heure_depart'),
        'arrival_time': item.get('heure_arrivee'),
        'train_no': item.get('train_no')
    })

df_trips = pd.DataFrame(records)

# 4. Génération de la base SQLite optimisée
print("4. Génération de la base SQLite...")
conn = sqlite3.connect('tgvmax_compact.db')
cursor = conn.cursor()

# Configuration SQLite pour maximiser la vitesse
cursor.execute('PRAGMA journal_mode = WAL;')
cursor.execute('PRAGMA synchronous = NORMAL;')

# Reconstitution de la table des gares
cursor.execute('DROP TABLE IF EXISTS stations;')
cursor.execute('''
CREATE TABLE stations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_station TEXT NOT NULL,
    latitude REAL,
    longitude REAL
);
''')

df_stations_to_db = df_stations[['id', 'name', 'parent_station', 'latitude', 'longitude']].drop_duplicates(subset=['id'])
df_stations_to_db.to_sql('stations', conn, if_exists='append', index=False)

# Reconstitution de la table des trajets
cursor.execute('DROP TABLE IF EXISTS trips;')
cursor.execute('''
CREATE TABLE trips (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    origin_id INTEGER NOT NULL,
    destination_id INTEGER NOT NULL,
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    train_no TEXT,
    FOREIGN KEY(origin_id) REFERENCES stations(id),
    FOREIGN KEY(destination_id) REFERENCES stations(id)
);
''')

df_trips.to_sql('trips', conn, if_exists='append', index=False)

# 5. Création des index composites ultra-performants
print("5. Création des index...")
cursor.execute('DROP INDEX IF EXISTS idx_direct_search;')
cursor.execute('DROP INDEX IF EXISTS idx_transfer_search;')

# Index 1 : Requêtes directes (Date + Départ + Arrivée)
cursor.execute('''
CREATE INDEX idx_direct_search 
ON trips (date, origin_id, destination_id, departure_time);
''')

# Index 2 : Recherche de correspondances / départs par horaire
cursor.execute('''
CREATE INDEX idx_transfer_search 
ON trips (date, origin_id, departure_time);
''')

conn.commit()
conn.close()

print("Base SQLite générée et optimisée avec succès !")