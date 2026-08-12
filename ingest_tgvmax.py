import sqlite3
import pandas as pd
import requests

# 1. Chargement et indexation par CODE IATA / SNCF_ID (et non par nom)
print("1. Chargement du fichier stations.csv...")
df_stations = pd.read_csv('stations.csv', sep=';', low_memory=False)

# On filtre les gares qui possèdent un code sncf_id / IATA (ex: FRLPD, FRLPE, etc.)
df_stations_clean = df_stations.dropna(subset=['sncf_id', 'name']).copy()

# Dictionnaire de correspondance : CODE IATA -> ID & NOM DE GARE RÉEL
# ex: 'FRLPD' -> {'id': 123, 'name': 'Lyon Part Dieu'}
iata_to_station = {}
for _, row in df_stations_clean.iterrows():
  iata_code = str(row['sncf_id']).strip().upper()
  iata_to_station[iata_code] = {'id': row['id'], 'name': row['name']}

# 2. Téléchargement du fichier JSON TGV Max
DATA_URL = 'https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/exports/json?lang=fr&timezone=Europe%2FBerlin'
print('2. Téléchargement des données TGV Max...')

response = requests.get(DATA_URL, stream=True)
response.raise_for_status()
raw_data = response.json()

print(f"Total d'enregistrements bruts reçus : {len(raw_data)}")

# 3. Filtrage et Normalisation via les codes IATA
print('3. Nettoyage, filtrage et mapping des gares via IATA...')
records = []

for item in raw_data:
  if item.get('od_happy_card') != 'OUI':
    continue

  # RÉCUPÉRATION DES CODES IATA (Ex: FRLPD au lieu de LYON INTRAMUROS)
  orig_iata = str(item.get('origine_iata', '')).strip().upper()
  dest_iata = str(item.get('destination_iata', '')).strip().upper()

  # Match avec notre référentiel de gares
  orig_info = iata_to_station.get(
      orig_iata,
      {'id': None, 'name': item.get('origine')},  # Fallback sur nom brut si inconnu
  )
  dest_info = iata_to_station.get(
      dest_iata, {'id': None, 'name': item.get('destination')}
  )

  records.append({
      'date': item.get('date'),
      'origin_id': orig_info['id'],
      'origin_name': orig_info[
          'name'
      ],  # Affiche "Lyon Part Dieu" au lieu de "LYON (INTRAMUROS)"
      'destination_id': dest_info['id'],
      'destination_name': dest_info['name'],
      'departure_time': item.get('heure_depart'),
      'arrival_time': item.get('heure_arrivee'),
      'train_no': item.get('train_no'),
  })

df_trips = pd.DataFrame(records)
print(f'Enregistrements TGV Max valides conservés : {len(df_trips)}')

# 4. Exportation vers SQLite
print('4. Génération de la base SQLite (tgvmax_compact.db)...')
conn = sqlite3.connect('tgvmax_compact.db')
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    origin_id INTEGER,
    origin_name TEXT,
    destination_id INTEGER,
    destination_name TEXT,
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    train_no TEXT
);
''')

# Index pour accélérer les requêtes
cursor.execute(
    'CREATE INDEX IF NOT EXISTS idx_direct ON trips (date, origin_name,'
    ' destination_name);'
)

# Insertion des trajets
df_trips.to_sql('trips', conn, if_exists='replace', index=False)

conn.commit()
conn.close()

print(
    'Traitement terminé ! Les gares Intramuros sont désormais ventilées en'
    ' gares réelles.'
)