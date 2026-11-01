"""
Cuotas de casas de apuestas (DraftKings, via ESPN) y simulacion de rentabilidad.

Uso (despues de nba_elo.py y nba_jugadores.py):
    python cuotas.py

Que hace:
  1. Para cada partido ya jugado descarga la cuota de cierre a ganador (moneyline).
  2. Para los partidos de hoy/manana descarga la cuota actual (ESPN solo la
     publica el mismo dia del partido).
  3. Pasa las cuotas a formato europeo (decimal) y calcula la probabilidad que
     da la casa quitando su margen.
  4. Simula, con cada modelo, apostar 1 unidad a cada partido donde el modelo
     ve "valor": prob_modelo x cuota - 1 >= UMBRAL. Calcula beneficio y ROI.
"""
import csv
import json
import os
from datetime import datetime, timedelta, timezone

import requests

import nba_elo as m1

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={}"
UMBRAL = 0.05          # valor minimo (5%) para considerar una apuesta
CACHE = os.path.join(m1.CACHE_DIR, "cuotas")
MODELOS = {"elo": "", "jugadores": "jug_"}


def americana_a_decimal(a):
    a = float(str(a).replace("+", ""))
    return 1 + (a / 100 if a > 0 else 100 / -a)


def cuota_cierre(event_id):
    """(cuota_local, cuota_visitante) decimales, o None si no hay."""
    ruta = os.path.join(CACHE, f"{event_id}.json")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            c = json.load(f)
    else:
        s = requests.get(SUMMARY_URL.format(event_id), timeout=30).json()
        c = None
        for p in s.get("pickcenter") or []:
            ml_l = p.get("homeTeamOdds", {}).get("moneyLine")
            ml_v = p.get("awayTeamOdds", {}).get("moneyLine")
            if ml_l and ml_v:
                c = {"casa": p.get("provider", {}).get("name", ""), "local": ml_l, "visit": ml_v}
                break
        os.makedirs(CACHE, exist_ok=True)
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(c, f)
    if not c:
        return None
    return americana_a_decimal(c["local"]), americana_a_decimal(c["visit"]), c["casa"]


def cuotas_actuales():
    """{id: (cuota_local, cuota_visit, casa)} para hoy y manana (si ya estan publicadas)."""
    res = {}
    hoy = datetime.now(timezone.utc).date()
    for d in (hoy, hoy + timedelta(days=1)):
        data = requests.get(m1.ESPN_URL.format(d.strftime("%Y%m%d")), timeout=30).json()
        for e in data.get("events", []):
            if e["status"]["type"]["state"] != "pre":
                continue
            for o in e["competitions"][0].get("odds") or []:
                ml = o.get("moneyline") or {}
                try:
                    l = ml["home"]["close"]["odds"]
                    v = ml["away"]["close"]["odds"]
                except KeyError:
                    continue
                res[e["id"]] = (americana_a_decimal(l), americana_a_decimal(v),
                                o.get("provider", {}).get("name", ""))
                break
    return res


def prob_mercado(cl, cv):
    """Probabilidad del local segun la casa, sin su margen."""
    il, iv = 1 / cl, 1 / cv
    return il / (il + iv)


def leer(nombre):
    if not os.path.exists(nombre):
        return []
    with open(nombre, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    partidos = m1.descargar_espn(1)
    clave = {(p["fecha"].date().isoformat(), p["local"], p["visit"]): p for p in partidos}
    jugados = [p for p in partidos if p["estado"] == 3]
    print(f"Descargando cuotas de cierre de {len(jugados)} partidos (solo la primera vez tarda)...")
    cierre = {}
    for p in jugados:
        c = cuota_cierre(p["id"])
        if c:
            cierre[p["id"]] = c

    filas_cuotas = []
    for p in jugados:
        if p["id"] in cierre:
            cl, cv, casa = cierre[p["id"]]
            filas_cuotas.append({"id": p["id"], "fecha": p["fecha"].date().isoformat(),
                                 "local": p["local"], "visitante": p["visit"], "casa": casa,
                                 "cuota_local": round(cl, 2), "cuota_visit": round(cv, 2),
                                 "prob_mercado_local": round(prob_mercado(cl, cv), 3),
                                 "gana_local": int(p["pts_local"] > p["pts_visit"])})
    m1.guardar_csv("cuotas_historial.csv", filas_cuotas)

    resumen = {"umbral": UMBRAL, "modelos": {}}
    # Referencia: que tal predice la propia casa de apuestas
    if filas_cuotas:
        n = len(filas_cuotas)
        resumen["mercado"] = {
            "partidos": n,
            "acierto": round(sum((f["prob_mercado_local"] > .5) == bool(f["gana_local"]) for f in filas_cuotas) / n, 3),
            "brier": round(sum((f["prob_mercado_local"] - f["gana_local"]) ** 2 for f in filas_cuotas) / n, 4),
        }

    actuales = cuotas_actuales()
    for modelo, pref in MODELOS.items():
        apuestas = []
        brier_mod, n_comp = 0.0, 0
        for h in leer(pref + "historial_predicciones.csv"):
            p = clave.get((h["fecha"], h["local"], h["visitante"]))
            if not p or p["id"] not in cierre:
                continue
            cl, cv, _ = cierre[p["id"]]
            pl = float(h["prob_local"])
            gana_l = int(h["pts_local"]) > int(h["pts_visit"])
            brier_mod += (pl - gana_l) ** 2
            n_comp += 1
            for lado, prob, cuota, gana in (("local", pl, cl, gana_l), ("visitante", 1 - pl, cv, not gana_l)):
                valor = prob * cuota - 1
                if valor >= UMBRAL:
                    apuestas.append({"fecha": h["fecha"], "partido": f"{h['visitante']} @ {h['local']}",
                                     "apuesta": h["local"] if lado == "local" else h["visitante"],
                                     "cuota": round(cuota, 2), "prob_modelo": round(prob, 3),
                                     "valor": round(valor, 3), "ganada": int(gana),
                                     "beneficio": round(cuota - 1 if gana else -1, 2)})
        m1.guardar_csv(pref + "apuestas_simuladas.csv", apuestas)
        n = len(apuestas)
        benef = sum(a["beneficio"] for a in apuestas)
        resumen["modelos"][modelo] = {
            "apuestas": n, "ganadas": sum(a["ganada"] for a in apuestas),
            "beneficio_unidades": round(benef, 2), "roi": round(benef / n, 3) if n else 0,
            "brier_modelo": round(brier_mod / n_comp, 4) if n_comp else None, "partidos_comparados": n_comp,
        }

        # Valor en los proximos partidos con cuota publicada
        proximos = leer(pref + "predicciones_proximas.csv")
        for pr in proximos:
            c = actuales.get(pr["id"])
            pr.update({"cuota_local": "", "cuota_visit": "", "prob_mercado_local": "", "valor_local": "", "valor_visit": ""})
            if c:
                cl, cv, _ = c
                pl = float(pr["prob_local"])
                pr.update({"cuota_local": round(cl, 2), "cuota_visit": round(cv, 2),
                           "prob_mercado_local": round(prob_mercado(cl, cv), 3),
                           "valor_local": round(pl * cl - 1, 3), "valor_visit": round((1 - pl) * cv - 1, 3)})
        m1.guardar_csv(pref + "predicciones_proximas.csv", proximos)

    with open("apuestas_resumen.json", "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=1)

    print(f"\nCasa de apuestas: acierto {resumen.get('mercado', {}).get('acierto')}, "
          f"Brier {resumen.get('mercado', {}).get('brier')} en {resumen.get('mercado', {}).get('partidos')} partidos")
    for m, r in resumen["modelos"].items():
        print(f"Modelo {m}: Brier {r['brier_modelo']} | {r['apuestas']} apuestas con valor >= {UMBRAL:.0%}, "
              f"{r['ganadas']} ganadas, beneficio {r['beneficio_unidades']:+.2f} u, ROI {r['roi']:+.1%}")
    print(f"Cuotas publicadas para {len(actuales)} proximos partidos")

    try:
        import dashboard
        dashboard.generar()
        print("\nDashboard actualizado: dashboard.html")
    except Exception as e:
        print(f"\nNo se pudo generar el dashboard: {e}")


if __name__ == "__main__":
    main()
