"""
Satellite Ship Tracker — Phase 2 : Sentinel-2 via Copernicus Data Space Ecosystem
  • Zone nommée + quadrillage automatique ≤ 10 km par tuile
  • Stockage structuré : downloads/{zone}/{date}/tile_{X}_{Y}.npy+.png
  • 3 étapes : catalog → sélection → balayage batch avec progress bar
  • Inspecteur par tuile (Gain/Gamma) + galerie globale reconstituant la grille
"""

import io
import json
import math
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
import folium
from folium.plugins import Draw, MiniMap
from streamlit_folium import st_folium

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from sentinelhub import (
        SHConfig, BBox, CRS, DataCollection,
        SentinelHubRequest, SentinelHubCatalog,
        MimeType, bbox_to_dimensions,
    )
    SH_AVAILABLE = True
except ImportError:
    SH_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# ─────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────
CDSE_BASE_URL  = "https://sh.dataspace.copernicus.eu"
CDSE_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu"
    "/auth/realms/CDSE/protocol/openid-connect/token"
)
TILE_MAX_KM = 10.0   # 10 km × 100 px/km = 1000 px à 10 m/px
MAX_PX      = 1024

TILE_OPTIONS = {
    "Satellite (Esri)": (
        "https://server.arcgisonline.com/ArcGIS/rest/services"
        "/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
    "Sombre (CartoDB)": "CartoDB dark_matter",
    "Clair (CartoDB)":  "CartoDB positron",
    "OpenStreetMap":    "OpenStreetMap",
}

# FLOAT32, réflectance [0.0, 1.0] — normalisation déléguée à apply_tone()
EVALSCRIPT_TRUE_COLOR = """
//VERSION=3
function setup() {
    return {
        input: [{ bands: ["B04", "B03", "B02"] }],
        output: { bands: 3, sampleType: "FLOAT32" }
    };
}
function evaluatePixel(sample) {
    return [sample.B04, sample.B03, sample.B02];
}
"""


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def build_sh_config(client_id: str, client_secret: str) -> "SHConfig":
    cfg = SHConfig()
    cfg.sh_client_id     = client_id
    cfg.sh_client_secret = client_secret
    cfg.sh_base_url      = CDSE_BASE_URL
    cfg.sh_token_url     = CDSE_TOKEN_URL
    return cfg


def get_cdse_collection() -> "DataCollection":
    return DataCollection.SENTINEL2_L2A.define_from(
        "cdse-sentinel-2-l2a", service_url=CDSE_BASE_URL
    )


def generate_grid(bbox: dict, max_size_km: float = TILE_MAX_KM) -> list[dict]:
    """
    Découpe bbox en tuiles de max_size_km × max_size_km.
    Tuile (0,0) = coin sud-ouest (lat_min, lon_min).
    Les tuiles de bord sont plus petites si la zone ne tombe pas juste.
    """
    lat_mid  = (bbox["lat_min"] + bbox["lat_max"]) / 2.0
    lat_step = max_size_km / 111.32
    lon_step = max_size_km / (111.32 * math.cos(math.radians(lat_mid)))

    tiles, lat, row = [], bbox["lat_min"], 0
    while lat < bbox["lat_max"] - 1e-9:
        lat_end = min(lat + lat_step, bbox["lat_max"])
        lon, col = bbox["lon_min"], 0
        while lon < bbox["lon_max"] - 1e-9:
            lon_end = min(lon + lon_step, bbox["lon_max"])
            tiles.append({
                "row":     row, "col":     col,
                "lon_min": round(lon,     6), "lat_min": round(lat,     6),
                "lon_max": round(lon_end, 6), "lat_max": round(lat_end, 6),
            })
            lon = lon_end
            col += 1
        lat = lat_end
        row += 1
    return tiles



def search_catalog(config, aoi_bbox, time_interval: tuple,
                   max_cloud: int) -> list[dict]:
    """Catalogue STAC CDSE → list d'acquisitions triées par nuages croissants."""
    collection = get_cdse_collection()
    catalog    = SentinelHubCatalog(config=config)
    raw = list(catalog.search(
        collection, bbox=aoi_bbox, time=time_interval,
        filter=f"eo:cloud_cover < {max_cloud}",
        fields={"include": ["id", "properties.datetime", "properties.eo:cloud_cover"]},
    ))
    items = []
    for r in raw:
        props    = r.get("properties", {})
        iso_full = props.get("datetime", "")
        date_str = iso_full[:10]
        time_str = iso_full[11:19] if len(iso_full) >= 19 else "??"
        cc       = round(props.get("eo:cloud_cover", 0.0), 1)
        icon     = "🟢" if cc < 10 else ("🟡" if cc < 40 else "🔴")
        items.append({
            "id":          r.get("id", "—"),
            "datetime":    iso_full,
            "date":        date_str,
            "time":        time_str,
            "cloud_cover": cc,
            "label":       f"{date_str}  {time_str[:5]} UTC  {icon} {cc}%",
        })
    return sorted(items, key=lambda x: x["cloud_cover"])


def download_tile(
    config, tile: dict, acquisition: dict, output_dir: Path
) -> tuple["np.ndarray | None", "Path | None"]:
    """
    Télécharge une tuile (TIFF FLOAT32), sauvegarde :
      - tile_{X}_{Y}.npy  (X=col, Y=row) → rechargé à la demande par l'Inspecteur
      - tile_{X}_{Y}.png  → aperçu pré-calculé pour la Galerie (gain 2.0, gamma 1.5)
    Retourne (img_float, npy_path) ou (None, None) si échec.
    """
    date_str = acquisition["date"]
    dt       = datetime.fromisoformat(acquisition["datetime"].replace("Z", "+00:00"))
    t1       = (dt + timedelta(days=1)).strftime("%Y-%m-%d")

    aoi_bbox_sh = BBox(
        bbox=[tile["lon_min"], tile["lat_min"], tile["lon_max"], tile["lat_max"]],
        crs=CRS.WGS84,
    )
    size = bbox_to_dimensions(aoi_bbox_sh, resolution=10)

    request = SentinelHubRequest(
        data_folder=str(output_dir / "_raw"),
        evalscript=EVALSCRIPT_TRUE_COLOR,
        input_data=[SentinelHubRequest.input_data(
            data_collection=get_cdse_collection(),
            time_interval=(date_str, t1),
            maxcc=1.0,
            mosaicking_order="mostRecent",
        )],
        responses=[SentinelHubRequest.output_response("default", MimeType.TIFF)],
        bbox=aoi_bbox_sh,
        size=size,
        config=config,
    )
    data = request.get_data(save_data=False)
    if not data or data[0] is None:
        return None, None

    img_float = data[0].astype(np.float32)
    stem      = f"tile_{tile['col']}_{tile['row']}"   # X=col, Y=row

    npy_path = output_dir / f"{stem}.npy"
    np.save(str(npy_path), img_float)

    if PIL_AVAILABLE:
        Image.fromarray(apply_tone(img_float, 2.0, 1.5)).save(
            str(output_dir / f"{stem}.png")
        )

    return img_float, npy_path


def apply_tone(img_float: np.ndarray, gain: float, gamma: float) -> np.ndarray:
    """Gain → Gamma → uint8 [0, 255]."""
    return (np.clip(img_float * gain, 0.0, 1.0) ** (1.0 / gamma) * 255).astype(np.uint8)


def array_to_png_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────
# CONFIG PAGE
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Satellite Ship Tracker",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    [data-testid="stMetric"] {
        background-color: #1a1d27;
        border: 1px solid #2d3146;
        border-radius: 8px;
        padding: 12px 16px;
        margin-bottom: 8px;
    }
    [data-testid="stMetricLabel"] { font-size: 0.75rem; color: #8b95a8; }
    [data-testid="stMetricValue"] { font-size: 1.1rem; color: #00d4ff; font-weight: 700; }
    .step-header {
        background: #1a1d27;
        border-left: 3px solid #00d4ff;
        padding: 6px 14px;
        border-radius: 0 6px 6px 0;
        margin: 12px 0 8px 0;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
# SIDEBAR — Part 1
# ─────────────────────────────────────────────
with st.sidebar:
    st.title("🛰️ Ship Tracker")
    st.caption("Phase 2 — Sentinel-2 CDSE · Tiling")
    st.divider()

    zone_name = st.text_input(
        "Nom de la zone d'intérêt",
        value="Zone_Alpha",
        placeholder="Zone_Alpha",
        help="Utilisé comme dossier de stockage : downloads/{zone}/{date}/",
    )
    # Invalidate tiles/catalog when zone name changes (guard: skip on first load)
    if zone_name != st.session_state.get("last_zone_name"):
        _prev_zone = st.session_state.get("last_zone_name")
        st.session_state["last_zone_name"] = zone_name
        for k in ("catalog_results", "search_version", "last_tiles", "last_tiles_meta"):
            st.session_state.pop(k, None)
        if _prev_zone is not None:
            st.rerun()

    st.divider()

    tile_label = st.selectbox("Fond de carte", list(TILE_OPTIONS.keys()), index=0)

    st.divider()

    st.markdown("**Plage de dates**")
    today      = date.today()
    date_start = st.date_input("Date de début", value=today - timedelta(days=30),
                               max_value=today)
    date_end   = st.date_input("Date de fin", value=today, max_value=today)
    if date_start > date_end:
        st.warning("Date de début > date de fin.")

    st.divider()

    st.markdown("**Couverture nuageuse max.**")
    max_cloud = st.slider("Cloud Cover (%)", 0, 100, 20, step=5)

    st.divider()

    with st.expander("🔑 Credentials Copernicus",
                     expanded=not os.getenv("SH_CLIENT_ID")):
        client_id = st.text_input(
            "Client ID",
            value=os.getenv("SH_CLIENT_ID", ""),
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        )
        client_secret = st.text_input(
            "Client Secret",
            value=os.getenv("SH_CLIENT_SECRET", ""),
            type="password",
            placeholder="••••••••••••••••",
        )
        st.caption(
            "[dataspace.copernicus.eu](https://dataspace.copernicus.eu) → "
            "User Settings → OAuth Clients.  \n"
            "Ou variables `SH_CLIENT_ID` / `SH_CLIENT_SECRET` dans `.env`."
        )

    st.divider()
    st.markdown("**Instructions**")
    st.markdown("""
1. Dessinez un rectangle (outil **▬** sur la carte)
2. La grille en orange s'affiche automatiquement
3. **Étape 1** → Chercher les passages satellites
4. **Étape 2** → Sélectionner une date
5. **Étape 3** → Télécharger toutes les tuiles
6. Inspectez et ajustez Gain/Gamma par tuile
""")
    st.divider()

    if st.button("Effacer la sélection", use_container_width=True):
        for k in ("bbox", "geojson_aoi", "map_center",
                  "catalog_results", "search_version",
                  "last_tiles", "last_tiles_meta", "last_zone_name"):
            st.session_state.pop(k, None)
        st.rerun()


# ─────────────────────────────────────────────
# CARTE FOLIUM — fragment isolé
# @st.fragment : les interactions carte (pan, zoom) ne re-rendent QUE
# ce bloc, sans toucher la sidebar ni les tabs → navigation fluide.
# returned_objects=["all_drawings","zoom"] : le pan ne déclenche PLUS
# de rerun Streamlit (centre non renvoyé → 0 callback sur le pan).
# ─────────────────────────────────────────────
@st.fragment
def _render_map(tile_label: str) -> None:
    _bbox  = st.session_state.get("bbox")
    _grid  = generate_grid(_bbox) if _bbox else []

    _tv       = TILE_OPTIONS[tile_label]
    _builtin  = _tv in ("CartoDB dark_matter", "CartoDB positron", "OpenStreetMap")

    m = folium.Map(
        location=st.session_state.get("map_center", [30.0, 10.0]),
        zoom_start=st.session_state.get("map_zoom", 3),
        tiles=_tv if _builtin else None,
        control_scale=True,
    )
    if not _builtin:
        folium.TileLayer(tiles=_tv, attr="Esri World Imagery",
                         name=tile_label).add_to(m)

    MiniMap(toggle_display=True, position="bottomright").add_to(m)

    Draw(
        draw_options={
            "polyline": False, "polygon": False, "circle": False,
            "marker":   False, "circlemarker": False,
            "rectangle": {"shapeOptions": {
                "color": "#00d4ff", "weight": 2, "opacity": 0.9,
                "fillColor": "#00d4ff", "fillOpacity": 0.08,
            }},
        },
        edit_options={"edit": False, "remove": True},
    ).add_to(m)

    for t in _grid:
        folium.Rectangle(
            bounds=[[t["lat_min"], t["lon_min"]], [t["lat_max"], t["lon_max"]]],
            color="#ff9f1c", weight=1, fill=False,
            tooltip=f"tile_{t['col']}_{t['row']}",
        ).add_to(m)

    map_out = st_folium(
        m,
        use_container_width=True,
        height=720,
        returned_objects=["all_drawings"],   # zoom/center exclus → 0 rerun sur navigation
        key="main_map",
    )

    # Nouvelle BBox dessinée → rerun complet pour mettre à jour grille + tabs
    drawings = (map_out or {}).get("all_drawings") or []
    if drawings:
        geom = drawings[-1].get("geometry", {})
        if geom.get("type") == "Polygon":
            coords   = geom["coordinates"][0]
            lons     = [c[0] for c in coords]
            lats     = [c[1] for c in coords]
            new_bbox = {
                "lon_min": round(min(lons), 6), "lat_min": round(min(lats), 6),
                "lon_max": round(max(lons), 6), "lat_max": round(max(lats), 6),
            }
            if new_bbox != st.session_state.get("bbox"):
                st.session_state["bbox"]        = new_bbox
                # Centre la carte sur la bbox dessinée au prochain rendu
                st.session_state["map_center"]  = [
                    (new_bbox["lat_min"] + new_bbox["lat_max"]) / 2,
                    (new_bbox["lon_min"] + new_bbox["lon_max"]) / 2,
                ]
                st.session_state["geojson_aoi"] = {
                    "type": "Polygon",
                    "coordinates": [[
                        [new_bbox["lon_min"], new_bbox["lat_min"]],
                        [new_bbox["lon_max"], new_bbox["lat_min"]],
                        [new_bbox["lon_max"], new_bbox["lat_max"]],
                        [new_bbox["lon_min"], new_bbox["lat_max"]],
                        [new_bbox["lon_min"], new_bbox["lat_min"]],
                    ]],
                }
                for k in ("catalog_results", "search_version",
                          "last_tiles", "last_tiles_meta"):
                    st.session_state.pop(k, None)
                st.rerun()


_render_map(tile_label)

# ─────────────────────────────────────────────
# GRILLE COURANTE (lue après le fragment)
# ─────────────────────────────────────────────
current_bbox = st.session_state.get("bbox")
grid_tiles   = generate_grid(current_bbox) if current_bbox else []


# ─────────────────────────────────────────────
# SIDEBAR — Part 2 : GPS + stats grille
# ─────────────────────────────────────────────
with st.sidebar:
    st.divider()
    st.markdown("**Zone GPS**")
    if current_bbox:
        c1, c2 = st.columns(2)
        c1.metric("Lat N", f"{current_bbox['lat_max']:.4f}°")
        c1.metric("Lat S", f"{current_bbox['lat_min']:.4f}°")
        c2.metric("Lon O", f"{current_bbox['lon_min']:.4f}°")
        c2.metric("Lon E", f"{current_bbox['lon_max']:.4f}°")
        d_lat   = abs(current_bbox["lat_max"] - current_bbox["lat_min"])
        d_lon   = abs(current_bbox["lon_max"] - current_bbox["lon_min"])
        lat_mid = (current_bbox["lat_max"] + current_bbox["lat_min"]) / 2
        area    = d_lat * 111.32 * d_lon * 111.32 * math.cos(math.radians(lat_mid))
        st.metric("Surface totale", f"{area:,.0f} km²")
        if grid_tiles:
            n_rows_g = max(t["row"] for t in grid_tiles) + 1
            n_cols_g = max(t["col"] for t in grid_tiles) + 1
            st.metric("Grille", f"{n_rows_g} × {n_cols_g} = {len(grid_tiles)} tuiles")
    else:
        st.caption("Dessinez un rectangle sur la carte.")


# ─────────────────────────────────────────────
# STOP si pas encore de zone dessinée
# ─────────────────────────────────────────────
if not current_bbox:
    st.stop()

bbox               = current_bbox
geojson_aoi        = st.session_state.get("geojson_aoi", {})
sentinel_bbox_list = [bbox["lon_min"], bbox["lat_min"],
                      bbox["lon_max"], bbox["lat_max"]]


# ─────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────
tab_s2, tab_bbox, tab_geojson, tab_code = st.tabs([
    "📡 Sentinel-2", "Sentinel Hub BBox", "GeoJSON AOI", "Code d'intégration",
])

# ════════════════════════════════════════════════════════════════════════════════
# TAB — Sentinel-2
# ════════════════════════════════════════════════════════════════════════════════
with tab_s2:
    if not SH_AVAILABLE:
        st.error("Package `sentinelhub` absent : `pip install sentinelhub`")
        st.stop()

    creds_ok = bool(client_id and client_secret)
    if not creds_ok:
        st.warning("Entrez vos credentials Copernicus dans la sidebar (section 🔑).")

    # Résumé grille
    if grid_tiles:
        n_rows_g = max(t["row"] for t in grid_tiles) + 1
        n_cols_g = max(t["col"] for t in grid_tiles) + 1
        st.info(
            f"Zone découpée en **{len(grid_tiles)} tuile(s)** ({n_rows_g} × {n_cols_g}) "
            f"de {TILE_MAX_KM:.0f} km max chacune — grille orange visible sur la carte."
        )

    # ── ÉTAPE 1 — Catalog search ───────────────────────────────────────────────
    st.markdown(
        '<div class="step-header">Étape 1 — Rechercher les passages satellites</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Période : **{date_start}** → **{date_end}** · Nuages ≤ **{max_cloud}%**"
    )

    if st.button("🔍 Chercher les passages satellites",
                 use_container_width=True, disabled=not creds_ok,
                 help="Interroge le catalogue STAC — aucun pixel téléchargé."):
        aoi_sh = BBox(bbox=sentinel_bbox_list, crs=CRS.WGS84)
        try:
            cfg = build_sh_config(client_id, client_secret)
            with st.spinner("Interrogation du catalogue Copernicus…"):
                results = search_catalog(
                    cfg, aoi_sh,
                    (str(date_start), str(date_end)),
                    max_cloud,
                )
            st.session_state["catalog_results"] = results
            st.session_state["search_version"]  = (
                st.session_state.get("search_version", 0) + 1
            )
            st.session_state.pop("last_tiles",      None)
            st.session_state.pop("last_tiles_meta", None)
        except Exception as e:
            st.error(f"Erreur catalogue : {e}")
            with st.expander("Détail"):
                st.exception(e)

    # ── ÉTAPE 2 — Sélection ───────────────────────────────────────────────────
    catalog_results = st.session_state.get("catalog_results")

    if catalog_results is not None:
        st.divider()
        st.markdown(
            '<div class="step-header">Étape 2 — Sélectionner un passage</div>',
            unsafe_allow_html=True,
        )

        if not catalog_results:
            st.warning(
                f"Aucune acquisition ≤ {max_cloud}% de nuages "
                f"entre {date_start} et {date_end}.  \n"
                "→ Élargissez la plage ou augmentez le seuil."
            )
        else:
            st.success(
                f"**{len(catalog_results)} passage(s)** — triés par couverture nuageuse."
            )

            df_catalog = pd.DataFrame([{
                "Date":         r["date"],
                "Heure UTC":    r["time"][:5],
                "Nuages (%)":   r["cloud_cover"],
                "ID (extrait)": r["id"][-30:] if len(r["id"]) > 30 else r["id"],
            } for r in catalog_results])
            st.dataframe(
                df_catalog,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Nuages (%)": st.column_config.ProgressColumn(
                        "Nuages (%)", min_value=0, max_value=100, format="%.1f%%"
                    ),
                },
            )

            version      = st.session_state.get("search_version", 0)
            selected_idx = st.selectbox(
                "Sélectionner un passage :",
                range(len(catalog_results)),
                format_func=lambda i: catalog_results[i]["label"],
                key=f"acq_select_{version}",
            )
            selected_acq = catalog_results[selected_idx]

            c1, c2, c3 = st.columns(3)
            c1.metric("Date",      selected_acq["date"])
            c2.metric("Heure UTC", selected_acq["time"][:5])
            icon = ("🟢" if selected_acq["cloud_cover"] < 10
                    else "🟡" if selected_acq["cloud_cover"] < 40 else "🔴")
            c3.metric("Nuages", f"{icon} {selected_acq['cloud_cover']}%")

            # ── ÉTAPE 3 — Batch download ──────────────────────────────────────
            st.divider()
            st.markdown(
                '<div class="step-header">Étape 3 — Balayage de la zone</div>',
                unsafe_allow_html=True,
            )
            st.caption(
                f"**{len(grid_tiles)} tuile(s)** · Résolution 10 m/pixel · "
                f"Stockage : `downloads/{zone_name}/{selected_acq['date']}/tile_X_Y.npy`"
            )

            if st.button(
                f"⬇️ Lancer le balayage de la zone ({len(grid_tiles)} tuile(s) — {selected_acq['date']})",
                use_container_width=True,
                type="primary",
            ):
                output_dir = Path("./downloads") / zone_name / selected_acq["date"]
                output_dir.mkdir(parents=True, exist_ok=True)

                # Metadata consumed by mosaic.py for georeferencing
                (output_dir / "metadata.json").write_text(json.dumps({
                    "zone":        zone_name,
                    "date":        selected_acq["date"],
                    "datetime":    selected_acq["datetime"],
                    "cloud_cover": selected_acq["cloud_cover"],
                    "bbox":        bbox,
                    "resolution_m": 10,
                    "grid": {
                        "n_cols": max(t["col"] for t in grid_tiles) + 1,
                        "n_rows": max(t["row"] for t in grid_tiles) + 1,
                        "tiles":  [
                            {"col": t["col"], "row": t["row"],
                             "lon_min": t["lon_min"], "lat_min": t["lat_min"],
                             "lon_max": t["lon_max"], "lat_max": t["lat_max"]}
                            for t in grid_tiles
                        ],
                    },
                }, indent=2))

                cfg          = build_sh_config(client_id, client_secret)
                tiles_result = {}
                errors       = []
                n_tiles      = len(grid_tiles)

                progress_bar = st.progress(0.0, text="Démarrage du balayage…")
                status_text  = st.empty()

                for i, tile in enumerate(grid_tiles):
                    r, c = tile["row"], tile["col"]
                    key  = f"{c}_{r}"   # X_Y : X=col, Y=row
                    status_text.markdown(
                        f"📥 Tuile **tile_{c}_{r}** "
                        f"({i + 1}/{n_tiles}) — "
                        f"[{tile['lat_min']:.3f}°, {tile['lon_min']:.3f}°]"
                    )
                    try:
                        img_float, npy_path = download_tile(
                            cfg, tile, selected_acq, output_dir
                        )
                        if img_float is not None:
                            stem = f"tile_{c}_{r}"
                            tiles_result[key] = {
                                "npy": str(npy_path.resolve()),
                                "png": str((output_dir / f"{stem}.png").resolve()),
                                "row": r, "col": c,
                            }
                        else:
                            errors.append(key)
                    except Exception as e:
                        errors.append(key)
                        st.warning(f"Tuile {key} échouée : {e}")

                    progress_bar.progress(
                        (i + 1) / n_tiles,
                        text=f"Tuile {i + 1}/{n_tiles} terminée",
                    )
                    time.sleep(0.3)   # politesse envers l'API CDSE

                progress_bar.empty()
                status_text.empty()

                if tiles_result:
                    st.session_state["last_tiles"]      = tiles_result
                    st.session_state["last_tiles_meta"] = selected_acq
                    ok_msg = (
                        f"✅ **{len(tiles_result)}/{n_tiles}** tuile(s) téléchargée(s) "
                        f"dans `downloads/{zone_name}/{selected_acq['date']}/`"
                    )
                    if errors:
                        ok_msg += f"  \n⚠️ Tuile(s) échouée(s) : `{', '.join(errors)}`"
                    st.success(ok_msg)
                else:
                    st.error(
                        "Aucune tuile téléchargée. "
                        "Vérifiez vos credentials et réessayez."
                    )

            # ══════════════════════════════════════════════════════════════════
            # INSPECTEUR + GALERIE (visible dès qu'il y a des tuiles)
            # ══════════════════════════════════════════════════════════════════
            if "last_tiles" in st.session_state:
                tiles_data  = st.session_state["last_tiles"]
                meta        = st.session_state.get("last_tiles_meta", {})
                # Key format: "{col}_{row}" — sort by (row, col) for display
                keys_sorted = sorted(
                    tiles_data.keys(),
                    key=lambda k: (int(k.split("_")[1]), int(k.split("_")[0])),
                )

                st.divider()

                # ── Inspecteur individuel ──────────────────────────────────────
                st.markdown("**🔍 Inspecteur de tuiles**")

                selected_tile_key = st.selectbox(
                    "Sélectionner une tuile :",
                    keys_sorted,
                    format_func=lambda k: (
                        f"tile_{k}  "
                        f"(col {k.split('_')[0]}, ligne {k.split('_')[1]})"
                    ),
                    key="tile_inspector_select",
                )

                npy_path = Path(tiles_data[selected_tile_key]["npy"])

                if npy_path.exists():
                    img_float = np.load(str(npy_path))

                    cg, cgm = st.columns(2)
                    gain = cg.slider(
                        "Gain (Luminosité)", 1.0, 5.0, 2.0, step=0.1, key="gain",
                        help="Amplifie la luminosité globale.",
                    )
                    gamma = cgm.slider(
                        "Correction Gamma", 0.5, 3.0, 1.5, step=0.1, key="gamma",
                        help="γ > 1 : débouche gris/ombres sans cramer les blancs.",
                    )

                    img_display = apply_tone(img_float, gain, gamma)

                    if img_display.max() < 5:
                        st.warning(
                            f"Tuile quasi-noire (max = {img_display.max()}). "
                            "Zone sans couverture S2 ou données absentes."
                        )

                    st.image(
                        img_display,
                        use_container_width=True,
                        caption=(
                            f"tile_{selected_tile_key} · "
                            f"{meta.get('date', '?')} {meta.get('time', '?')[:5]} UTC · "
                            f"☁ {meta.get('cloud_cover', '?')}%"
                        ),
                    )

                    if PIL_AVAILABLE:
                        fname = (
                            f"{zone_name}_{meta.get('date', '?')}_"
                            f"tile_{selected_tile_key}_"
                            f"g{gain:.1f}_y{gamma:.1f}.png"
                        )
                        st.download_button(
                            label=f"💾 Télécharger tile_{selected_tile_key} (paramètres actuels)",
                            data=array_to_png_bytes(img_display),
                            file_name=fname,
                            mime="image/png",
                            use_container_width=True,
                        )
                else:
                    st.error(f"Fichier .npy introuvable : `{npy_path}`")

                # ── Galerie globale ────────────────────────────────────────────
                with st.expander(
                    f"🗺️ Afficher la galerie de la zone ({len(keys_sorted)} tuiles)"
                ):
                    st.caption(
                        "Vue d'ensemble nord en haut. "
                        "Repérez une tuile, sélectionnez-la dans l'Inspecteur ci-dessus "
                        "pour ajuster Gain/Gamma."
                    )

                    # Key format: "{col}_{row}" — group by row (index 1), north on top
                    rows_dict: dict[int, list] = {}
                    for key in keys_sorted:
                        r_idx = int(key.split("_")[1])
                        rows_dict.setdefault(r_idx, []).append(key)

                    for r_idx in sorted(rows_dict.keys(), reverse=True):
                        row_keys = sorted(
                            rows_dict[r_idx],
                            key=lambda k: int(k.split("_")[0]),   # sort by col asc
                        )
                        gallery_cols = st.columns(len(row_keys))
                        for c_idx, key in enumerate(row_keys):
                            tile_info = tiles_data[key]
                            png_path  = Path(tile_info["png"])
                            if png_path.exists():
                                gallery_cols[c_idx].image(
                                    str(png_path),
                                    caption=f"tile_{key}",
                                    use_container_width=True,
                                )
                            elif Path(tile_info["npy"]).exists():
                                img_fb = np.load(str(tile_info["npy"]))
                                gallery_cols[c_idx].image(
                                    apply_tone(img_fb, 2.0, 1.5),
                                    caption=f"tile_{key}",
                                    use_container_width=True,
                                )
                            else:
                                gallery_cols[c_idx].warning(
                                    f"tile_{key}\nmanquante"
                                )


# ════════════════════════════════════════════════════════════════════════════════
# TABs export
# ════════════════════════════════════════════════════════════════════════════════
with tab_bbox:
    st.markdown("BBox globale :")
    st.code(f"bbox_coords = {sentinel_bbox_list}", language="python")
    if grid_tiles:
        st.markdown(f"**{len(grid_tiles)} sous-BBoxes** (≤ {TILE_MAX_KM:.0f} km) :")
        preview = [
            {"tile": f"{t['row']}_{t['col']}",
             "bbox": [t["lon_min"], t["lat_min"], t["lon_max"], t["lat_max"]]}
            for t in grid_tiles[:6]
        ]
        st.json(preview)
        if len(grid_tiles) > 6:
            st.caption(f"… et {len(grid_tiles) - 6} autre(s).")

with tab_geojson:
    st.markdown("GeoJSON Polygon (zone globale) :")
    st.json(geojson_aoi)
    st.download_button(
        label="Télécharger AOI.geojson",
        data=json.dumps(geojson_aoi, indent=2),
        file_name="aoi.geojson",
        mime="application/json",
    )

with tab_code:
    st.markdown("Snippet tiling + batch download :")
    st.code(f"""\
import numpy as np, math, time
from pathlib import Path
from sentinelhub import (
    SHConfig, BBox, CRS, DataCollection,
    SentinelHubRequest, MimeType, bbox_to_dimensions,
)

config = SHConfig()
config.sh_client_id     = "VOTRE_CLIENT_ID"
config.sh_client_secret = "VOTRE_CLIENT_SECRET"
config.sh_base_url      = "{CDSE_BASE_URL}"
config.sh_token_url     = "{CDSE_TOKEN_URL}"

# ── Tiling ────────────────────────────────────────────────────────────────────
bbox     = {sentinel_bbox_list}
max_km   = {TILE_MAX_KM}
lat_mid  = (bbox[1] + bbox[3]) / 2
lat_step = max_km / 111.32
lon_step = max_km / (111.32 * math.cos(math.radians(lat_mid)))

tiles, lat, row = [], bbox[1], 0
while lat < bbox[3]:
    lon, col = bbox[0], 0
    while lon < bbox[2]:
        tiles.append(dict(row=row, col=col,
            lon_min=lon,                   lat_min=lat,
            lon_max=min(lon+lon_step,bbox[2]), lat_max=min(lat+lat_step,bbox[3])))
        lon += lon_step; col += 1
    lat += lat_step; row += 1

# ── Batch download ─────────────────────────────────────────────────────────────
collection = DataCollection.SENTINEL2_L2A.define_from(
    "cdse-sentinel-2-l2a", service_url=config.sh_base_url
)
out = Path("./downloads"); out.mkdir(exist_ok=True)

for t in tiles:
    aoi  = BBox([t["lon_min"],t["lat_min"],t["lon_max"],t["lat_max"]], crs=CRS.WGS84)
    size = bbox_to_dimensions(aoi, resolution=10)
    req  = SentinelHubRequest(
        evalscript=EVALSCRIPT_TRUE_COLOR,
        input_data=[SentinelHubRequest.input_data(
            data_collection=collection,
            time_interval=("{date_start}", "{date_end}"),
            maxcc=1.0, mosaicking_order="mostRecent",
        )],
        responses=[SentinelHubRequest.output_response("default", MimeType.TIFF)],
        bbox=aoi, size=size, config=config,
    )
    img = req.get_data()[0].astype(np.float32)   # [0.0, 1.0]
    np.save(out / f"tile_{{t['row']}}_{{t['col']}}.npy", img)
    time.sleep(0.3)

# ── Tone mapping ───────────────────────────────────────────────────────────────
gain, gamma = 2.0, 1.5
img_uint8   = (np.clip(img * gain, 0, 1) ** (1/gamma) * 255).astype(np.uint8)
""", language="python")
