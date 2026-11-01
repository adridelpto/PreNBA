"""
Modelo 3: proyeccion de estadisticas individuales por jugador y partido
(puntos, rebotes, asistencias, triples, tiros, robos, tapones, P+R+A).

Uso:
    python nba_props.py            # proximos 7 dias
    python nba_props.py --dias 3

Como proyecta cada estadistica de un jugador en un partido:
  1. Ritmo por minuto: mezcla de esta temporada (box scores partido a partido)
     y de las dos anteriores (ESPN), pesando mas lo reciente, y acercando a la
     media de la liga a quien tiene pocos minutos.
  2. Minutos esperados: media de sus ultimos 5 partidos mezclada con su media
     de la temporada (o la de la temporada pasada si aun no ha jugado).
  3. Rival: cuanto concede el rival de esa estadistica frente a la media de la
     liga (ej. equipos que permiten muchos triples), suavizado al principio.
  4. Proyeccion = ritmo x minutos x factor rival. El rango (percentil 20-80)
     sale de la variabilidad partido a partido del propio jugador.
Los lesionados "Out" no se proyectan.

Para medir el error se repite el calculo en cada partido ya jugado usando solo
lo que se sabia antes de ese partido, y se compara con lo que hizo el jugador.
"""
import argparse
import json
import math
import os
from datetime import datetime, timedelta, timezone

import requests

import nba_elo as m1
import nba_jugadores as m2

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}"
BOX_CACHE = os.path.join(m1.CACHE_DIR, "box")

# Estadisticas que se proyectan: clave -> nombre visible
STATS = {"pts": "Puntos", "reb": "Rebotes", "ast": "Asistencias", "tri": "Triples",
         "tiros": "Tiros de campo", "rob": "Robos", "tap": "Tapones", "pra": "P+R+A"}
BASE = ["pts", "reb", "ast", "tri", "tiros", "t3a", "rob", "tap"]   # se guardan por minuto
PESO_ACTUAL = 1.5          # peso por minuto de esta temporada
PESOS_PREVIAS = {1: 1.0, 2: 0.5}
MIN_PRIOR = 150            # minutos ficticios a la media de la liga
MIN_PROYECTAR = 12         # solo se muestran jugadores con 12+ minutos esperados
Z80 = 0.8416               # percentil 80 de la normal


# ------------------------------------------------------------- datos

def box_score(event_id):
    """Lista de filas {id, nombre, equipo, rival, min, pts, ...} de un partido terminado."""
    ruta = os.path.join(BOX_CACHE, f"{event_id}.json")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    s = requests.get(SUMMARY_URL.format(event_id), timeout=30).json()
    equipos = s.get("boxscore", {}).get("players", [])
    abrs = [t["team"]["abbreviation"] for t in equipos]
    filas = []
    for t in equipos:
        eq = t["team"]["abbreviation"]
        rival = next((a for a in abrs if a != eq), "")
        st = t["statistics"][0]
        idx = {k: i for i, k in enumerate(st["keys"])}
        for a in st.get("athletes", []):
            v = a.get("stats") or []
            if a.get("didNotPlay") or not v:
                continue
            try:
                mins = float(v[idx["minutes"]])
            except (ValueError, KeyError):
                continue
            if mins <= 0:
                continue
            fg = v[idx["fieldGoalsMade-fieldGoalsAttempted"]].split("-")
            t3 = v[idx["threePointFieldGoalsMade-threePointFieldGoalsAttempted"]].split("-")
            n = lambda k: float(v[idx[k]] or 0)
            filas.append({"id": a["athlete"]["id"], "nombre": a["athlete"]["displayName"],
                          "equipo": eq, "rival": rival, "min": mins, "pts": n("points"),
                          "reb": n("rebounds"), "ast": n("assists"), "rob": n("steals"),
                          "tap": n("blocks"), "tiros": float(fg[1]), "tri": float(t3[0]),
                          "t3a": float(t3[1])})
    os.makedirs(BOX_CACHE, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(filas, f)
    return filas


def totales_previos():
    """{season: {id: {min, pj, pts, reb, ...}}} de las dos temporadas anteriores."""
    res = {}
    for k in PESOS_PREVIAS:
        season = m2.TEMPORADA_ACTUAL - k
        data = m2._get_json(m2.STATS_URL.format(season),
                            os.path.join(m1.CACHE_DIR, f"jugadores_{season}.json"))
        jug = {}
        for a in data["athletes"]:
            v = {}
            for c in a["categories"]:
                nombres = next(cc["names"] for cc in data["categories"] if cc["name"] == c["name"])
                v.update(dict(zip(nombres, c["values"])))
            if v.get("minutes", 0) <= 0:
                continue
            jug[a["athlete"]["id"]] = {
                "min": v["minutes"], "pj": v.get("gamesPlayed", 0),
                "pts": v.get("points", 0), "reb": v.get("rebounds", 0), "ast": v.get("assists", 0),
                "rob": v.get("steals", 0), "tap": v.get("blocks", 0),
                "tiros": v.get("fieldGoalsAttempted", 0),
                "tri": v.get("threePointFieldGoalsMade", 0),
                "t3a": v.get("threePointFieldGoalsAttempted", 0)}
        res[season] = jug
    return res


# ------------------------------------------------------------- modelo

class Estado:
    """Lo que se sabe en un momento de la temporada (se va actualizando partido a partido)."""

    def __init__(self, previas):
        self.previas = previas
        self.juegos = {}          # id -> lista de filas de esta temporada
        self.concedido = {}       # equipo -> {stat: total concedido}, y "pj"
        # Media de la liga por minuto (temporada anterior) para regresion
        prev = previas[m2.TEMPORADA_ACTUAL - 1]
        mins = sum(j["min"] for j in prev.values())
        self.liga = {s: sum(j[s] for j in prev.values()) / mins for s in BASE}

    def anadir_partido(self, filas):
        por_eq = {}
        for f in filas:
            self.juegos.setdefault(f["id"], []).append(f)
            por_eq.setdefault(f["rival"], {s: 0.0 for s in BASE})
            for s in BASE:
                por_eq[f["rival"]][s] += f[s]
        for eq, tot in por_eq.items():        # lo que concede `eq` (rival de esos jugadores)
            c = self.concedido.setdefault(eq, {s: 0.0 for s in BASE} | {"pj": 0})
            for s in BASE:
                c[s] += tot[s]
            c["pj"] += 1

    def ritmo(self, pid):
        num = {s: self.liga[s] * MIN_PRIOR for s in BASE}
        den = MIN_PRIOR
        for k, w in PESOS_PREVIAS.items():
            j = self.previas[m2.TEMPORADA_ACTUAL - k].get(pid)
            if j:
                for s in BASE:
                    num[s] += w * j[s]
                den += w * j["min"]
        for f in self.juegos.get(pid, []):
            for s in BASE:
                num[s] += PESO_ACTUAL * f[s]
            den += PESO_ACTUAL * f["min"]
        return {s: num[s] / den for s in BASE}

    def minutos(self, pid):
        js = self.juegos.get(pid, [])
        prev = self.previas[m2.TEMPORADA_ACTUAL - 1].get(pid)
        prev_mpg = prev["min"] / prev["pj"] if prev and prev["pj"] >= 5 else None
        if len(js) >= 3:
            ult = sum(f["min"] for f in js[-5:]) / len(js[-5:])
            temp = sum(f["min"] for f in js) / len(js)
            return 0.6 * ult + 0.4 * temp
        if js:
            act = sum(f["min"] for f in js) / len(js)
            return (act * len(js) + (prev_mpg or act) * 2) / (len(js) + 2)
        return prev_mpg

    def factor_rival(self, rival, s):
        c = self.concedido.get(rival)
        if not c or c["pj"] == 0:
            return 1.0
        medias = [x[s] / x["pj"] for x in self.concedido.values() if x["pj"]]
        media = sum(medias) / len(medias)
        if media <= 0:
            return 1.0
        bruto = (c[s] / c["pj"]) / media
        return 1 + (bruto - 1) * c["pj"] / (c["pj"] + 10)

    def desviacion(self, pid, s, proy):
        """Desviacion tipica partido a partido, mezclada con una de referencia."""
        ref = math.sqrt(max(proy, 0.3)) * (1.25 if s in ("pts", "pra", "tiros") else 1.0)
        js = [f for f in self.juegos.get(pid, []) if f["min"] >= 10]
        if len(js) < 4:
            return ref
        vals = [f[s] if s != "pra" else f["pts"] + f["reb"] + f["ast"] for f in js]
        m = sum(vals) / len(vals)
        sd = math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))
        return (sd * len(js) + ref * 6) / (len(js) + 6)

    def proyectar(self, pid, rival, mins=None):
        mins = self.minutos(pid) if mins is None else mins
        if not mins:
            return None
        r = self.ritmo(pid)
        p = {s: r[s] * mins * self.factor_rival(rival, s) for s in BASE}
        p["pra"] = p["pts"] + p["reb"] + p["ast"]
        p["min"] = mins
        return p

    def media_simple(self, pid):
        """Referencia ingenua: su media por partido esta temporada (3+ partidos) o la pasada."""
        js = self.juegos.get(pid, [])
        if len(js) >= 3:
            m = {s: sum(f[s] for f in js) / len(js) for s in BASE}
        else:
            prev = self.previas[m2.TEMPORADA_ACTUAL - 1].get(pid)
            if not prev or prev["pj"] < 5:
                return None
            m = {s: prev[s] / prev["pj"] for s in BASE}
        m["pra"] = m["pts"] + m["reb"] + m["ast"]
        return m


def ejecutar(dias_pred=7):
    partidos = m1.descargar_espn(dias_pred)
    jugados = [p for p in partidos if p["estado"] == 3]
    print(f"Descargando box scores de {len(jugados)} partidos (solo la primera vez tarda)...")
    est = Estado(totales_previos())

    # Evaluacion honesta: proyectar cada partido antes de anadirlo
    err = {s: [0.0, 0.0, 0] for s in STATS}        # [error modelo, error media simple, n]
    zs = {s: [] for s in STATS}                     # |real - proy| / desviacion, para calibrar el rango
    ultimos = []
    for p in jugados:
        filas = box_score(p["id"])
        for f in filas:
            if f["min"] < 10:
                continue
            pr = est.proyectar(f["id"], f["rival"])
            base = est.media_simple(f["id"])
            if not pr or not base:
                continue
            real = dict(f, pra=f["pts"] + f["reb"] + f["ast"])
            for s in STATS:
                err[s][0] += abs(pr[s] - real[s])
                err[s][1] += abs(base[s] - real[s])
                err[s][2] += 1
            for s in STATS:
                zs[s].append(abs(real[s] - pr[s]) / est.desviacion(f["id"], s, pr[s]))
            ultimos.append({"fecha": p["fecha"].date().isoformat(), "jugador": f["nombre"],
                            "equipo": f["equipo"], "rival": f["rival"], "min_real": int(f["min"]),
                            **{f"{s}_proy": round(pr[s], 1) for s in ("pts", "reb", "ast", "tri")},
                            **{f"{s}_real": int(real[s]) for s in ("pts", "reb", "ast", "tri")}})
        est.anadir_partido(filas)

    evaluacion = {s: {"nombre": STATS[s], "error_modelo": round(e[0] / e[2], 2),
                      "error_media": round(e[1] / e[2], 2), "n": e[2]} for s, e in err.items() if e[2]}
    # Escala del rango para que el 60% de los resultados reales caiga dentro (percentil 20-80)
    escala = {s: (sorted(z)[int(len(z) * 0.6)] / Z80 if z else 1.0) for s, z in zs.items()}
    for s in STATS:
        if s in evaluacion:
            evaluacion[s]["escala_rango"] = round(escala[s], 2)
    print("\nError medio por jugador y partido (modelo vs. su media simple):")
    for s, e in evaluacion.items():
        if not s.startswith("_"):
            print(f"  {e['nombre']:15s} {e['error_modelo']:5.2f}  vs  {e['error_media']:5.2f}   ({e['n']} casos)")

    # Proyecciones de los proximos partidos
    plantilla, lesiones, nombres = m2.plantillas_actuales()
    por_eq = {}
    for pid, eq in plantilla.items():
        por_eq.setdefault(eq, []).append(pid)
    ahora = datetime.now(timezone.utc)
    proximos = []
    for p in partidos:
        if p["estado"] != 1 or p["fecha"] > ahora + timedelta(days=dias_pred):
            continue
        for eq, rival, local in ((p["local"], p["visit"], True), (p["visit"], p["local"], False)):
            for pid in por_eq.get(eq, []):
                estado = lesiones.get(pid, "")
                if estado.lower() == "out":
                    continue
                pr = est.proyectar(pid, rival)
                if not pr or pr["min"] < MIN_PROYECTAR:
                    continue
                fila = {"id_partido": p["id"], "hora_utc": p["fecha"].strftime("%Y-%m-%dT%H:%MZ"),
                        "partido": f"{p['visit']} @ {p['local']}", "jugador": nombres.get(pid, pid),
                        "equipo": eq, "rival": rival, "local": int(local), "estado": estado,
                        "min": round(pr["min"], 1)}
                for s in STATS:
                    sd = est.desviacion(pid, s, pr[s]) * escala[s]
                    fila[s] = round(pr[s], 1)
                    fila[s + "_rango"] = f"{max(0, pr[s] - Z80 * sd):.0f}-{pr[s] + Z80 * sd:.0f}"
                proximos.append(fila)
    proximos.sort(key=lambda f: (f["hora_utc"], f["partido"], -f["pts"]))
    print(f"\n{len(proximos)} proyecciones de jugador para los proximos {dias_pred} dias")

    m1.guardar_csv("props_proximos.csv", proximos)
    m1.guardar_csv("props_ultimos.csv", ultimos[-400:])
    with open("props_evaluacion.json", "w", encoding="utf-8") as f:
        json.dump(evaluacion, f, ensure_ascii=False, indent=1)
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
