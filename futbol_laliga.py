"""
Modelo LaLiga: probabilidad de victoria local / empate / victoria visitante.

Uso:
    python futbol_laliga.py            # proximos 10 dias
    python futbol_laliga.py --dias 14

Como funciona (modelo de Poisson tipo Dixon-Coles):
  1. Descarga de ESPN todos los resultados de LaLiga desde 2022-23.
  2. Cada equipo tiene un rating de ATAQUE (cuanto marca) y de DEFENSA (cuanto
     encaja), mas una ventaja de jugar en casa. Se ajustan a los goles reales,
     dando mas peso a los partidos recientes (los de hace 6 meses pesan la mitad).
  3. Los recien ascendidos empiezan con un rating de "equipo ascendido" hasta
     que acumulan partidos en Primera.
  4. Goles esperados de cada equipo -> distribucion de Poisson -> probabilidad de
     cada marcador -> 1 / X / 2. Se corrige un poco para reflejar que en futbol
     hay algo mas de empates 0-0 y 1-1 de lo que da Poisson.
  5. Para evaluar, cada jornada de esta temporada se predice usando solo los
     partidos jugados antes.
"""
import argparse
import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone

import requests

LIGA = "esp.1"
URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/" + LIGA + "/scoreboard?dates={}"
CACHE = os.path.join("cache_espn", "laliga")
TEMPORADAS = [2022, 2023, 2024, 2025, 2026]      # ano de inicio (2026 = 2026-27, la actual)
ACTUAL = 2026
FECHAS_REF = {2022: "20221016", 2023: "20231015", 2024: "20241020", 2025: "20251019", 2026: "20261018"}

VIDA_MEDIA_DIAS = 180     # un partido de hace 180 dias pesa la mitad
PRIOR_PARTIDOS = 6        # fuerza del prior (en partidos)
PRIOR_ASCENDIDO = (-0.20, 0.20)   # ataque/defensa (escala log) de un recien ascendido
RHO = -0.08               # correccion Dixon-Coles para marcadores bajos
MAX_GOLES = 10


# ------------------------------------------------------------ datos

def _get(fecha, permanente):
    ruta = os.path.join(CACHE, fecha + ".json")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    d = requests.get(URL.format(fecha), timeout=30).json()
    if permanente:
        os.makedirs(CACHE, exist_ok=True)
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(d, f)
    return d


def descargar():
    hoy = datetime.now(timezone.utc).date()
    partidos = {}
    for t in TEMPORADAS:
        ref = _get(FECHAS_REF[t], t != ACTUAL and True)
        calendario = ref["leagues"][0].get("calendar", [])
        for c in calendario:
            dia = datetime.fromisoformat(c.replace("Z", "+00:00")).date()
            clave = dia.strftime("%Y%m%d")
            data = _get(clave, dia < hoy - timedelta(days=2))
            for e in data.get("events", []):
                comp = e["competitions"][0]
                eq = {x["homeAway"]: x for x in comp["competitors"]}
                estado = e["status"]["type"]["state"]
                if e["status"]["type"].get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
                    continue
                partidos[e["id"]] = {
                    "id": e["id"], "temporada": t,
                    "fecha": datetime.fromisoformat(e["date"].replace("Z", "+00:00")),
                    "estado": estado,
                    "local": eq["home"]["team"]["displayName"],
                    "visit": eq["away"]["team"]["displayName"],
                    "gl": int(eq["home"].get("score") or 0),
                    "gv": int(eq["away"].get("score") or 0),
                }
    return sorted(partidos.values(), key=lambda p: p["fecha"])


# ------------------------------------------------------------ modelo

def ajustar(jugados, hasta, equipos_temporada, equipos_previa):
    """Ajusta ataque/defensa con los partidos anteriores a `hasta`."""
    datos = []
    for p in jugados:
        if p["fecha"] >= hasta:
            break
        dias = (hasta - p["fecha"]).total_seconds() / 86400
        datos.append((p["local"], p["visit"], p["gl"], p["gv"], 0.5 ** (dias / VIDA_MEDIA_DIAS)))
    equipos = set(equipos_temporada) | {d[0] for d in datos} | {d[1] for d in datos}
    prior = {e: ((0.0, 0.0) if e in equipos_previa else PRIOR_ASCENDIDO) for e in equipos}
    at = {e: prior[e][0] for e in equipos}
    de = {e: prior[e][1] for e in equipos}
    wt = sum(d[4] for d in datos) or 1
    mu = math.log(max(sum(d[4] * (d[2] + d[3]) for d in datos) / (2 * wt), 0.5))
    h = 0.2
    for _ in range(60):
        # ataque
        g = {e: 0.0 for e in equipos}; x = {e: 0.0 for e in equipos}
        for l, v, gl, gv, w in datos:
            g[l] += w * gl; x[l] += w * math.exp(mu + h + de[v])
            g[v] += w * gv; x[v] += w * math.exp(mu + de[l])
        for e in equipos:
            k = PRIOR_PARTIDOS
            at[e] = math.log((g[e] + k * math.exp(mu + prior[e][0])) / (x[e] + k * math.exp(mu)))
        # defensa
        g = {e: 0.0 for e in equipos}; x = {e: 0.0 for e in equipos}
        for l, v, gl, gv, w in datos:
            g[v] += w * gl; x[v] += w * math.exp(mu + h + at[l])
            g[l] += w * gv; x[l] += w * math.exp(mu + at[v])
        for e in equipos:
            k = PRIOR_PARTIDOS
            de[e] = math.log((g[e] + k * math.exp(mu + prior[e][1])) / (x[e] + k * math.exp(mu)))
        # centrar ataques en 0 para que mu sea la media
        m = sum(at.values()) / len(at)
        at = {e: a - m for e, a in at.items()}
        mu += m
        # ventaja de campo
        gh = sum(w * gl for l, v, gl, gv, w in datos)
        xh = sum(w * math.exp(mu + at[l] + de[v]) for l, v, gl, gv, w in datos)
        if gh and xh:
            h = math.log(gh / xh)
    return {"mu": mu, "h": h, "at": at, "de": de}


def probabilidades(m, local, visit):
    at, de = m["at"], m["de"]
    lam = math.exp(m["mu"] + m["h"] + at.get(local, PRIOR_ASCENDIDO[0]) + de.get(visit, PRIOR_ASCENDIDO[1]))
    mu_ = math.exp(m["mu"] + at.get(visit, PRIOR_ASCENDIDO[0]) + de.get(local, PRIOR_ASCENDIDO[1]))
    pl = [math.exp(-lam) * lam ** i / math.factorial(i) for i in range(MAX_GOLES + 1)]
    pv = [math.exp(-mu_) * mu_ ** j / math.factorial(j) for j in range(MAX_GOLES + 1)]
    p1 = px = p2 = 0.0
    mejor = (0, 0, 0)
    for i in range(MAX_GOLES + 1):
        for j in range(MAX_GOLES + 1):
            p = pl[i] * pv[j]
            if i == 0 and j == 0: p *= 1 - lam * mu_ * RHO
            elif i == 0 and j == 1: p *= 1 + lam * RHO
            elif i == 1 and j == 0: p *= 1 + mu_ * RHO
            elif i == 1 and j == 1: p *= 1 - RHO
            if p > mejor[2]:
                mejor = (i, j, p)
            if i > j: p1 += p
            elif i == j: px += p
            else: p2 += p
    t = p1 + px + p2
    return p1 / t, px / t, p2 / t, lam, mu_, f"{mejor[0]}-{mejor[1]}"


# ------------------------------------------------------------ ejecucion

def guardar_csv(nombre, filas):
    if not filas:
        return
    with open(nombre, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=filas[0].keys())
        w.writeheader()
        w.writerows(filas)


def ejecutar(dias_pred=10):
    print("Descargando LaLiga desde 2022-23 (solo la primera vez tarda)...")
    partidos = descargar()
    jugados = [p for p in partidos if p["estado"] == "post"]
    equipos_por_temp = {}
    for p in partidos:
        equipos_por_temp.setdefault(p["temporada"], set()).update((p["local"], p["visit"]))
    eq_act, eq_prev = equipos_por_temp.get(ACTUAL, set()), equipos_por_temp.get(ACTUAL - 1, set())

    # Evaluacion: se reajusta antes de cada dia de partidos de esta temporada
    historial, modelo, dia_modelo = [], None, None
    for p in jugados:
        if p["temporada"] != ACTUAL:
            continue
        dia = p["fecha"].date()
        if dia != dia_modelo:
            corte = datetime(dia.year, dia.month, dia.day, tzinfo=timezone.utc)
            modelo, dia_modelo = ajustar(jugados, corte, eq_act, eq_prev), dia
        p1, px, p2, el, ev, marcador = probabilidades(modelo, p["local"], p["visit"])
        res = "1" if p["gl"] > p["gv"] else "X" if p["gl"] == p["gv"] else "2"
        pred = max((("1", p1), ("X", px), ("2", p2)), key=lambda x: x[1])[0]
        historial.append({"fecha": dia.isoformat(), "local": p["local"], "visitante": p["visit"],
                          "p1": round(p1, 3), "px": round(px, 3), "p2": round(p2, 3),
                          "goles_esp_local": round(el, 2), "goles_esp_visit": round(ev, 2),
                          "goles_local": p["gl"], "goles_visit": p["gv"], "resultado": res,
                          "prediccion": pred, "acierto": int(pred == res)})

    n = len(historial)
    if n:
        acierto = sum(h["acierto"] for h in historial) / n
        rps = 0.0     # Ranked Probability Score (menor es mejor)
        for h in historial:
            o = {"1": (1, 0, 0), "X": (0, 1, 0), "2": (0, 0, 1)}[h["resultado"]]
            c1 = h["p1"] - o[0]
            c2 = h["p1"] + h["px"] - o[0] - o[1]
            rps += (c1 ** 2 + c2 ** 2) / 2
        rps /= n
        local = sum(h["resultado"] == "1" for h in historial) / n
        print(f"\nPartidos evaluados: {n}   Acierto: {acierto:.1%}   RPS: {rps:.3f}")
        print(f"(referencia: apostar siempre por el local acertaria {local:.1%})")

    # Modelo con todo lo jugado hasta ahora
    ahora = datetime.now(timezone.utc)
    modelo = ajustar(jugados, ahora, eq_act, eq_prev)
    preds = []
    for p in partidos:
        if p["estado"] == "pre" and ahora - timedelta(hours=3) <= p["fecha"] <= ahora + timedelta(days=dias_pred):
            p1, px, p2, el, ev, marcador = probabilidades(modelo, p["local"], p["visit"])
            preds.append({"id": p["id"], "hora_utc": p["fecha"].strftime("%Y-%m-%dT%H:%MZ"),
                          "fecha": p["fecha"].date().isoformat(), "local": p["local"], "visitante": p["visit"],
                          "p1": round(p1, 3), "px": round(px, 3), "p2": round(p2, 3),
                          "goles_esp_local": round(el, 2), "goles_esp_visit": round(ev, 2),
                          "marcador_probable": marcador})
    print(f"\nPredicciones proximos {dias_pred} dias:")
    for pr in preds:
        print(f"  {pr['fecha']}  {pr['local']} - {pr['visitante']}:  1 {pr['p1']:.0%}  X {pr['px']:.0%}  2 {pr['p2']:.0%}"
              f"   ({pr['marcador_probable']})")

    # Ranking: diferencia de goles esperada por partido contra un rival medio (neutral)
    ranking = []
    for e in sorted(eq_act):
        gf = math.exp(modelo["mu"] + modelo["at"].get(e, 0))
        gc = math.exp(modelo["mu"] + modelo["de"].get(e, 0))
        ranking.append({"equipo": e, "goles_favor": round(gf, 2), "goles_contra": round(gc, 2),
                        "diferencia": round(gf - gc, 2)})
    ranking.sort(key=lambda r: -r["diferencia"])
    print("\nRanking (goles a favor / en contra esperados por partido ante un rival medio):")
    for i, r in enumerate(ranking, 1):
        print(f"  {i:2d}. {r['equipo']:25s} {r['goles_favor']:.2f} / {r['goles_contra']:.2f}  ({r['diferencia']:+.2f})")

    # Clasificacion real de esta temporada
    tabla = {}
    for p in jugados:
        if p["temporada"] != ACTUAL:
            continue
        for e, gf, gc in ((p["local"], p["gl"], p["gv"]), (p["visit"], p["gv"], p["gl"])):
            t = tabla.setdefault(e, {"equipo": e, "pj": 0, "pts": 0, "gf": 0, "gc": 0})
            t["pj"] += 1; t["gf"] += gf; t["gc"] += gc
            t["pts"] += 3 if gf > gc else 1 if gf == gc else 0
    clasif = sorted(tabla.values(), key=lambda t: (-t["pts"], -(t["gf"] - t["gc"]), -t["gf"]))

    guardar_csv("futbol_historial.csv", historial)
    guardar_csv("futbol_proximos.csv", preds)
    guardar_csv("futbol_ratings.csv", ranking)
    guardar_csv("futbol_clasificacion.csv", clasif)
    # Registro de cada ejecucion para ver como cambian los porcentajes
    nombre = "futbol_evolucion.csv"
    sello = ahora.strftime("%Y-%m-%dT%H:%MZ")
    filas = [{"ejecucion_utc": sello, **{k: p[k] for k in ("id", "local", "visitante", "p1", "px", "p2")}} for p in preds]
    if filas:
        nuevo = not os.path.exists(nombre)
        with open(nombre, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=filas[0].keys())
            if nuevo:
                w.writeheader()
            w.writerows(filas)
    try:
        import dashboard_futbol
        dashboard_futbol.generar()
        print("\nDashboard actualizado: futbol.html")
    except Exception as e:
        print(f"\nNo se pudo generar el dashboard de futbol: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=10, help="dias a predecir")
    ejecutar(ap.parse_args().dias)
