#!/usr/bin/env python3
"""
Trail Calculator -- Strava OAuth + analisis D+/h + calculadora personalizada
Desplegable en Railway / Render / Replit con 3 variables de entorno.
"""
import os, requests, json
from flask import Flask, redirect, request, session, render_template_string, url_for
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'cambia-esto-en-produccion')

STRAVA_CLIENT_ID     = os.environ.get('STRAVA_CLIENT_ID', '')
STRAVA_CLIENT_SECRET = os.environ.get('STRAVA_CLIENT_SECRET', '')
BASE_URL             = os.environ.get('BASE_URL', 'http://localhost:5000')

MOUNTAIN_TYPES  = {'TrailRun', 'Run', 'Hike', 'Walk'}
MIN_ACT_DPLUS   = 200
MIN_CLIMB_DPLUS = 150
REAL_RATES = {'s': 500, 'm': 480, 'l': 450, 'xl': 420}  # fallback si no hay segmentos GPS en esa categoria

# -- Analisis ----------------------------------------------------------

def get_cat(km):
    if km < 2:  return 's'
    if km < 5:  return 'm'
    if km < 8:  return 'l'
    return 'xl'

def fetch_activities(token, max_pages=10):
    acts = []
    for page in range(1, max_pages + 1):
        r = requests.get(
            'https://www.strava.com/api/v3/athlete/activities',
            headers={'Authorization': f'Bearer {token}'},
            params={'per_page': 100, 'page': page},
            timeout=15
        )
        if r.status_code != 200: break
        batch = r.json()
        if not batch: break
        acts.extend(batch)
    return acts

ANALYZE_TYPES = {'Run', 'TrailRun', 'Hike', 'Walk'}

def analyze(activities):
    buckets = defaultdict(list)
    stats = {'km': 0, 'dplus': 0, 'sessions': 0, 'trail': 0}
    for a in activities:
        sport = a.get('sport_type') or a.get('type', '')
        if sport not in ANALYZE_TYPES: continue
        dist_m = a.get('distance', 0)
        time_s = a.get('moving_time', 0)
        dplus  = a.get('total_elevation_gain', 0)
        if dist_m < 500 or time_s < 60: continue
        dist_km = dist_m / 1000
        time_h  = time_s / 3600
        stats['km']       += dist_km
        stats['dplus']    += dplus
        stats['sessions'] += 1
        if sport == 'TrailRun': stats['trail'] += 1
        if dplus >= 60 and time_h > 0:
            buckets[get_cat(dist_km)].append(round(dplus / time_h))

    def avg(lst, default):
        return round(sum(lst) / len(lst)) if lst else default

    rates = {
        's':  avg(buckets['s'],  REAL_RATES['s']),
        'm':  avg(buckets['m'],  REAL_RATES['m']),
        'l':  avg(buckets['l'],  REAL_RATES['l']),
        'xl': avg(buckets['xl'], REAL_RATES['xl']),
        'n_s':  len(buckets['s']),
        'n_m':  len(buckets['m']),
        'n_l':  len(buckets['l']),
        'n_xl': len(buckets['xl']),
    }
    stats['km']    = round(stats['km'])
    stats['dplus'] = round(stats['dplus'])
    return rates, stats


def analyze_from_segments(segments):
    """Calcula tasas D+/h por categoria usando segmentos GPS (subida pura)."""
    buckets = defaultdict(list)
    for seg in segments:
        cat = get_cat(seg['dist_km'])
        buckets[cat].append(seg['rate'])
    def avg(lst, default):
        return round(sum(lst) / len(lst)) if lst else default
    return {
        's':   avg(buckets['s'],   REAL_RATES['s']),
        'm':   avg(buckets['m'],   REAL_RATES['m']),
        'l':   avg(buckets['l'],   REAL_RATES['l']),
        'xl':  avg(buckets['xl'],  REAL_RATES['xl']),
        'n_s': len(buckets['s']),
        'n_m': len(buckets['m']),
        'n_l': len(buckets['l']),
        'n_xl': len(buckets['xl']),
    }

def fmt_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f'{h}h {m:02d}m' if h > 0 else f'{m}m'


def detect_climbs(altitudes, times, distances):
    n = len(altitudes)
    if n < 20:
        return []
    w = 5
    smooth = []
    for i in range(n):
        lo, hi = max(0, i - w//2), min(n, i + w//2 + 1)
        smooth.append(sum(altitudes[lo:hi]) / (hi - lo))

    DROP_TOL = 25
    climbs = []
    i = 0
    while i < n - 1:
        if smooth[i+1] <= smooth[i]:
            i += 1
            continue
        start_i  = i
        max_alt  = smooth[i]
        max_i    = i
        cur_drop = 0
        j = i + 1
        while j < n:
            delta = smooth[j] - smooth[j-1]
            if delta > 0:
                if smooth[j] > max_alt:
                    max_alt = smooth[j]
                    max_i   = j
                cur_drop = 0
            else:
                cur_drop += -delta
                if cur_drop > DROP_TOL:
                    break
            j += 1

        gain    = max_alt - smooth[start_i]
        horiz_m = distances[max_i] - distances[start_i] if distances else 0
        if gain >= MIN_CLIMB_DPLUS:
            dt = times[max_i] - times[start_i]
            if dt > 60:
                rate = round(gain / (dt / 3600))
                if 100 <= rate <= 3000:
                    slope = round(gain / horiz_m * 100) if horiz_m > 10 else 0
                    climbs.append({
                        'dplus':   round(gain),
                        'time_s':  int(dt),
                        'rate':    rate,
                        'dist_km': round(horiz_m / 1000, 1) if horiz_m else 0,
                        'slope':   slope,
                    })
        i = max_i + 1
    return climbs


def _fetch_one_stream(token, act):
    try:
        r = requests.get(
            f'https://www.strava.com/api/v3/activities/{act["id"]}/streams',
            headers={'Authorization': f'Bearer {token}'},
            params={'keys': 'altitude,time,distance', 'key_by_type': 'true', 'resolution': 'medium'},
            timeout=12
        )
        if r.status_code != 200:
            return None
        streams   = r.json()
        alt_data  = streams.get('altitude', {}).get('data', [])
        t_data    = streams.get('time',     {}).get('data', [])
        dist_data = streams.get('distance', {}).get('data', [])
        if len(alt_data) < 20 or len(alt_data) != len(t_data):
            return None
        if len(dist_data) != len(alt_data):
            dist_data = [0] * len(alt_data)
        climbs = detect_climbs(alt_data, t_data, dist_data)
        return (act, climbs)
    except Exception:
        return None


def fetch_segments(token, activities, max_acts=30):
    mountain = [
        a for a in activities
        if (a.get('sport_type') or a.get('type', '')) in MOUNTAIN_TYPES
        and a.get('total_elevation_gain', 0) >= MIN_ACT_DPLUS
    ]
    mountain = sorted(mountain, key=lambda a: a.get('total_elevation_gain', 0), reverse=True)[:max_acts]

    segments = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_fetch_one_stream, token, act): act for act in mountain}
        for fut in as_completed(futures, timeout=25):
            result = fut.result()
            if not result:
                continue
            act, climbs = result
            name = act.get('name', 'Actividad')
            date = act.get('start_date_local', '')[:10]
            for c in climbs:
                segments.append({
                    'name':     name,
                    'date':     date,
                    'dplus':    c['dplus'],
                    'dist_km':  c['dist_km'],
                    'slope':    c['slope'],
                    'time_fmt': fmt_time(c['time_s']),
                    'rate':     c['rate'],
                })

    segments.sort(key=lambda s: s['dplus'], reverse=True)
    return segments

# -- Templates ---------------------------------------------------------

INDEX = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Trail Calculator</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f1117;--card:#1a1d27;--border:#2d3148;--text:#f0f2ff;
      --muted:#8892b0;--green:#10b981;--orange:#fc4c02;--radius:14px;
      --font:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--font);
     min-height:100vh;display:flex;flex-direction:column;align-items:center;
     justify-content:center;padding:24px;text-align:center}
.logo{font-size:42px;margin-bottom:16px}
h1{font-size:28px;font-weight:700;margin-bottom:8px}
.sub{font-size:15px;color:var(--muted);line-height:1.6;max-width:340px;margin:0 auto 36px}
.btn-strava{display:inline-flex;align-items:center;gap:12px;background:var(--orange);
            color:#fff;font-size:16px;font-weight:600;padding:16px 28px;
            border-radius:var(--radius);text-decoration:none;
            border:none;cursor:pointer;font-family:var(--font)}
.btn-strava:active{opacity:.85}
.strava-logo{width:24px;height:24px;fill:#fff}
.steps{margin-top:48px;text-align:left;max-width:320px;width:100%}
.step{display:flex;gap:14px;align-items:flex-start;margin-bottom:20px}
.step-num{background:var(--card);border:1px solid var(--border);border-radius:50%;
          width:32px;height:32px;display:flex;align-items:center;justify-content:center;
          font-size:13px;font-weight:600;flex-shrink:0;color:var(--green)}
.step-text{font-size:14px;color:var(--muted);line-height:1.5;padding-top:5px}
.step-text strong{color:var(--text)}
.footer{margin-top:48px;font-size:11px;color:#3d4466}
{% if error %}
.error{background:#2d1a1a;border:1px solid #7f1d1d;border-radius:10px;
       color:#fca5a5;font-size:13px;padding:12px 16px;margin-bottom:24px;max-width:340px}
{% endif %}
</style>
</head>
<body>
<div class="logo">&#x26F0;&#xFE0F;</div>
<h1>Trail Calculator</h1>
<p class="sub">Conecta tu Strava y obtene una calculadora de tiempos personalizada con tus tasas reales de ascenso.</p>
{% if error %}
<div class="error">{{ error }}</div>
{% endif %}
<a class="btn-strava" href="{{ auth_url }}">
  <svg class="strava-logo" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path d="M15.387 17.944l-2.089-4.116h-3.065L15.387 24l5.15-10.172h-3.066m-7.008-5.599l2.836 5.598h4.172L10.463 0l-7 13.828h4.169"/>
  </svg>
  Conectar con Strava
</a>
<div class="steps">
  <div class="step">
    <div class="step-num">1</div>
    <div class="step-text"><strong>Autorizas</strong> el acceso de lectura a tus actividades en Strava.</div>
  </div>
  <div class="step">
    <div class="step-num">2</div>
    <div class="step-text"><strong>Analizamos</strong> tus runs y trail runs: D+/h por categoria de subida.</div>
  </div>
  <div class="step">
    <div class="step-num">3</div>
    <div class="step-text"><strong>Obtenes</strong> tu calculadora personalizada para estimar tiempos de ascenso.</div>
  </div>
</div>
<div class="footer">Solo lectura &middot; Sin guardar datos &middot; Desconecta desde Strava cuando quieras</div>
</body>
</html>"""

ERROR_PAGE = """<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Error</title>
<style>
body{background:#0f1117;color:#f0f2ff;font-family:system-ui,sans-serif;
     min-height:100vh;display:flex;align-items:center;justify-content:center;
     flex-direction:column;text-align:center;padding:24px}
.box{background:#1a1d27;border:1px solid #7f1d1d;border-radius:14px;padding:24px;max-width:360px}
h2{color:#f87171;margin-bottom:12px}
p{font-size:14px;color:#8892b0;line-height:1.6}
a{color:#60a5fa;font-size:14px;display:block;margin-top:20px}
</style>
</head>
<body>
<div class="box">
<h2>Algo salio mal</h2>
<p>{{ msg }}</p>
<a href="/">&#x2190; Volver al inicio</a>
</div>
</body>
</html>"""


RESULT = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Tu calculadora &middot; Trail Calculator</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{--bg:#0f1117;--card:#1a1d27;--card2:#22263a;--border:#2d3148;
      --text:#f0f2ff;--muted:#8892b0;--sub:#5c6589;
      --red:#ff4d6d;--amber:#f59e0b;--green:#10b981;--blue:#60a5fa;
      --orange:#fc4c02;--radius:14px;--font:system-ui,-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--font);
     padding:0 0 48px;max-width:560px;margin:0 auto}
.header{background:var(--card);padding:16px;border-bottom:1px solid var(--border);
        display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10}
.avatar{width:40px;height:40px;border-radius:50%;object-fit:cover;border:2px solid var(--border)}
.avatar-fallback{width:40px;height:40px;border-radius:50%;background:var(--card2);
                 border:2px solid var(--border);display:flex;align-items:center;
                 justify-content:center;font-size:16px;font-weight:700;color:var(--green)}
.header-text h1{font-size:15px;font-weight:600}
.header-text p{font-size:12px;color:var(--muted);margin-top:2px}
.section{padding:16px 14px 0}
.stat-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:4px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;
           padding:12px 10px;text-align:center}
.stat-val{font-size:20px;font-weight:700;color:var(--green)}
.stat-label{font-size:10px;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.4px}
.section-title{font-size:11px;font-weight:600;color:var(--muted);letter-spacing:.6px;
               text-transform:uppercase;margin:20px 0 10px}
.rate-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.rate-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
           padding:14px 12px;text-align:center}
.rate-label{font-size:11px;color:var(--muted);margin-bottom:6px;line-height:1.3}
.rate-val{font-size:26px;font-weight:700;margin-bottom:2px}
.rate-count{font-size:10px;color:var(--sub)}
.badge{display:inline-block;background:#0d2d22;color:var(--green);border:1px solid #1a5c3a;
       font-size:10px;padding:2px 8px;border-radius:20px;margin-top:4px}
/* Tabla de segmentos */
.seg-wrap{overflow-x:auto;margin-top:0}
.seg-table{width:100%;border-collapse:collapse;font-size:12px;min-width:420px}
.seg-table th{font-size:10px;font-weight:600;color:var(--sub);text-transform:uppercase;
              letter-spacing:.5px;padding:6px 8px;text-align:left;
              border-bottom:1px solid var(--border);white-space:nowrap}
.seg-table th:not(:first-child){text-align:right}
.seg-table td{padding:9px 8px;border-bottom:1px solid #1a1e30;vertical-align:middle}
.seg-table td:not(:first-child){text-align:right;white-space:nowrap}
.seg-table tr:last-child td{border-bottom:none}
.seg-table tr:hover td{background:#1e2235}
.seg-name-cell{font-weight:600;font-size:12px;color:var(--text);line-height:1.3}
.seg-date-cell{font-size:10px;color:var(--sub);margin-top:2px}
.rate-hot{color:var(--red)}
.rate-warm{color:var(--amber)}
.rate-cool{color:var(--green)}
.rate-cold{color:var(--blue)}
/* Calculadora */
.calc-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
           padding:18px 16px;margin-top:12px}
.slider-block{margin-bottom:18px}
.slider-top{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.slider-name{font-size:13px;color:var(--muted)}
.slider-val{font-size:18px;font-weight:600}
input[type=range]{-webkit-appearance:none;width:100%;height:6px;background:var(--border);
                  border-radius:3px;outline:none;cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:26px;height:26px;
  border-radius:50%;background:var(--blue);border:3px solid var(--bg);cursor:pointer}
input[type=range]::-moz-range-thumb{width:26px;height:26px;border-radius:50%;
  background:var(--blue);border:3px solid var(--bg);cursor:pointer}
.mode-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin:18px 0 16px}
.mode-btn{padding:11px 4px;border-radius:10px;border:1.5px solid var(--border);
          background:transparent;color:var(--muted);font-size:13px;font-weight:500;
          cursor:pointer;transition:.15s;font-family:var(--font)}
.mode-btn.active{background:var(--blue);color:#fff;border-color:var(--blue)}
.results-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.result-card{background:var(--card2);border-radius:12px;padding:14px 8px;text-align:center}
.result-label{font-size:10px;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px}
.result-val{font-size:22px;font-weight:700}
.result-val.green{color:var(--green)}
.ref-note{font-size:11px;color:var(--sub);text-align:center;margin-top:12px;min-height:16px;line-height:1.4}
.footer{text-align:center;font-size:11px;color:#3d4466;margin-top:32px}
.footer a{color:#5c6589;text-decoration:none}
</style>
</head>
<body>

<div class="header">
  {% if athlete.profile %}
  <img class="avatar" src="{{ athlete.profile }}" alt="{{ athlete.firstname }}">
  {% else %}
  <div class="avatar-fallback">{{ athlete.firstname[0] }}</div>
  {% endif %}
  <div class="header-text">
    <h1>{{ athlete.firstname }} {{ athlete.lastname }}</h1>
    <p>Calculadora personalizada con tus tasas reales</p>
  </div>
</div>

<div class="section">
  <div class="section-title">Actividades analizadas</div>
  <div class="stat-row">
    <div class="stat-card">
      <div class="stat-val">{{ stats.sessions }}</div>
      <div class="stat-label">Sesiones</div>
    </div>
    <div class="stat-card">
      <div class="stat-val">{{ stats.km }} km</div>
      <div class="stat-label">Total</div>
    </div>
    <div class="stat-card">
      <div class="stat-val">{{ (stats.dplus / 1000) | round(1) }}K m</div>
      <div class="stat-label">D+ total</div>
    </div>
  </div>

  <div class="section-title">Tus tasas reales de ascenso</div>
  <div class="rate-grid">
    <div class="rate-card">
      <div class="rate-label">Subidas cortas<br>&lt;2 km</div>
      <div class="rate-val" style="color:var(--red)">{{ rates.s }}</div>
      <div class="rate-count">m/h &middot; {{ rates.n_s }} sesiones</div>
      {% if rates.n_s < 3 %}<div class="badge">pocos datos</div>{% endif %}
    </div>
    <div class="rate-card">
      <div class="rate-label">Subidas medias<br>2-5 km</div>
      <div class="rate-val" style="color:var(--amber)">{{ rates.m }}</div>
      <div class="rate-count">m/h &middot; {{ rates.n_m }} sesiones</div>
      {% if rates.n_m < 3 %}<div class="badge">pocos datos</div>{% endif %}
    </div>
    <div class="rate-card">
      <div class="rate-label">Subidas largas<br>5-8 km</div>
      <div class="rate-val" style="color:var(--green)">{{ rates.l }}</div>
      <div class="rate-count">m/h &middot; {{ rates.n_l }} sesiones</div>
      {% if rates.n_l < 3 %}<div class="badge">pocos datos</div>{% endif %}
    </div>
    <div class="rate-card">
      <div class="rate-label">Muy largas<br>8+ km</div>
      <div class="rate-val" style="color:var(--blue)">{{ rates.xl }}</div>
      <div class="rate-count">m/h &middot; {{ rates.n_xl }} sesiones</div>
      {% if rates.n_xl < 3 %}<div class="badge">pocos datos</div>{% endif %}
    </div>
  </div>

  {% if segments %}
  <div class="section-title">Tus segmentos de referencia (ritmo carrera)</div>
  <div class="seg-wrap">
  <table class="seg-table">
    <thead>
      <tr>
        <th>Segmento</th>
        <th>Dist.</th>
        <th>D+</th>
        <th>Pend.</th>
        <th>Tiempo</th>
        <th>D+/hora</th>
      </tr>
    </thead>
    <tbody>
    {% for seg in segments %}
    <tr>
      <td>
        <div class="seg-name-cell">{{ seg.name }}</div>
        <div class="seg-date-cell">{{ seg.date }}</div>
      </td>
      <td>{{ seg.dist_km }} km</td>
      <td>+{{ seg.dplus }} m</td>
      <td>{{ seg.slope }}%</td>
      <td><strong>{{ seg.time_fmt }}</strong></td>
      <td><strong class="
        {%- if seg.rate >= 900 %}rate-hot
        {%- elif seg.rate >= 750 %}rate-warm
        {%- elif seg.rate >= 600 %}rate-cool
        {%- else %}rate-cold
        {%- endif %}">{{ seg.rate }} m/h</strong></td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
  {% endif %}

  <div class="section-title">Calculadora</div>
  <div class="calc-card">
    <div class="slider-block">
      <div class="slider-top">
        <span class="slider-name">Desnivel positivo</span>
        <span class="slider-val" id="dplus-out">900 m</span>
      </div>
      <input type="range" id="dplus" min="50" max="3000" step="50" value="900">
    </div>
    <div class="slider-block" style="margin-bottom:0">
      <div class="slider-top">
        <span class="slider-name">Distancia horizontal</span>
        <span class="slider-val" id="dist-out">5.0 km</span>
      </div>
      <input type="range" id="dist" min="0.5" max="20" step="0.5" value="5">
    </div>
    <div class="mode-row">
      <button class="mode-btn active" onclick="setMode('race',this)">Carrera</button>
      <button class="mode-btn" onclick="setMode('train',this)">Entrena.</button>
      <button class="mode-btn" onclick="setMode('easy',this)">Suave/Z2</button>
    </div>
    <div class="results-grid">
      <div class="result-card">
        <div class="result-label">Pendiente</div>
        <div class="result-val" id="grade-out">18%</div>
      </div>
      <div class="result-card">
        <div class="result-label">D+/hora</div>
        <div class="result-val" id="rate-out">760 m/h</div>
      </div>
      <div class="result-card">
        <div class="result-label">Tiempo est.</div>
        <div class="result-val green" id="time-out">1h 11m</div>
      </div>
    </div>
    <div class="ref-note" id="ref-note"></div>
  </div>

  <div class="footer">
    <a href="/">&#x2190; Nueva consulta</a> &nbsp;&middot;&nbsp;
    Trail Calculator &middot; Solo lectura de Strava
  </div>
</div>

<script>
const RATES = { s:{{ rates.s }}, m:{{ rates.m }}, l:{{ rates.l }}, xl:{{ rates.xl }} };
const MULT  = { race:1.0, train:0.82, easy:0.67 };
let mode = 'race';
function cat(d){ return d<2?'s':d<5?'m':d<8?'l':'xl'; }
function fmt(mins){
  const h=Math.floor(mins/60), m=Math.round(mins%60);
  return h>0 ? h+'h '+m+'m' : m+'m';
}
function setMode(m,btn){
  mode=m;
  document.querySelectorAll('.mode-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  update();
}
function update(){
  const dp   = parseInt(document.getElementById('dplus').value);
  const dist = parseFloat(document.getElementById('dist').value);
  document.getElementById('dplus-out').textContent = dp+' m';
  document.getElementById('dist-out').textContent  = dist.toFixed(1)+' km';
  const grade = Math.round(dp / (dist * 10));
  const rate  = Math.round(RATES[cat(dist)]*MULT[mode]);
  const tMins = (dp/rate)*60;
  document.getElementById('grade-out').textContent = grade+'%';
  document.getElementById('rate-out').textContent  = rate+' m/h';
  document.getElementById('time-out').textContent  = fmt(tMins);
  const modeLabel = {race:'en carrera',train:'en entrenamiento',easy:'en Z2/suave'}[mode];
  document.getElementById('ref-note').textContent =
    dp+'m de D+ en '+dist+'km '+modeLabel+' -> '+fmt(tMins);
}
document.getElementById('dplus').addEventListener('input',update);
document.getElementById('dist').addEventListener('input',update);
update();
</script>
</body>
</html>"""

# -- Routes ------------------------------------------------------------

@app.route('/')
def index():
    error = request.args.get('error')
    auth_url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={STRAVA_CLIENT_ID}"
        f"&redirect_uri={BASE_URL}/auth/callback"
        f"&response_type=code"
        f"&scope=activity:read_all"
        f"&approval_prompt=auto"
    )
    return render_template_string(INDEX, auth_url=auth_url, error=error)

@app.route('/auth/callback')
def auth_callback():
    code  = request.args.get('code')
    error = request.args.get('error')
    if error or not code:
        return redirect('/?error=Acceso+denegado+o+cancelado')

    resp = requests.post('https://www.strava.com/oauth/token', data={
        'client_id':     STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'code':          code,
        'grant_type':    'authorization_code',
    }, timeout=15)

    if resp.status_code != 200:
        import sys
        print(f"[ERROR] token exchange: {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        try:
            err_detail = resp.json().get('message', resp.text[:200])
        except Exception:
            err_detail = resp.text[:200]
        return render_template_string(ERROR_PAGE, msg=f'Error Strava ({resp.status_code}): {err_detail}')

    data    = resp.json()
    token   = data.get('access_token')
    athlete = data.get('athlete', {})

    activities = fetch_activities(token)
    _, stats = analyze(activities)
    all_segs = fetch_segments(token, activities)
    rates = analyze_from_segments(all_segs)
    segments = all_segs[:12]

    session['athlete'] = {
        'firstname': athlete.get('firstname', 'Atleta'),
        'lastname':  athlete.get('lastname', ''),
        'profile':   athlete.get('profile_medium') or athlete.get('profile', ''),
    }
    session['rates']    = rates
    session['stats']    = stats
    session['segments'] = segments

    return redirect('/resultado')

@app.route('/resultado')
def resultado():
    if 'rates' not in session:
        return redirect('/')
    return render_template_string(
        RESULT,
        athlete=session['athlete'],
        rates=session['rates'],
        stats=session['stats'],
        segments=session.get('segments', []),
    )

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
