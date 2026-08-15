import sqlite3
import pandas as pd
import requests

# 1. Chargement et création du dictionnaire des métropoles & coordonnées
print("1. Chargement et résolution des gares depuis stations.csv...")
df_stations = pd.read_csv('stations.csv', sep=';', low_memory=False)

# Mappage id -> name pour trouver le nom de la ville parente
id_to_name = df_stations.set_index('id')['name'].to_dict()

# Ajout de la colonne du nom de la ville parente
df_stations['parent_station'] = df_stations['parent_station_id'].map(id_to_name)
df_stations['parent_station'] = df_stations['parent_station'].fillna(df_stations['name'])

# Indexation par code IATA / SNCF (sncf_id)
df_stations_clean = df_stations.dropna(subset=['sncf_id']).copy()

# Dictionnaire de correspondance IATA -> Infos de la gare (avec latitude & longitude)
iata_to_station = {}
for _, row in df_stations_clean.iterrows():
    iata_code = str(row['sncf_id']).strip().upper()
    
    # Conversion securisee des coordonnees
    try:
        lat = float(row['latitude']) if pd.notna(row['latitude']) else 0.0
    except (ValueError, TypeError):
        lat = 0.0
        
    try:
        lon = float(row['longitude']) if pd.notna(row['longitude']) else 0.0
    except (ValueError, TypeError):
        lon = 0.0

    iata_to_station[iata_code] = {
        'id': row['id'],
        'name': row['name'],
        'parent_station': row['parent_station'],
        'latitude': lat,
        'longitude': lon
    }

# 2. Téléchargement des trajets TGV Max
DATA_URL = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/exports/json?lang=fr&timezone=Europe%2FBerlin"
print("2. Téléchargement des données TGV Max...")

response = requests.get(DATA_URL, stream=True)
response.raise_for_status()
raw_data = response.json()

print(f"Total d'enregistrements bruts reçus : {len(raw_data)}")

# 3. Traitement et enrichissement avec la ville parente et les coordonnées
print("3. Nettoyage et enrichissement des trajets...")
records = []

for item in raw_data:
    if item.get('od_happy_card') != 'OUI':
        continue

    orig_iata = str(item.get('origine_iata', '')).strip().upper()
    dest_iata = str(item.get('destination_iata', '')).strip().upper()

    orig_info = iata_to_station.get(orig_iata, {
        'id': None, 'name': item.get('origine'), 'parent_station': item.get('origine'),
        'latitude': 0.0, 'longitude': 0.0
    })
    dest_info = iata_to_station.get(dest_iata, {
        'id': None, 'name': item.get('destination'), 'parent_station': item.get('destination'),
        'latitude': 0.0, 'longitude': 0.0
    })

    records.append({
        'date': item.get('date'),
        'origin_id': orig_info['id'],
        'origin_name': orig_info['name'],
        'origin_parent_station': orig_info['parent_station'],
        'origin_lat': orig_info['latitude'],
        'origin_lon': orig_info['longitude'],
        'destination_id': dest_info['id'],
        'destination_name': dest_info['name'],
        'destination_parent_station': dest_info['parent_station'],
        'dest_lat': dest_info['latitude'],
        'dest_lon': dest_info['longitude'],
        'departure_time': item.get('heure_depart'),
        'arrival_time': item.get('heure_arrivee'),
        'train_no': item.get('train_no')
    })

df_trips = pd.DataFrame(records)

# 4. Enregistrement dans SQLite avec le nouveau schéma
print("4. Génération de la base SQLite...")
conn = sqlite3.connect('tgvmax_compact.db')
cursor = conn.cursor()

cursor.execute('DROP TABLE IF EXISTS trips;')

cursor.execute('''
CREATE TABLE trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    origin_id INTEGER,
    origin_name TEXT,
    origin_parent_station TEXT,
    origin_lat REAL,
    origin_lon REAL,
    destination_id INTEGER,
    destination_name TEXT,
    destination_parent_station TEXT,
    dest_lat REAL,
    dest_lon REAL,
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    train_no TEXT
);
''')

# Insérer les données dans la table
df_trips.to_sql('trips', conn, if_exists='append', index=False)

cursor.execute('DROP INDEX IF EXISTS idx_parent_search;')
cursor.execute('DROP INDEX IF EXISTS idx_exact_search;')

# Index 1 : Trajets directs (Recherche instantanée par Date + Gare de départ)
cursor.execute('''
CREATE INDEX IF NOT EXISTS idx_direct_search 
ON trips (date, origin_parent_station, origin_name);
''')

# Index 2 : Correspondances (Jointure rapide Date + Gare de correspondance)
cursor.execute('''
CREATE INDEX IF NOT EXISTS idx_transfer_search 
ON trips (date, origin_parent_station, departure_time);
''')

conn.commit()
conn.close()

print("Base SQLite régénérée avec succès avec les coordonnées GPS !")