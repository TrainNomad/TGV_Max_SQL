import sqlite3
import pandas as pd
import requests

# 1. Chargement et création du dictionnaire des métropoles
print("1. Chargement et résolution des gares parentes depuis stations.csv...")
df_stations = pd.read_csv('stations.csv', sep=';', low_memory=False)

# Mappage id -> name pour trouver le nom de la ville parente
id_to_name = df_stations.set_index('id')['name'].to_dict()

# Ajout de la colonne du nom de la ville parente
# Si pas de parent_station_id, la gare est sa propre ville parente
df_stations['parent_station'] = df_stations['parent_station_id'].map(id_to_name)
df_stations['parent_station'] = df_stations['parent_station'].fillna(df_stations['name'])

# Indexation par code IATA / SNCF (sncf_id)
df_stations_clean = df_stations.dropna(subset=['sncf_id']).copy()

# Dictionnaire de correspondance IATA -> Infos de la gare
iata_to_station = {}
for _, row in df_stations_clean.iterrows():
    iata_code = str(row['sncf_id']).strip().upper()
    iata_to_station[iata_code] = {
        'id': row['id'],
        'name': row['name'],
        'parent_station': row['parent_station']  # Ex: "Paris" pour Gare de Lyon
    }

# 2. Téléchargement des trajets TGV Max
DATA_URL = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/exports/json?lang=fr&timezone=Europe%2FBerlin"
print("2. Téléchargement des données TGV Max...")

response = requests.get(DATA_URL, stream=True)
response.raise_for_status()
raw_data = response.json()

print(f"Total d'enregistrements bruts reçus : {len(raw_data)}")

# 3. Traitement et enrichissement avec la ville parente
print("3. Nettoyage et enrichissement des trajets...")
records = []

for item in raw_data:
    if item.get('od_happy_card') != 'OUI':
        continue

    orig_iata = str(item.get('origine_iata', '')).strip().upper()
    dest_iata = str(item.get('destination_iata', '')).strip().upper()

    orig_info = iata_to_station.get(orig_iata, {
        'id': None, 'name': item.get('origine'), 'parent_station': item.get('origine')
    })
    dest_info = iata_to_station.get(dest_iata, {
        'id': None, 'name': item.get('destination'), 'parent_station': item.get('destination')
    })

    records.append({
        'date': item.get('date'),
        'origin_id': orig_info['id'],
        'origin_name': orig_info['name'],
        'origin_parent_station': orig_info['parent_station'],  # Ville de départ (ex: PARIS)
        'destination_id': dest_info['id'],
        'destination_name': dest_info['name'],
        'destination_parent_station': dest_info['parent_station'],  # Ville d'arrivée (ex: LYON)
        'departure_time': item.get('heure_depart'),
        'arrival_time': item.get('heure_arrivee'),
        'train_no': item.get('train_no')
    })

df_trips = pd.DataFrame(records)

# 4. Enregistrement dans SQLite avec schéma mis à jour
print("4. Génération de la base SQLite...")
conn = sqlite3.connect('tgvmax_compact.db')
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    origin_id INTEGER,
    origin_name TEXT,
    origin_parent_station TEXT,
    destination_id INTEGER,
    destination_name TEXT,
    destination_parent_station TEXT,
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    train_no TEXT
);
''')

# Indexation sur les villes et sur les gares pour des recherches ultra-rapides
cursor.execute('CREATE INDEX IF NOT EXISTS idx_parent_search ON trips (date, origin_parent_station, destination_parent_station);')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_exact_search ON trips (date, origin_name, destination_name);')

df_trips.to_sql('trips', conn, if_exists='replace', index=False)

conn.commit()
conn.close()

print("Base SQLite mise à jour avec succès avec la colonne parent_station !")