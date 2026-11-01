"""Genera futbol.html a partir de los CSV de futbol_laliga.py.
    python dashboard_futbol.py
"""
import json
import os
from datetime import datetime, timezone

from dashboard import leer


def generar(salida="futbol.html"):
    datos = {
        "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "historial": leer("futbol_historial.csv"),
        "proximos": leer("futbol_proximos.csv"),
        "ratings": leer("futbol_ratings.csv"),
        "clasificacion": leer("futbol_clasificacion.csv"),
        "evolucion": leer("futbol_evolucion.csv"),
    }
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "futbol_plantilla.html"), encoding="utf-8") as f:
        plantilla = f.read()
    with open(salida, "w", encoding="utf-8") as f:
        f.write(plantilla.replace("/*__DATOS__*/null", json.dumps(datos, ensure_ascii=False)))


if __name__ == "__main__":
    generar()
    print("futbol.html generado")
