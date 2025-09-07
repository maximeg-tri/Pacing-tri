# app.py
import streamlit as st
import gpxpy
import pandas as pd
import math
import io

# --------------------
# Utilitaires
# --------------------
rho = 1.225  # densité air (kg/m³)
g = 9.81

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon/2)**2)
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

def segmenter(points):
    segs = []
    for i in range(1, len(points)):
        lat1, lon1, ele1 = points[i-1]
        lat2, lon2, ele2 = points[i]
        d = haversine(lat1, lon1, lat2, lon2)
        d_ele = ele2 - ele1
        slope = d_ele / d if d > 0 else 0
        segs.append((d, slope))
    return segs

# --------------------
# Modèle vélo (physique simple)
# --------------------
def power_required(v, slope, m, CdA, Crr):
    theta = math.atan(slope)
    rolling = Crr * m * g * math.cos(theta) * v
    aero = 0.5 * rho * CdA * v**3
    gravity = m * g * math.sin(theta) * v
    return rolling + aero + gravity

def solve_velocity(P, slope, m, CdA, Crr):
    v_low, v_high = 0.1, 25.0
    for _ in range(50):
        v_mid = 0.5*(v_low+v_high)
        if power_required(v_mid, slope, m, CdA, Crr) > P:
            v_high = v_mid
        else:
            v_low = v_mid
    return v_mid

def calcul_pacing_velo(segs, P_cible, CP_bike, m, CdA, Crr):
    rows = []
    for d, slope in segs:
        # règle simple d'adaptation de puissance selon pente
        if slope > 0.05:
            P = min(P_cible * 1.12, CP_bike)   # montée raide : pic jusqu'à CP
        elif slope > 0.02:
            P = P_cible * 1.06
        elif slope < -0.03:
            P = max(P_cible * 0.60, 50)
        else:
            P = P_cible

        v = solve_velocity(P, slope, m, CdA, Crr)
        t = d / v if v > 0 else 0
        rows.append({
            "distance_m": d,
            "slope": slope,
            "power_W": round(P,1),
            "speed_kmh": round(v*3.6,2),
            "time_s": round(t,1)
        })

    df = pd.DataFrame(rows)
    df["cum_dist_km"] = df["distance_m"].cumsum()/1000
    df["cum_time_min"] = df["time_s"].cumsum()/60
    # approx IF & TSS
    total_time_h = df["time_s"].sum()/3600
    if total_time_h > 0:
        avg_power = (df["power_W"] * df["time_s"]).sum() / df["time_s"].sum()
        IF = avg_power / CP_bike if CP_bike > 0 else 0
        TSS = total_time_h * (IF**2) * 100
    else:
        avg_power = IF = TSS = 0
    stats = {"total_km": df["distance_m"].sum()/1000,
             "total_time_h": total_time_h,
             "avg_power": round(avg_power,1),
             "IF": round(IF,3),
             "TSS": round(TSS,1)}
    return df, stats

# --------------------
# Modèle course à pied (pace adjustment simple)
# --------------------
def pace_adjust_by_slope(base_pace_s_per_km, slope):
    # règles empiriques simples :
    # - uphill: ~ +12 s/km par 1% de pente
    # - downhill: ~ -6 s/km par 1% (amélioration), capée à 18 s/km max
    pct = slope * 100
    if pct > 0:
        add = 12.0 * pct
        return base_pace_s_per_km + add
    else:
        improvement = min(6.0 * (-pct), 18.0)
        return max(base_pace_s_per_km - improvement, base_pace_s_per_km * 0.85)

def calcul_pacing_run(segs, base_pace_min_per_km, fatigue_factor):
    base_pace_s = base_pace_min_per_km * 60
    rows = []
    for d, slope in segs:
        pace_s = pace_adjust_by_slope(base_pace_s * (1 + fatigue_factor), slope)
        v = 1000.0 / pace_s  # m/s
        t = d / v if v > 0 else 0
        rows.append({
            "distance_m": d,
            "slope": slope,
            "pace_min_per_km": round(pace_s/60,2),
            "speed_kmh": round(v*3.6,2),
            "time_s": round(t,1)
        })
    df = pd.DataFrame(rows)
    df["cum_dist_km"] = df["distance_m"].cumsum()/1000
    df["cum_time_min"] = df["time_s"].cumsum()/60
    stats = {"total_km": df["distance_m"].sum()/1000,
             "total_time_h": df["time_s"].sum()/3600,
             "avg_pace_min_per_km": round((df["time_s"].sum() / (df["distance_m"].sum()/1000))/60,2) if df["distance_m"].sum()>0 else None}
    return df, stats

# --------------------
# Lecture GPX
# --------------------
def lire_gpx_file(file):
    gpx = gpxpy.parse(file)
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            for p in segment.points:
                points.append((p.latitude, p.longitude, p.elevation if p.elevation is not None else 0.0))
    return points

# --------------------
# Interface Streamlit
# --------------------
st.set_page_config(page_title="Pacing Triathlon", layout="wide")
st.title("🏊‍♂️🚴‍♂️🏃‍♂️ Pacing Triathlon - vélo & CAP")

st.markdown("""
Importe un ou plusieurs fichiers GPX (si mode triathlon, upload d'abord le GPX vélo puis le GPX course).
Les paramètres par défaut sont adaptés à ton équipement (Canyon Speedmax, trifonction, casque aéro).
""")

uploaded = st.file_uploader("GPX (1 ou 2 fichiers) — vélo puis course (optionnel)", accept_multiple_files=True, type=["gpx"])

with st.sidebar:
    st.header("Paramètres (modifiables)")
    poids_total = st.number_input("Poids total (kg) - athlète + vélo + équipement", 60.0, 110.0, 78.0, 0.5)
    CdA = st.number_input("CdA (m²)", 0.18, 0.30, 0.235, 0.005)
    Crr = st.number_input("Crr", 0.002, 0.010, 0.004, 0.0001)
    st.markdown("---")
    CP_bike = st.number_input("FTP / CP vélo (W)", 150, 600, 310)
    default_P_cible = int(round(0.82 * CP_bike))
    P_cible = st.number_input("Puissance cible vélo (W)", 100, 500, default_P_cible)
    st.markdown("---")
    # course
    use_custom_pace = st.checkbox("Saisir allure cible course (min/km) au lieu de CP run (km/h) ?", value=False)
    if use_custom_pace:
        base_pace = st.number_input("Allure cible (min/km)", 3.0, 8.0, 4.02, 0.01)
        CP_run_kmh = None
    else:
        CP_run_kmh = st.number_input("CP run (km/h) ≈ vitesse SV2 (si non, coche l'option allure)", 10.0, 20.0, 14.9, 0.1)
        base_pace = None
    fatigue_factor = st.slider("Facteur fatigue après vélo (%) — pour triathlon (valeur approximative)", 0, 20, 5) / 100.0
    st.markdown("---")
    st.markdown("⚠️ Les formules sont simplifiées — ajuste les paramètres selon tes tests (FTP, poids, sensations).")

if uploaded is not None and len(uploaded) >= 1:
    # determine files
    bike_file = uploaded[0]
    run_file = uploaded[1] if len(uploaded) > 1 else None

    # Vélo
    points_bike = lire_gpx_file(bike_file)
    segs_bike = segmenter(points_bike)
    df_bike, stats_bike = calcul_pacing_velo(segs_bike, P_cible, CP_bike, poids_total, CdA, Crr)

    st.subheader("📉 Résultats - Vélo")
    st.write(pd.DataFrame([stats_bike]))
    st.dataframe(df_bike.head(200))
    st.line_chart(df_bike[["cum_dist_km","power_W"]].set_index("cum_dist_km"))
    st.line_chart(df_bike[["cum_dist_km","speed_kmh"]].set_index("cum_dist_km"))

    csv_bike = df_bike.to_csv(index=False).encode('utf-8')
    st.download_button("Télécharger pacing vélo (CSV)", csv_bike, "pacing_velo.csv", "text/csv")

    # Course (soit GPX de run uploadé, soit on réutilise tracé vélo)
    if run_file is None:
        points_run = points_bike
        st.info("Aucun GPX de course uploadé — j'utilise le même parcours pour la simulation course.")
    else:
        points_run = lire_gpx_file(run_file)

    segs_run = segmenter(points_run)

    # base pace determination
    if base_pace is None and CP_run_kmh is not None:
        base_pace = 60.0 / CP_run_kmh  # min/km

    # apply fatigue factor (only in triathlon sense if user wants)
    if st.button("Calculer pacing course (avec facteur fatigue)"):
        df_run, stats_run = calcul_pacing_run(segs_run, base_pace, fatigue_factor)
        st.subheader("🏃 Résultats - Course à pied")
        st.write(pd.DataFrame([stats_run]))
        st.dataframe(df_run.head(200))
        st.line_chart(df_run[["cum_dist_km","speed_kmh"]].set_index("cum_dist_km"))
        st.line_chart(df_run[["cum_dist_km","pace_min_per_km"]].set_index("cum_dist_km"))
        csv_run = df_run.to_csv(index=False).encode('utf-8')
        st.download_button("Télécharger pacing run (CSV)", csv_run, "pacing_run.csv", "text/csv")

    # triathlon résumé
    st.subheader("🔗 Résumé triathlon (approx.)")
    st.write(f"Vélo — distance {round(stats_bike['total_km'],2)} km, temps estimé {round(stats_bike['total_time_h']*60,1)} min, IF≈{stats_bike['IF']}, TSS≈{stats_bike['TSS']}")
    est_run_time_h = None
    if run_file is not None or st.button("Estimer course finale (rapide)"):
        # quick estimate using calcul_pacing_run without re-click
        df_run, stats_run = calcul_pacing_run(segs_run, base_pace, fatigue_factor)
        est_run_time_h = stats_run['total_time_h']
        st.write(f"Course — distance {round(stats_run['total_km'],2)} km, temps estimé {round(est_run_time_h*60,1)} min (avec facteur fatigue {int(fatigue_factor*100)}%)")

else:
    st.info("Upload un fichier GPX pour commencer (1 fichier pour vélo ou 2 fichiers : vélo puis course).")
