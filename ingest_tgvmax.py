import sqlite3
import requests
import json
import pandas as pd

# 1. Chargement et indexation du référentiel des gares (stations.csv)
print("1. Chargement du fichier stations.csv...")
df_stations = pd.read_csv('stations.csv', sep=';', low_memory=False)

# Nettoyage et création d'un dictionnaire de correspondance (Nom / UIC -> Station ID)
# On privilégie les gares principales ou celles disposant d'un code SNCF / UIC
df_stations_clean = df_stations.dropna(subset=['name']).copy()
df_stations_clean['clean_name'] = df_stations_clean['name'].str.strip().str.upper()

# Table de correspondance : Nom nettoyé -> ID unique
station_to_id = dict(zip(df_stations_clean['clean_name'], df_stations_clean['id']))

# 2. Téléchargement du fichier JSON TGV Max
DATA_URL = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/exports/json?lang=fr&timezone=Europe%2FBerlin"
print("2. Téléchargement des données TGV Max...")

response = requests.get(DATA_URL, stream=True)
response.raise_for_status()
raw_data = response.json()

print(f"Total d'enregistrements bruts reçus : {len(raw_data)}")

# 3. Filtrage et Normalisation des Données
print("3. Nettoyage, filtrage et mapping des gares...")
records = []
unknown_stations = set()

for item in raw_data:
    # Filtrer uniquement les trajets avec disponibilité TGV Max
    if item.get('od_happy_card') != 'OUI':
        continue

    orig_name = str(item.get('origine', '')).strip().upper()
    dest_name = str(item.get('destination', '')).strip().upper()

    # Recherche de l'ID de gare dans le référentiel stations.csv
    orig_id = station_to_id.get(orig_name)
    dest_id = station_to_id.get(dest_name)

    # Si la gare n'est pas trouvée directement, enregistrer pour diagnostic
    if not orig_id:
        unknown_stations.add(orig_name)
    if not dest_id:
        unknown_stations.add(dest_name)

    records.append({
        'date': item.get('date'),
        'origin_id': orig_id,
        'origin_name': orig_name,
        'destination_id': dest_id,
        'destination_name': dest_name,
        'departure_time': item.get('heure_depart'),
        'arrival_time': item.get('heure_arrivee'),
        'train_no': item.get('train_no')
    })

df_trips = pd.DataFrame(records)
print(f"Enregistrements TGV Max valides conservés : {len(df_trips)}")

# 4. Exportation vers une base SQLite optimisée
print("4. Génération de la base SQLite (tgvmax_compact.db)...")
conn = sqlite3.connect('tgvmax_compact.db')
cursor = conn.cursor()

# Table des gares référencées dans les trajets
cursor.execute('''
CREATE TABLE IF NOT EXISTS stations (
    station_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
''')

# Table des trajets compressée
cursor.execute('''
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    origin_id INTEGER,
    destination_id INTEGER,
    origin_name TEXT,
    destination_name TEXT,
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    train_no TEXT
);
''')

# Création des index composites pour accélérer les recherches et correspondances
cursor.execute('CREATE INDEX IF NOT EXISTS idx_direct ON trips (date, origin_name, destination_name);')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_origin ON trips (date, origin_name);')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_destination ON trips (date, destination_name);')

# Inserer les trajets
df_trips.to_sql('trips', conn, if_exists='replace', index=False)

conn.commit()
conn.close()

print("Traitement terminé avec succès ! Fichier tgvmax_compact.db généré.")