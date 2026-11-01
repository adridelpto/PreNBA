# Modelo Elo NBA

Predice partidos de la NBA con un sistema Elo usando solo la temporada actual
(K=20, ventaja de campo +70, back-to-back -25, multiplicador por margen de victoria).

## Requisitos (solo la primera vez)

```
pip install requests
```

## Uso diario

Abre una terminal en esta carpeta (`C:\Users\Adrián\Desktop\prenba`) y ejecuta:

```
python nba_elo.py
```

O haz doble clic en `actualizar.bat`.

Opciones:

- `--dias 3` predice los próximos 3 días (por defecto 7).
- `--fuente nba` usa el calendario oficial de cdn.nba.com en vez de ESPN
  (ahora mismo devuelve error 403 desde scripts).
- `--json archivo.json` usa un `scheduleLeagueV2.json` descargado a mano desde el navegador.

Lo ideal es ejecutarlo una vez al día, por la mañana (hora de España), cuando ya
han terminado los partidos de la noche.

## Qué genera

- `ratings.csv`: ranking Elo actual de los 30 equipos.
- `predicciones_proximas.csv`: favorito, probabilidad del local y spread estimado.
- `historial_predicciones.csv`: cada partido jugado con la probabilidad que daba el
  modelo **antes** del partido y si acertó. Sirve para medir la precisión real.
- `cache_espn/`: días ya descargados, para que cada ejecución sea rápida. Se puede
  borrar sin problema; se vuelve a descargar.

## Modelo 2: Elo + jugadores (`nba_jugadores.py`)

```
python nba_jugadores.py
```

`actualizar.bat` ejecuta los dos modelos.

1. Descarga de ESPN las estadísticas de todos los jugadores de las últimas 5 temporadas
   completas y de la actual.
2. Rating de cada jugador = Game Score por 36 minutos. Las temporadas recientes pesan
   más, y quien ha jugado pocos minutos se acerca al nivel de un jugador de reemplazo.
3. Coge las plantillas actuales y quita a los lesionados marcados como "Out".
   Fuerza de plantilla = suma de rating × minutos esperados.
4. La diferencia de fuerza se pasa a puntos Elo. La escala se calibró con la temporada
   2025-26 (0,53 puntos de margen por punto de fuerza).
5. Se mezcla con el Elo de ganar/perder de esta temporada. Con 0 partidos jugados manda
   la plantilla; con 25 partidos jugados, 50/50; después pesa cada vez más el Elo.

Genera los mismos archivos que el modelo 1 con el prefijo `jug_`, más `jug_jugadores.csv`
(rating de cada jugador) y `jug_ratings.csv` (ranking combinado).

## Dashboard

Cada ejecución genera `dashboard.html` (y una copia `index.html` para publicarla en la web).
Ábrelo con doble clic. Muestra:

- Próximos partidos con el favorito y su probabilidad. La columna "Cambio" indica
  cuánto ha subido o bajado desde la primera vez que se predijo ese partido.
- Pulsando un partido, la evolución de su probabilidad entre actualizaciones.
- Evolución del Elo de hasta 3 equipos, el ranking, el acierto acumulado y los últimos resultados.

`evolucion_predicciones.csv` guarda las predicciones de cada ejecución. Es lo que
permite ver cómo cambian los porcentajes. No lo borres.

## Actualización automática (GitHub)

`.github/workflows/actualizar.yml` está preparado para que GitHub ejecute el modelo cada
3 días en sus servidores, guarde los CSV y publique el dashboard en GitHub Pages.
Funciona con el PC apagado y es gratis. Para activarlo hace falta subir esta carpeta a
un repositorio de GitHub y activar Pages (rama `main`, carpeta raíz).

## Cómo leer los resultados

- **Precisión**: % de partidos en que el favorito del modelo ganó. Elegir siempre
  al local da ~55-58%; un buen modelo NBA ronda 65-68%.
- **Brier** (menor es mejor, 0.25 = moneda al aire) y **LogLoss** (0.693 = moneda al aire).
- Al principio de temporada todos los equipos empiezan en 1500, así que las
  primeras semanas el modelo acierta poco. Mejora a medida que se juegan partidos.
