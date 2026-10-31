"""
Modelo Elo para predecir partidos NBA usando solo la temporada actual.

Uso:
    pip install requests
    python nba_elo.py            # descarga el calendario/resultados y predice
    python nba_elo.py --dias 3   # predicciones para los proximos 3 dias

Que hace:
  1. Descarga el calendario oficial de la temporada (cdn.nba.com), que incluye
     los resultados de los partidos ya jugados.
  2. Recorre los partidos terminados en orden. ANTES de actualizar, guarda la
     probabilidad que daba el modelo (asi la evaluacion es honesta, sin mirar
     el resultado). Despues actualiza el Elo de ambos equipos.
  3. Muestra el ranking, la precision acumulada y las predicciones de los
     proximos partidos. Guarda todo en CSV.
"""
import argparse
import csv
import json
import math
from datetime import datetime, timedelta, timezone

SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"

# Parametros del modelo (ajustables cuando haya mas datos)
ELO_INICIAL = 1500
K = 20                 # cuanto se mueve el rating tras cada partido
VENTAJA_CASA = 70      # puntos Elo de ventaja por jugar en casa (~2.5 pts de spread)
PENAL_B2B = 25         # penalizacion por jugar 2 noches seguidas (back-to-back)
ELO_POR_PUNTO = 28     # equivalencia aproximada Elo -> puntos de diferencia


def descargar_calendario(ruta_local=None):
    if ruta_local:
        with open(ruta_local, encoding="utf-8") as f:
            data = json.load(f)
    else:
        import requests
        r = requests.get(SCHEDULE_URL, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    partidos = []
    for dia in data["leagueSchedule"]["gameDates"]:
        for g in dia["games"]:
            # Solo temporada regular y playoffs (ids 002... y 004...); fuera pretemporada
            if not g["gameId"].startswith(("002", "004")):
                continue
            fecha = datetime.fromisoformat(g["gameDateTimeUTC"].replace("Z", "+00:00"))
            partidos.append({
                "id": g["gameId"],
                "fecha": fecha,
                "estado": g["gameStatus"],          # 1 programado, 2 en juego, 3 final
                "local": g["homeTeam"]["teamTricode"],
                "visit": g["awayTeam"]["teamTricode"],
                "pts_local": g["homeTeam"].get("score") or 0,
                "pts_visit": g["awayTeam"].get("score") or 0,
            })
    partidos.sort(key=lambda p: p["fecha"])
    return partidos


ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}"
CACHE_DIR = "cache_espn"


def descargar_espn(dias_pred=7, inicio="2026-10-01"):
    """Fuente alternativa: marcadores diarios publicos de ESPN.
    Guarda en cache_espn/ los dias ya terminados para no volver a pedirlos."""
    import os
    import requests
    os.makedirs(CACHE_DIR, exist_ok=True)
    hoy = datetime.now(timezone.utc).date()
    d = datetime.fromisoformat(inicio).date()
    fin = hoy + timedelta(days=dias_pred)
    partidos = []
    while d <= fin:
        clave = d.strftime("%Y%m%d")
        ruta = os.path.join(CACHE_DIR, clave + ".json")
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8") as f:
                data = json.load(f)
        else:
            r = requests.get(ESPN_URL.format(clave), timeout=30)
            r.raise_for_status()
            data = r.json()
            # Solo se cachean dias pasados con todos los partidos terminados
            if d < hoy - timedelta(days=1) and all(
                    e["status"]["type"]["state"] == "post" for e in data.get("events", [])):
                with open(ruta, "w", encoding="utf-8") as f:
                    json.dump(data, f)
        for e in data.get("events", []):
            if e["season"]["type"] not in (2, 3):   # 2 regular, 3 playoffs
                continue
            c = e["competitions"][0]
            eq = {t["homeAway"]: t for t in c["competitors"]}
            estado = {"pre": 1, "in": 2, "post": 3}[e["status"]["type"]["state"]]
            partidos.append({
                "id": e["id"],
                "fecha": datetime.fromisoformat(e["date"].replace("Z", "+00:00")),
                "estado": estado,
                "local": eq["home"]["team"]["abbreviation"],
                "visit": eq["away"]["team"]["abbreviation"],
                "pts_local": int(eq["home"].get("score") or 0),
                "pts_visit": int(eq["away"].get("score") or 0),
            })
        d += timedelta(days=1)
    vistos = set()
    partidos = [p for p in partidos if not (p["id"] in vistos or vistos.add(p["id"]))]
    partidos.sort(key=lambda p: p["fecha"])
    return partidos


def prob_local(elo_l, elo_v, b2b_l=False, b2b_v=False):
    diff = elo_l + VENTAJA_CASA - elo_v
    diff -= PENAL_B2B if b2b_l else 0
    diff += PENAL_B2B if b2b_v else 0
    return 1 / (1 + 10 ** (-diff / 400)), diff


def mult_margen(margen, diff_elo_ganador):
    # Multiplicador tipo FiveThirtyEight: ganar por mucho mueve mas el rating,
    # pero se corrige si el favorito gana (para evitar inflar ratings).
    return ((abs(margen) + 3) ** 0.8) / (7.5 + 0.006 * diff_elo_ganador)


def ejecutar(partidos, dias_pred=2):
    elo = {}
    ultimo_partido = {}
    historial = []
    evolucion_elo = []

    def es_b2b(eq, fecha):
        ult = ultimo_partido.get(eq)
        return ult is not None and (fecha - ult) < timedelta(hours=36)

    for p in partidos:
        if p["estado"] != 3:
            continue
        L, V = p["local"], p["visit"]
        el, ev = elo.setdefault(L, ELO_INICIAL), elo.setdefault(V, ELO_INICIAL)
        b2b_l, b2b_v = es_b2b(L, p["fecha"]), es_b2b(V, p["fecha"])
        pl, diff = prob_local(el, ev, b2b_l, b2b_v)

        gana_local = p["pts_local"] > p["pts_visit"]
        margen = p["pts_local"] - p["pts_visit"]
        res = 1.0 if gana_local else 0.0
        diff_ganador = diff if gana_local else -diff
        delta = K * mult_margen(margen, diff_ganador) * (res - pl)
        elo[L] = el + delta
        elo[V] = ev - delta
        ultimo_partido[L] = ultimo_partido[V] = p["fecha"]
        for eq_ in (L, V):
            evolucion_elo.append({"fecha": p["fecha"].date().isoformat(),
                                  "equipo": eq_, "elo": round(elo[eq_], 1)})

        historial.append({
            "fecha": p["fecha"].date().isoformat(), "local": L, "visitante": V,
            "prob_local": round(pl, 3), "pts_local": p["pts_local"],
            "pts_visit": p["pts_visit"], "acierto": int((pl > 0.5) == gana_local),
        })

    # Metricas de evaluacion
    n = len(historial)
    if n:
        acierto = sum(h["acierto"] for h in historial) / n
        brier = sum((h["prob_local"] - (h["pts_local"] > h["pts_visit"])) ** 2 for h in historial) / n
        logloss = -sum(math.log(h["prob_local"] if h["pts_local"] > h["pts_visit"] else 1 - h["prob_local"])
                       for h in historial) / n
        print(f"\nPartidos evaluados: {n}")
        print(f"Precision: {acierto:.1%}   Brier: {brier:.3f}   LogLoss: {logloss:.3f}")
        print("(referencia: elegir siempre al local acierta ~55-58%; un buen modelo NBA ~65-68%)")
        # Precision por bloques de 50 partidos para ver como mejora
        for i in range(0, n, 50):
            bloque = historial[i:i + 50]
            print(f"  partidos {i+1:4d}-{i+len(bloque):4d}: {sum(h['acierto'] for h in bloque)/len(bloque):.1%}")

    print("\nRanking Elo:")
    for i, (eq, r) in enumerate(sorted(elo.items(), key=lambda x: -x[1]), 1):
        print(f"  {i:2d}. {eq}  {r:7.1f}")

    # Predicciones de proximos partidos
    ahora = datetime.now(timezone.utc)
    limite = ahora + timedelta(days=dias_pred)
    preds = []
    for p in partidos:
        if p["estado"] == 1 and p["fecha"] <= limite:
            L, V = p["local"], p["visit"]
            pl, diff = prob_local(elo.get(L, ELO_INICIAL), elo.get(V, ELO_INICIAL),
                                  es_b2b(L, p["fecha"]), es_b2b(V, p["fecha"]))
            preds.append({"id": p["id"], "hora_utc": p["fecha"].strftime("%Y-%m-%dT%H:%MZ"),
                          "fecha": p["fecha"].date().isoformat(), "local": L, "visitante": V,
                          "prob_local": round(pl, 3), "spread_local": round(-diff / ELO_POR_PUNTO, 1)})
    print(f"\nPredicciones proximos {dias_pred} dias:")
    for pr in preds:
        fav = pr["local"] if pr["prob_local"] >= 0.5 else pr["visitante"]
        prob = max(pr["prob_local"], 1 - pr["prob_local"])
        print(f"  {pr['fecha']}  {pr['visitante']} @ {pr['local']}  ->  {fav} {prob:.0%}  (spread local {pr['spread_local']:+})")

    guardar_csv("historial_predicciones.csv", historial)
    guardar_csv("predicciones_proximas.csv", preds)
    guardar_csv("ratings.csv", [{"equipo": e, "elo": round(r, 1)} for e, r in
                                sorted(elo.items(), key=lambda x: -x[1])])
    guardar_csv("evolucion_elo.csv", evolucion_elo)
    registrar_ejecucion(preds)
    try:
        import dashboard
        dashboard.generar()
        print("\nDashboard actualizado: dashboard.html")
    except Exception as e:  # el dashboard nunca debe romper la prediccion
        print(f"\nNo se pudo generar el dashboard: {e}")
    return elo, historial, preds


def registrar_ejecucion(preds):
    """Anade las predicciones de esta ejecucion a evolucion_predicciones.csv,
    para ver como cambian los porcentajes de un partido entre ejecuciones."""
    import os
    nombre = "evolucion_predicciones.csv"
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    filas = [{"ejecucion_utc": ahora, **p} for p in preds]
    if not filas:
        return
    nuevo = not os.path.exists(nombre)
    with open(nombre, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=filas[0].keys())
        if nuevo:
            w.writeheader()
        w.writerows(filas)


def guardar_csv(nombre, filas):
    if not filas:
        return
    with open(nombre, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=filas[0].keys())
        w.writeheader()
        w.writerows(filas)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=7, help="dias a predecir")
    ap.add_argument("--json", help="usar un calendario guardado en local en vez de descargarlo")
    ap.add_argument("--fuente", choices=["espn", "nba"], default="espn",
                    help="espn (por defecto) o nba (cdn.nba.com, puede dar 403)")
    a = ap.parse_args()
    if a.json or a.fuente == "nba":
        partidos = descargar_calendario(a.json)
    else:
        partidos = descargar_espn(a.dias)
    ejecutar(partidos, a.dias)
