# Satellite Zone Scanner

Application web de téléchargement d'images satellite **Sentinel-2** à haute résolution (10 m/pixel), construite avec Streamlit et l'API Copernicus Data Space Ecosystem (CDSE).

Dessinez n'importe quelle zone sur la carte → l'app la découpe automatiquement en tuiles de 10 km → télécharge chaque tuile → assemble une mosaïque complète.

---

## Table des matières

1. [Ce que fait l'application](#ce-que-fait-lapplication)
2. [Prérequis](#prérequis)
3. [Installation](#installation)
4. [Obtenir les clés API Copernicus](#obtenir-les-clés-api-copernicus)
5. [Lancer l'application](#lancer-lapplication)
6. [Guide d'utilisation pas à pas](#guide-dutilisation-pas-à-pas)
7. [Assembler la mosaïque avec mosaic.py](#assembler-la-mosaïque-avec-mosaicpy)
8. [Structure des fichiers téléchargés](#structure-des-fichiers-téléchargés)
9. [Questions fréquentes](#questions-fréquentes)
10. [Dépannage](#dépannage)

---

## Ce que fait l'application

- **Carte interactive** (Folium/Leaflet) pour dessiner votre zone d'intérêt
- **Recherche catalogue** : liste les passages satellites disponibles sur votre zone avec leur taux de couverture nuageuse, sans télécharger le moindre pixel
- **Balayage par tuiles** : découpe automatiquement la zone en tuiles de ≤ 10 km, télécharge chacune à la résolution native Sentinel-2 (10 m/pixel)
- **Inspecteur** : visualisez et ajustez le rendu (Gain / Gamma) tuile par tuile
- **Galerie** : vue d'ensemble reconstituant la grille, nord en haut
- **Mosaïque** : script séparé (`mosaic.py`) pour coller toutes les tuiles en une seule grande image sans couture

---

## Prérequis

| Logiciel | Version minimale | Vérification |
|---|---|---|
| Python | 3.10 ou supérieur | `python --version` |
| pip | récent | `pip --version` |
| Git | n'importe laquelle | `git --version` |

> **Windows** : utilisez le terminal **PowerShell** ou **Git Bash**. Les commandes `source` deviennent `.\.venv\Scripts\activate`.

---

## Installation

### 1. Cloner le dépôt

```bash
git clone https://github.com/votre-nom/satellite-zone-scanner.git
cd satellite-zone-scanner
```

### 2. Créer un environnement virtuel Python

Un environnement virtuel isole les dépendances du projet de votre Python système. **Recommandé.**

```bash
# Créer l'environnement
python -m venv venv_satellite

# L'activer (Mac / Linux)
source venv_satellite/bin/activate

# L'activer (Windows PowerShell)
.\venv_satellite\Scripts\activate
```

Vous devriez voir `(venv_satellite)` apparaître au début de votre ligne de commande.

### 3. Installer les dépendances

```bash
pip install -r requirements.txt
```

Cela installe Streamlit, Folium, la bibliothèque Sentinel Hub, NumPy, Pillow, etc.

> **Note** : l'installation peut prendre 1 à 3 minutes selon votre connexion.

### 4. Renseigner les clés API

Les clés s'entrent **directement dans la sidebar de l'application**, dans la section **🔑 Credentials Copernicus**. Vous n'avez besoin de les saisir qu'une fois par session.

**Optionnel — pré-remplissage automatique via `.env`**

Si vous ne voulez pas ressaisir vos clés à chaque démarrage, créez un fichier `.env` à la racine du projet :

```
SH_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SH_CLIENT_SECRET=votre_secret_ici
```

L'application détecte ce fichier et pré-remplit les champs de la sidebar automatiquement. Sans ce fichier, tout fonctionne exactement pareil — vous remplissez juste les champs à la main.

> **Si vous utilisez `.env`** : ne le commitez jamais sur GitHub. Il est déjà listé dans `.gitignore`.

---

## Obtenir les clés API Copernicus

L'application utilise l'**API Copernicus Data Space Ecosystem (CDSE)** pour accéder aux images Sentinel-2. L'accès est **gratuit** avec un quota généreux.

### Étape 1 : Créer un compte

1. Allez sur [dataspace.copernicus.eu](https://dataspace.copernicus.eu)
2. Cliquez sur **Register** (en haut à droite)
3. Remplissez le formulaire et validez votre e-mail

### Étape 2 : Créer un client OAuth2

1. Connectez-vous à votre compte
2. Cliquez sur votre nom d'utilisateur (en haut à droite) → **User Settings**
3. Dans le menu de gauche, cliquez sur **OAuth Clients**
4. Cliquez sur **+ Add** ou **Create new client**
5. Donnez-lui un nom (ex. : `satellite-tracker`)
6. Cliquez sur **Create**
7. Copiez le **Client ID** et le **Client Secret** qui s'affichent

> **Attention** : le Client Secret ne s'affiche qu'une seule fois. Copiez-le immédiatement dans votre fichier `.env`.

### Quota gratuit

Le quota gratuit CDSE permet de télécharger plusieurs centaines de km² par mois. Pour une zone de 50 × 50 km (25 tuiles de 10 km), chaque téléchargement consomme environ 25 requêtes API.

---

## Lancer l'application

```bash
# Assurez-vous que l'environnement virtuel est activé
source venv_satellite/bin/activate   # Mac / Linux
# ou
.\venv_satellite\Scripts\activate    # Windows

# Lancer l'application
streamlit run satellite_tracker.py
```

Streamlit ouvre automatiquement votre navigateur à l'adresse `http://localhost:8501`.

---

## Guide d'utilisation pas à pas

### Vue d'ensemble de l'interface

```
┌─────────────────────────────────────────────────────────────────┐
│  Sidebar (gauche)          │  Zone principale                   │
│  ─ Nom de la zone          │  ─ Carte interactive               │
│  ─ Fond de carte           │  ─ Onglets :                       │
│  ─ Plage de dates          │      📡 Sentinel-2 (principal)     │
│  ─ Couverture nuageuse     │      BBox / GeoJSON / Code         │
│  ─ Credentials Copernicus  │                                    │
│  ─ Instructions            │                                    │
│  ─ GPS + stats zone        │                                    │
└─────────────────────────────────────────────────────────────────┘
```

---

### Étape 0 : Nommer votre zone

Dans la sidebar, remplissez le champ **"Nom de la zone d'intérêt"** (par défaut : `Zone_Alpha`).

Ce nom est utilisé comme nom de dossier pour stocker les images :
```
downloads/Zone_Alpha/2024-05-15/tile_0_0.npy
```

Choisissez un nom sans espaces ni caractères spéciaux (ex. : `Port_Marseille`, `Golfe_Persique`).

---

### Étape 1 : Dessiner la zone sur la carte

1. Sur la carte, repérez votre zone d'intérêt (naviguez avec la souris, zoomez avec la molette)
2. Dans la barre d'outils de la carte (à gauche), cliquez sur l'outil **Rectangle** (icône ▬)
3. **Cliquez et faites glisser** pour dessiner votre zone

Dès que vous relâchez la souris :
- La bounding box bleue s'affiche
- Une **grille orange** apparaît immédiatement, montrant comment la zone sera découpée en tuiles de 10 km
- Dans la sidebar, les coordonnées GPS et la surface totale s'affichent

> **Conseil** : commencez par une petite zone (quelques tuiles) pour tester. Une zone de 20 × 20 km = 4 tuiles = environ 2 minutes de téléchargement.

---

### Étape 2 : Configurer la recherche

Dans la sidebar, ajustez :

- **Plage de dates** : période pendant laquelle chercher des images (par défaut : les 30 derniers jours)
- **Couverture nuageuse max.** : filtrer les images trop nuageuses (0% = ciel parfaitement clair, 100% = tout accepter)

---

### Étape 3 : Chercher les passages satellites

Dans l'onglet **📡 Sentinel-2**, cliquez sur :

```
🔍 Chercher les passages satellites
```

Cette étape interroge uniquement le **catalogue STAC** — aucun pixel n'est téléchargé. C'est très rapide (quelques secondes).

Le tableau qui s'affiche liste tous les passages disponibles, triés du moins nuageux au plus nuageux :

| Date | Heure UTC | Nuages (%) | ID |
|---|---|---|---|
| 2024-05-15 | 10:25 | 🟢 2.3% | ... |
| 2024-05-12 | 10:25 | 🟡 35.1% | ... |

---

### Étape 4 : Sélectionner un passage

Dans le menu déroulant **"Sélectionner un passage"**, choisissez la date qui vous intéresse. Préférez les passages avec peu de nuages (icône 🟢).

---

### Étape 5 : Lancer le balayage

Cliquez sur le bouton principal :

```
⬇️ Lancer le balayage de la zone (N tuile(s) — YYYY-MM-DD)
```

L'application :
1. Crée le dossier `downloads/{zone}/{date}/`
2. Sauvegarde un fichier `metadata.json` avec les informations de la grille
3. Télécharge chaque tuile une par une (barre de progression en temps réel)
4. Sauvegarde deux fichiers par tuile :
   - `tile_{col}_{row}.npy` : données brutes (float32, pour traitement ultérieur)
   - `tile_{col}_{row}.png` : aperçu visuel (prêt à l'emploi)

> **Durée** : environ 3 à 5 secondes par tuile (incluant un délai de politesse de 0,3 s pour ménager l'API). Une zone de 10 × 10 km (1 tuile) ≈ 5 secondes. Une zone de 50 × 50 km (25 tuiles) ≈ 2 minutes.

---

### Étape 6 : Inspecter les tuiles

Après le téléchargement, un **inspecteur** apparaît automatiquement.

- **Menu déroulant** : sélectionnez une tuile
- **Gain** (1.0 → 5.0) : amplifie la luminosité. Utile pour les zones sombres (forêt, eau).
- **Gamma** (0.5 → 3.0) : ajuste le contraste. γ > 1 débouche les ombres.
- **Bouton 💾** : télécharge la tuile avec vos réglages actuels

---

### Étape 7 : Galerie mosaïque

Cliquez sur **"🗺️ Afficher la galerie de la zone"** pour voir toutes les tuiles assemblées en grille, avec le Nord en haut.

---

## Assembler la mosaïque avec mosaic.py

Le script `mosaic.py` colle toutes les tuiles en **une seule grande image** sans couture, en préservant la résolution native de 10 m/pixel.

### Utilisation interactive (recommandée pour débuter)

```bash
python mosaic.py
```

Le script vous pose deux questions :
1. Quelle zone ? (affiche la liste des zones disponibles)
2. Quelle date ?

### Utilisation en ligne de commande

```bash
# Basique
python mosaic.py -z Zone_Alpha -d 2024-05-15

# Avec réglages d'exposition personnalisés
python mosaic.py -z Zone_Alpha -d 2024-05-15 --gain 3.0 --gamma 1.2

# Réduire la taille de l'image de moitié (utile pour les grandes zones)
python mosaic.py -z Zone_Alpha -d 2024-05-15 --scale 0.5

# Exporter aussi un GeoTIFF géoréférencé (ouvrable dans QGIS)
python mosaic.py -z Zone_Alpha -d 2024-05-15 --geotiff

# Lister les zones et dates disponibles
python mosaic.py --list
```

### Options disponibles

| Option | Défaut | Description |
|---|---|---|
| `-z`, `--zone` | — | Nom de la zone (ex. `Zone_Alpha`) |
| `-d`, `--date` | — | Date au format `YYYY-MM-DD` |
| `--gain` | `2.0` | Luminosité (1.0 = naturel, 3.0 = très lumineux) |
| `--gamma` | `1.5` | Contraste (1.0 = linéaire, 2.0 = ombres éclaircies) |
| `--scale` | `1.0` | Réduction de taille (0.5 = moitié, 0.25 = quart) |
| `--geotiff` | désactivé | Exporte un `.tif` géoréférencé |
| `--list` | — | Liste les zones/dates disponibles et quitte |

### Export GeoTIFF (optionnel)

Le GeoTIFF est un format d'image géoréférencé lisible dans **QGIS**, **ArcGIS**, Google Earth Engine, etc.

Pour l'activer, installez `rasterio` :

```bash
pip install rasterio
```

Puis ajoutez `--geotiff` à votre commande. Le fichier `mosaic.tif` sera georéférencé en **WGS84 (EPSG:4326)** si un fichier `metadata.json` est présent dans le dossier de la zone.

### Estimation de la taille des fichiers

| Zone | Tuiles | Résolution native | PNG sortie |
|---|---|---|---|
| 10 × 10 km | 1 | 1 000 × 1 000 px | ~2 MB |
| 50 × 50 km | 25 | 5 000 × 5 000 px | ~40 MB |
| 100 × 100 km | 100 | 10 000 × 10 000 px | ~200 MB |
| 200 × 200 km | 400 | 20 000 × 20 000 px | ~800 MB |

> Pour les grandes zones, utilisez `--scale 0.5` pour diviser la taille par 4.

---

## Structure des fichiers téléchargés

```
downloads/
└── Zone_Alpha/                        ← nom de la zone
    └── 2024-05-15/                    ← date du passage satellite
        ├── metadata.json              ← infos grille, bbox, nuages
        ├── tile_0_0.npy               ← données brutes tuile (col=0, ligne=0)
        ├── tile_0_0.png               ← aperçu tuile
        ├── tile_1_0.npy               ← tuile suivante (col=1, ligne=0)
        ├── tile_1_0.png
        ├── tile_0_1.npy               ← ligne suivante (col=0, ligne=1)
        ├── tile_0_1.png
        ├── ...
        ├── mosaic_g2.0_y1.5.png       ← mosaïque assemblée (mosaic.py)
        └── mosaic.tif                 ← GeoTIFF géoréférencé (optionnel)
```

**Convention de nommage des tuiles** :
- `tile_{col}_{row}` où `col` = colonne (Ouest → Est) et `row` = ligne (Sud → Nord)
- La tuile `tile_0_0` est le coin **Sud-Ouest** de la zone

**Format `.npy`** : tableau NumPy float32 de forme `(hauteur, largeur, 3)`, valeurs entre 0.0 et 1.0 (réflectance brute). Utilisable directement avec NumPy/Python pour des analyses personnalisées.

---

## Questions fréquentes

**Q : J'obtiens une image noire après téléchargement.**

Il y a deux causes possibles :
1. La zone choisie n'a aucune couverture Sentinel-2 pour cette date précise (peu probable si le catalogue l'a listée)
2. Le taux de nuages réel est plus élevé que celui indiqué par le catalogue — essayez une autre date

**Q : L'application me dit "Aucune acquisition" dans le catalogue.**

Vérifiez :
- Que vos credentials sont corrects (section 🔑 dans la sidebar)
- Que la plage de dates est suffisamment large (au moins 2-3 semaines)
- Que le seuil de couverture nuageuse n'est pas trop restrictif (essayez 50%)

**Q : Le téléchargement d'une tuile échoue.**

Le script continue sur les autres tuiles et liste les échecs à la fin. Les causes habituelles :
- Quota API temporairement dépassé → attendez quelques minutes et relancez
- Zone sans couverture pour cette date → choisissez une autre acquisition

**Q : Puis-je télécharger des images d'une autre constellation que Sentinel-2 ?**

Non, l'application est configurée uniquement pour Sentinel-2 L2A (images en couleur naturelle True Color).

**Q : Quelle est la résolution exacte des images ?**

10 mètres par pixel — c'est la résolution native des bandes rouge, vert et bleu de Sentinel-2. Chaque pixel de 10 m × 10 m au sol.

**Q : Puis-je analyser les données brutes `.npy` ?**

Oui. Chaque fichier `.npy` est un tableau NumPy `float32` de forme `(H, W, 3)` contenant la réflectance de surface (valeurs entre 0.0 et 1.0) pour les bandes Rouge (B04), Vert (B03) et Bleu (B02).

```python
import numpy as np
img = np.load("downloads/Zone_Alpha/2024-05-15/tile_0_0.npy")
print(img.shape)   # (1000, 1000, 3)
print(img.min(), img.max())   # 0.0  0.35 (environ)
```

---

## Dépannage

### Erreur `ModuleNotFoundError`

```
ModuleNotFoundError: No module named 'sentinelhub'
```

L'environnement virtuel n'est pas activé ou les dépendances ne sont pas installées :

```bash
source venv_satellite/bin/activate
pip install -r requirements.txt
```

### Erreur d'authentification Copernicus

```
HTTPError: 401 Unauthorized
```

Vérifiez que votre `Client ID` et `Client Secret` sont corrects dans le fichier `.env` ou dans la section 🔑 de la sidebar.

### La carte ne s'affiche pas

Vérifiez que le package `streamlit-folium` est bien installé :

```bash
pip install streamlit-folium
```

### Streamlit version incompatible

L'application nécessite Streamlit ≥ 1.37 (pour `@st.fragment`). Mettez à jour si besoin :

```bash
pip install --upgrade streamlit
```

### Port 8501 déjà utilisé

```bash
streamlit run satellite_tracker.py --server.port 8502
```

---

## Licence

Ce projet est open source. Voir le fichier `LICENSE` pour les détails.

Les images Sentinel-2 sont fournies par l'**Agence Spatiale Européenne (ESA)** et distribuées gratuitement via le programme **Copernicus** de l'Union Européenne.
