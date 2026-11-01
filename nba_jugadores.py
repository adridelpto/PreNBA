"""
Modelo 2: Elo de equipo + rendimiento de los jugadores de cada plantilla.

Uso:
    python nba_jugadores.py            # predicciones de los proximos 7 dias
    python nba_jugadores.py --dias 3

Que hace:
  1. Descarga de ESPN las estadisticas de todos los jugadores de las ultimas 5
     temporadas completas y de la actual.
  2. Calcula un rating por jugador: Game Score por 36 minutos (puntos, tiros,
     rebotes, asistencias, robos, tapones, perdidas y faltas), ponderando mas
     las temporadas recientes y acercando al "jugador de reemplazo" a quien ha
     jugado pocos minutos.
  3. Descarga las plantillas actuales y los lesionados ("Out" no juegan).
     Fuerza de plantilla = suma de rating x minutos esperados de cada jugador.
  4. Convierte la diferencia de fuerza entre plantillas en puntos Elo. La escala
     se calibra con la temporada 2025-26 (ratings de antes de esa temporada
     frente a los resultados reales de esa temporada).
  5. Mezcla esa estimacion con el Elo de ganar/perder de esta temporada (el del
     modelo 1). Al principio de temporada pesa mas la plantilla; segun se juegan
     partidos, pesa mas el Elo.
"""
import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone

import requests

import nba_elo as m1

TEMPORADA_ACTUAL = 2027        # ESPN nombra la temporada por el ano en que acaba: 2027 = 2026-27
STATS_URL = ("https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/statistics/"
             "byathlete?region=us&lang=en&contentorigin=espn&isqualified=false&page=1&limit=1000"
             "&sort=general.avgMinutes:desc&seasontype=2&season={}")
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{}/roster"
INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"

# Peso de cada temporada anterior (1 = la anterior, 2 = hace dos...)
PESOS_TEMPORADA = {1: 1.0, 2: 0.7, 3: 0.5, 4: 0.3, 5: 0.2}
PESO_ACTUAL = 1.2              # la temporada en curso, por minuto jugado
MIN_PRIOR = 400                # minutos "ficticios" de jugador de reemplazo (regresion a la media)
MIN_MAX = 38                   # minutos maximos esperados por jugador
PARTIDOS_MEZCLA = 25           # con 25 partidos jugados el Elo pesa 50% y la plantilla 50%
CALIB_TEMPORADA = 2026         # temporada usada para calibrar la escala (2025-26)
CALIB_INICIO, CALIB_FIN = "2025-10-20", "2026-04-13"


# ---------------------------------------------------------------- descargas

def _get_json(url, cache=None, refrescar=False):
    if cache and os.path.exists(cache) and not refrescar:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    data = r.json()
    if cache:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(data, f)
    return data


def stats_temporada(season):
    """{id: {...}} con totales de cada jugador en esa temporada regular."""
    actual = season == TEMPORADA_ACTUAL
    data = _get_json(STATS_URL.format(season),
                     None if actual else os.path.join(m1.CACHE_DIR, f"jugadores_{season}.json"))
    jugadores = {}
    for a in data["athletes"]:
        vals = {}
        for c in a["categories"]:
            nombres = next(cc["names"] for cc in data["categories"] if cc["name"] == c["name"])
            vals.update(dict(zip(nombres, c["values"])))
        jugadores[a["athlete"]["id"]] = {
            "nombre": a["athlete"]["displayName"],
            "equipo": a["athlete"].get("teamShortName", ""),
            "pj": vals.get("gamesPlayed", 0),
            "min": vals.get("minutes", 0),
            "min_pp": vals.get("avgMinutes", 0),
            "gmsc": game_score(vals),
        }
    return jugadores


def game_score(v):
    """Game Score de Hollinger sobre totales de temporada. ESPN no separa
    rebotes ofensivos/defensivos, asi que se usa 0.4 por rebote (media aprox.)."""
    return (v.get("points", 0) + 0.4 * v.get("fieldGoalsMade", 0)
            - 0.7 * v.get("fieldGoalsAttempted", 0)
            - 0.4 * (v.get("freeThrowsAttempted", 0) - v.get("freeThrowsMade", 0))
            + 0.4 * v.get("rebounds", 0) + v.get("steals", 0)
            + 0.7 * v.get("assists", 0) + 0.7 * v.get("blocks", 0)
            - 0.4 * v.get("fouls", 0) - v.get("turnovers", 0))


def plantillas_actuales():
    """({id_jugador: equipo}, {id_jugador: estado_lesion}, {id: nombre})"""
    equipos = _get_json(TEAMS_URL)["sports"][0]["leagues"][0]["teams"]
    plantilla, nombres = {}, {}
    for t in equipos:
        abr, tid = t["team"]["abbreviation"], t["team"]["id"]
        for a in _get_json(ROSTER_URL.format(tid)).get("athletes", []):
            plantilla[a["id"]] = abr
            nombres[a["id"]] = a["displayName"]
    lesiones = {}
    for eq in _get_json(INJURIES_URL).get("injuries", []):
        for i in eq.get("injuries", []):
            ath = i.get("athlete", {})
            pid = ath.get("id")
            if not pid:
                for l in ath.get("links", []):
                    if "/id/" in l.get("href", ""):
                        pid = l["href"].split("/id/")[1].split("/")[0]
                        break
            if pid:
                lesiones[str(pid)] = i.get("status", "")
    return plantilla, lesiones, nombres


# ---------------------------------------------------------------- ratings

def ratings_jugadores(stats, hasta, incluir_actual):
    """Rating (Game Score por 36 min) de cada jugador usando las 5 temporadas
    anteriores a `hasta` y, si se pide, la propia temporada `hasta`."""
    acum = {}
    temporadas = [(hasta - k, w) for k, w in PESOS_TEMPORADA.items()]
    if incluir_actual:
        temporadas.append((hasta, PESO_ACTUAL))
    for season, w in temporadas:
        for pid, j in stats.get(season, {}).items():
            if j["min"] <= 0:
                continue
            a = acum.setdefault(pid, [0.0, 0.0])
            a[0] += w * j["gmsc"] / j["min"] * 36 * j["min"]
            a[1] += w * j["min"]
    # Nivel de reemplazo: percentil 20 de los jugadores con minutos reales
    vals = sorted(a[0] / a[1] for a in acum.values() if a[1] > 1000)
    repl = vals[len(vals) // 5] if vals else 8.0
    ratings = {pid: (a[0] + MIN_PRIOR * repl) / (a[1] + MIN_PRIOR) for pid, a in acum.items()}
    return ratings, repl


def minutos_esperados(pid, stats, season, actual_ok):
    """Minutos por partido esperados: los de esta temporada si ya ha jugado
    3+ partidos; si no, los de la anterior; si es nuevo, 8."""
    if actual_ok:
        j = stats.get(season, {}).get(pid)
        if j and j["pj"] >= 3:
            return j["min_pp"]
    for k in (1, 2):
        j = stats.get(season - k, {}).get(pid)
        if j and j["pj"] >= 5:
            return j["min_pp"]
    return 8.0


def fuerza_plantillas(plantilla, ratings, repl, stats, season, actual_ok, lesiones=None):
    """Fuerza de cada equipo = suma(rating36 x minutos/36) con 240 minutos repartidos."""
    lesiones = lesiones or {}
    por_equipo = {}
    for pid, eq in plantilla.items():
        if lesiones.get(pid, "").lower() == "out":
            continue
        mins = min(minutos_esperados(pid, stats, season, actual_ok), MIN_MAX)
        por_equipo.setdefault(eq, []).append((pid, ratings.get(pid, repl), mins))
    fuerza, detalle = {}, {}
    for eq, js in por_equipo.items():
        js.sort(key=lambda x: -x[2])
        js = js[:13]
        total = sum(m for _, _, m in js) or 1
        escala = 240 / total
        fuerza[eq] = sum(r * min(m * escala, MIN_MAX) / 36 for _, r, m in js)
        detalle[eq] = js
    return fuerza, detalle


def calibrar(stats):
    """Puntos de margen por cada punto de diferencia de fuerza, ajustado con la
    temporada 2025-26: ratings previos a esa temporada vs. resultados reales."""
    ruta = os.path.join(m1.CACHE_DIR, f"calibracion_{CALIB_TEMPORADA}.json")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    ratings, repl = ratings_jugadores(stats, CALIB_TEMPORADA, incluir_actual=False)
    plantilla = {pid: j["equipo"] for pid, j in stats[CALIB_TEMPORADA].items() if j["equipo"]}
    fuerza, _ = fuerza_plantillas(plantilla, ratings, repl, stats, CALIB_TEMPORADA, actual_ok=True)
    print(f"Calibrando con la temporada 2025-26 (descarga unica de ~175 dias)...")
    partidos = [p for p in m1.descargar_espn(inicio=CALIB_INICIO, fin=CALIB_FIN) if p["estado"] == 3]
    xs, ys = [], []
    for p in partidos:
        if p["local"] in fuerza and p["visit"] in fuerza:
            xs.append(fuerza[p["local"]] - fuerza[p["visit"]])
            ys.append(p["pts_local"] - p["pts_visit"])
    my = sum(ys) / len(ys)                        # ventaja de campo media
    mx = sum(xs) / len(xs)
    pend = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    corr = (sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)))
    res = {"puntos_por_fuerza": round(pend, 4), "correlacion": round(corr, 3),
           "partidos": len(xs), "ventaja_campo_pts": round(my, 2)}
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(res, f)
    return res


# ---------------------------------------------------------------- modelo

def ejecutar(dias_pred=7):
    print("Descargando estadisticas de jugadores...")
    stats = {s: stats_temporada(s) for s in range(TEMPORADA_ACTUAL - 6, TEMPORADA_ACTUAL + 1)}
    calib = calibrar(stats)
    esc = calib["puntos_por_fuerza"] * m1.ELO_POR_PUNTO     # puntos Elo por punto de fuerza
    print(f"Calibracion: {calib['puntos_por_fuerza']} pts de margen por punto de fuerza "
          f"(correlacion {calib['correlacion']} en {calib['partidos']} partidos)")

    plantilla, lesiones, nombres = plantillas_actuales()

    # Fuerza "honesta" para evaluar los partidos ya jugados: solo temporadas anteriores
    r_pre, repl_pre = ratings_jugadores(stats, TEMPORADA_ACTUAL, incluir_actual=False)
    f_pre, _ = fuerza_plantillas(plantilla, r_pre, repl_pre, stats, TEMPORADA_ACTUAL, actual_ok=False)
    # Fuerza para predecir: incluye esta temporada y quita a los lesionados "Out"
    r_hoy, repl_hoy = ratings_jugadores(stats, TEMPORADA_ACTUAL, incluir_actual=True)
    f_hoy, detalle = fuerza_plantillas(plantilla, r_hoy, repl_hoy, stats, TEMPORADA_ACTUAL,
                                       actual_ok=True, lesiones=lesiones)
    media_pre = sum(f_pre.values()) / len(f_pre)
    media_hoy = sum(f_hoy.values()) / len(f_hoy)

    partidos = m1.descargar_espn(dias_pred)
    elo, jugados, ultimo = {}, {}, {}
    historial = []

    def b2b(eq, fecha):
        u = ultimo.get(eq)
        return u is not None and (fecha - u) < timedelta(hours=36)

    def prob(L, V, fecha, fuerza, media):
        el, ev = elo.get(L, m1.ELO_INICIAL), elo.get(V, m1.ELO_INICIAL)
        g = min(jugados.get(L, 0), jugados.get(V, 0))
        w = g / (g + PARTIDOS_MEZCLA)                       # peso del Elo de resultados
        elo_jug_L = m1.ELO_INICIAL + esc * (fuerza.get(L, media) - media)
        elo_jug_V = m1.ELO_INICIAL + esc * (fuerza.get(V, media) - media)
        rL = w * el + (1 - w) * elo_jug_L
        rV = w * ev + (1 - w) * elo_jug_V
        return m1.prob_local(rL, rV, b2b(L, fecha), b2b(V, fecha)) + (w,)

    for p in partidos:
        if p["estado"] != 3:
            continue
        L, V = p["local"], p["visit"]
        pl, diff, w = prob(L, V, p["fecha"], f_pre, media_pre)
        gana = p["pts_local"] > p["pts_visit"]
        historial.append({
            "fecha": p["fecha"].date().isoformat(), "local": L, "visitante": V,
            "prob_local": round(pl, 3), "pts_local": p["pts_local"],
            "pts_visit": p["pts_visit"], "acierto": int((pl > 0.5) == gana),
        })
        # El Elo de resultados se actualiza igual que en el modelo 1
        el, ev = elo.setdefault(L, m1.ELO_INICIAL), elo.setdefault(V, m1.ELO_INICIAL)
        pl1, d1 = m1.prob_local(el, ev, b2b(L, p["fecha"]), b2b(V, p["fecha"]))
        res = 1.0 if gana else 0.0
        delta = m1.K * m1.mult_margen(p["pts_local"] - p["pts_visit"], d1 if gana else -d1) * (res - pl1)
        elo[L], elo[V] = el + delta, ev - delta
        jugados[L] = jugados.get(L, 0) + 1
        jugados[V] = jugados.get(V, 0) + 1
        ultimo[L] = ultimo[V] = p["fecha"]

    n = len(historial)
    if n:
        acierto = sum(h["acierto"] for h in historial) / n
        brier = sum((h["prob_local"] - (h["pts_local"] > h["pts_visit"])) ** 2 for h in historial) / n
        print(f"\nPartidos evaluados: {n}   Precision: {acierto:.1%}   Brier: {brier:.3f}")

    ahora = datetime.now(timezone.utc)
    preds = []
    for p in partidos:
        if p["estado"] == 1 and p["fecha"] <= ahora + timedelta(days=dias_pred):
            L, V = p["local"], p["visit"]
            pl, diff, w = prob(L, V, p["fecha"], f_hoy, media_hoy)
            preds.append({"id": p["id"], "hora_utc": p["fecha"].strftime("%Y-%m-%dT%H:%MZ"),
                          "fecha": p["fecha"].date().isoformat(), "local": L, "visitante": V,
                          "prob_local": round(pl, 3), "spread_local": round(-diff / m1.ELO_POR_PUNTO, 1),
                          "peso_elo": round(w, 2)})
    print(f"\nPredicciones proximos {dias_pred} dias:")
    for pr in preds:
        fav = pr["local"] if pr["prob_local"] >= 0.5 else pr["visitante"]
        print(f"  {pr['fecha']}  {pr['visitante']} @ {pr['local']}  ->  {fav} "
              f"{max(pr['prob_local'], 1 - pr['prob_local']):.0%}")

    # Ranking combinado de equipos
    g_min = {eq: jugados.get(eq, 0) for eq in f_hoy}
    ranking = []
    for eq in f_hoy:
        g = g_min[eq]
        w = g / (g + PARTIDOS_MEZCLA)
        e = elo.get(eq, m1.ELO_INICIAL)
        ej = m1.ELO_INICIAL + esc * (f_hoy[eq] - media_hoy)
        top = max(detalle[eq], key=lambda x: x[1] * x[2])
        bajas = [nombres.get(pid, pid) for pid, est in lesiones.items()
                 if est.lower() == "out" and plantilla.get(pid) == eq]
        ranking.append({"equipo": eq, "elo": round(w * e + (1 - w) * ej, 1),
                        "elo_resultados": round(e, 1), "elo_plantilla": round(ej, 1),
                        "mejor_jugador": nombres.get(top[0], top[0]), "bajas": ", ".join(bajas)})
    ranking.sort(key=lambda r: -r["elo"])
    print("\nRanking combinado:")
    for i, r in enumerate(ranking, 1):
        print(f"  {i:2d}. {r['equipo']:4s} {r['elo']:7.1f}  (resultados {r['elo_resultados']:.0f}, "
              f"plantilla {r['elo_plantilla']:.0f})")

    jugadores = sorted(
        ({"jugador": nombres.get(pid, pid), "equipo": eq, "rating36": round(r_hoy.get(pid, repl_hoy), 1),
          "min_esperados": round(min(minutos_esperados(pid, stats, TEMPORADA_ACTUAL, True), MIN_MAX), 1),
          "lesion": lesiones.get(pid, "")} for pid, eq in plantilla.items()),
        key=lambda j: -j["rating36"] * j["min_esperados"])

    m1.guardar_csv("jug_historial_predicciones.csv", historial)
    m1.guardar_csv("jug_predicciones_proximas.csv", preds)
    m1.guardar_csv("jug_ratings.csv", ranking)
    m1.guardar_csv("jug_jugadores.csv", jugadores)
    m1.registrar_ejecucion(preds, "jug_evolucion_predicciones.csv")
    with open("jug_calibracion.json", "w", encoding="utf-8") as f:
        json.dump(calib, f)
    try:
        import dashboard
        dashboard.generar()
        print("\nDashboard actualizado: dashboard.html")
    except Exception as e:
        print(f"\nNo se pudo generar el dashboard: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=7, help="dias a predecir")
    ejecutar(ap.parse_args().dias)
