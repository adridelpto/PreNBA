"""Genera dashboard.html a partir de los CSV que crea nba_elo.py.
Se llama solo al final de nba_elo.py; tambien se puede ejecutar a mano:
    python dashboard.py
"""
import csv
import json
import os
from datetime import datetime, timezone

EQUIPOS = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GS": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NO": "New Orleans Pelicans", "NY": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SA": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTAH": "Utah Jazz", "WSH": "Washington Wizards",
}


def leer(nombre):
    if not os.path.exists(nombre):
        return []
    with open(nombre, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def generar(salida="dashboard.html"):
    datos = {
        "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "equipos": EQUIPOS,
        "evolucion_elo": leer("evolucion_elo.csv"),
        "modelos": {
            "elo": {
                "nombre": "Elo de equipo",
                "ratings": leer("ratings.csv"),
                "proximos": leer("predicciones_proximas.csv"),
                "historial": leer("historial_predicciones.csv"),
                "evolucion_pred": leer("evolucion_predicciones.csv"),
            },
            "jugadores": {
                "nombre": "Elo + jugadores",
                "ratings": leer("jug_ratings.csv"),
                "proximos": leer("jug_predicciones_proximas.csv"),
                "historial": leer("jug_historial_predicciones.csv"),
                "evolucion_pred": leer("jug_evolucion_predicciones.csv"),
                "jugadores": leer("jug_jugadores.csv")[:60],
            },
        },
    }
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "dashboard_plantilla.html"), encoding="utf-8") as f:
        plantilla = f.read()
    html = plantilla.replace("/*__DATOS__*/null", json.dumps(datos, ensure_ascii=False))
    with open(salida, "w", encoding="utf-8") as f:
        f.write(html)
    # Copia como index.html para publicarlo en GitHub Pages
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    generar()
    print("dashboard.html generado")
