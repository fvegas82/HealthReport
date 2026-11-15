#!/usr/bin/env python3
"""
Health Report Generator - Informe integrado de 14 días
Genera HTML con glucosa, actividad, composición corporal e insulina.
"""

import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import psycopg

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

TZ = ZoneInfo(os.getenv("TZ", "Europe/Madrid"))
# The reporter and portal share the backup volume.  Keeping the report below
# this directory makes the public route deterministic and avoids mixing report
# assets with database backups.
OUT = Path("/reports/healthhub/reports/semanal")
OUT.mkdir(parents=True, exist_ok=True)
DSN = os.environ["DATABASE_URL"]
NIGHT = os.getenv("NIGHTSCOUT_URL", "http://nightscout:1337").rstrip("/")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

# ============================================================================
# FUNCIONES DE DATOS
# ============================================================================

def glucose_from_nightscout(start, end):
    """Obtener lecturas de glucosa de Nightscout en rango de fechas."""
    try:
        token = hashlib.sha1(os.environ["NIGHTSCOUT_API_SECRET"].encode()).hexdigest()
        url = (f"{NIGHT}/api/v1/entries.json?count=100000&"
               f"find[date][$gte]={int(start.timestamp()*1000)}&"
               f"find[date][$lte]={int(end.timestamp()*1000)}")
        req = urllib.request.Request(url, headers={"api-secret": token})
        with urllib.request.urlopen(req, timeout=60) as r:
            rows = json.load(r)
        data = [(datetime.fromtimestamp(x["date"]/1000, TZ), float(x["sgv"]))
                for x in rows if x.get("sgv")]
        return sorted(data, key=lambda x: x[0])
    except Exception as e:
        log.error(f"Error obteniendo glucosa de Nightscout: {e}")
        return []

def health_events_from_db(start):
    """Obtener eventos de salud de PostgreSQL desde fecha."""
    try:
        with psycopg.connect(DSN) as db:
            cursor = db.execute(
                "SELECT event_type, occurred_at, payload FROM health_events "
                "WHERE occurred_at >= %s ORDER BY occurred_at ASC",
                (start,)
            )
            return cursor.fetchall()
    except Exception as e:
        log.error(f"Error obteniendo eventos de base de datos: {e}")
        return []

def renpho_composition(nas_path="/backups/renpho"):
    """Leer última composición corporal RENPHO desde JSON."""
    try:
        renpho_file = Path(nas_path) / "ultimo.json"
        if renpho_file.exists():
            with open(renpho_file, "r") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"No se pudo leer RENPHO: {e}")
    return None

# ============================================================================
# CÁLCULOS GLUCÉMICOS
# ============================================================================

def calculate_glucose_metrics(values):
    """Calcular métricas de glucosa."""
    if not values:
        return {
            "mean": 0, "std": 0, "cv": 0, "tir": 0,
            "hypo": 0, "hyper": 0, "count": 0
        }
    
    gs = np.array([v for _, v in values])
    mean = float(np.mean(gs))
    std = float(np.std(gs))
    cv = (std / mean * 100) if mean > 0 else 0
    tir = float(np.mean((gs >= 70) & (gs <= 180)) * 100)
    hypo = float(np.mean(gs < 70) * 100)
    hyper = float(np.mean(gs > 180) * 100)
    
    return {
        "mean": mean,
        "std": std,
        "cv": cv,
        "tir": tir,
        "hypo": hypo,
        "hyper": hyper,
        "count": len(values)
    }

def agp_data(values):
    """Calcular percentiles AGP (5, 25, 50, 75, 95)."""
    if not values:
        return None, None
    
    hours = [[] for _ in range(96)]
    for t, v in values:
        idx = (t.hour * 60 + t.minute) // 15
        hours[idx].append(v)
    
    percentiles = np.array([
        [np.percentile(x, q) if x else np.nan for x in hours]
        for q in (5, 25, 50, 75, 95)
    ])
    x = np.arange(96) / 4
    
    return percentiles, x

# ============================================================================
# GRÁFICOS
# ============================================================================

def plot_agp(values, output_path):
    """Generar gráfico AGP mejorado."""
    percentiles, x = agp_data(values)
    if percentiles is None:
        return False
    
    try:
        fig, ax = plt.subplots(figsize=(14, 5), dpi=100)
        ax.axhspan(70, 180, color="#e7f5eb", alpha=0.7, label="Rango seguro (70-180)")
        ax.fill_between(x, percentiles[0], percentiles[4], alpha=0.20, color="#3b82f6", label="P5-P95")
        ax.fill_between(x, percentiles[1], percentiles[3], alpha=0.35, color="#3b82f6", label="P25-P75")
        ax.plot(x, percentiles[2], lw=2.5, color="#1e40af", label="Mediana (P50)")
        
        ax.set(xlim=(0, 24), ylim=(40, 300), xlabel="Hora del día", 
               ylabel="mg/dL", title="AGP - Últimos 14 días")
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        ax.grid(alpha=0.2)
        ax.legend(loc="upper right", fontsize=9)
        
        fig.tight_layout()
        fig.savefig(output_path, dpi=100, bbox_inches='tight')
        plt.close(fig)
        return True
    except Exception as e:
        log.error(f"Error generando AGP: {e}")
        return False

def plot_daily_profiles(values, events, output_dir):
    """Generar gráficos de perfiles diarios (últimos 14 días)."""
    if not values:
        return []
    
    # Agrupar por día
    by_day = {}
    for t, v in values:
        day = t.astimezone(TZ).date()
        if day not in by_day:
            by_day[day] = []
        by_day[day].append((t, v))
    
    # Agrupar eventos por día
    events_by_day = {}
    for event_type, when, payload in events:
        day = when.astimezone(TZ).date()
        if day not in events_by_day:
            events_by_day[day] = []
        events_by_day[day].append((event_type, when, payload))
    
    saved = []
    # Newest first, for the whole reporting period.
    for day in sorted(by_day.keys(), reverse=True)[:14]:
        try:
            fig, ax = plt.subplots(figsize=(12, 4), dpi=100)
            
            # Datos del día
            times = [t for t, _ in by_day[day]]
            glu = [v for _, v in by_day[day]]
            
            ax.axhspan(70, 180, color="#e7f5eb", alpha=0.7)
            ax.plot(times, glu, marker='o', lw=1.5, markersize=4, color="#1e40af")
            ax.fill_between(times, 40, glu, alpha=0.15, color="#3b82f6")
            
            # Marcar insulina (rojo) y entrenamientos (violeta)
            if day in events_by_day:
                for evt_type, when, payload in events_by_day[day]:
                    if evt_type == "InsulinDoseRecord":
                        ax.axvline(when, color="#dc2626", linestyle="--", alpha=0.6, linewidth=1)
                    elif evt_type == "ExerciseSessionRecord":
                        ax.axvline(when, color="#9333ea", linestyle="--", alpha=0.6, linewidth=1)
            
            ax.set(xlim=(datetime.combine(day, datetime.min.time(), tzinfo=TZ),
                         datetime.combine(day, datetime.max.time(), tzinfo=TZ)),
                   ylim=(40, 300), ylabel="mg/dL",
                   title=f"Perfil diario - {day.strftime('%d/%m/%Y')}")
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.grid(alpha=0.2)
            
            filename = f"daily-{day.isoformat()}.png"
            filepath = output_dir / filename
            fig.tight_layout()
            fig.savefig(filepath, dpi=100, bbox_inches='tight')
            plt.close(fig)
            saved.append(filename)
        except Exception as e:
            log.error(f"Error generando perfil del día {day}: {e}")
    
    return saved

# ============================================================================
# PROCESAMIENTO DE EVENTOS
# ============================================================================

def aggregate_events(events):
    """Agregar eventos por tipo y día."""
    aggregated = {
        "by_day": {},
        "by_type": {}
    }
    
    for event_type, when, payload in events:
        day = when.astimezone(TZ).date()
        
        if day not in aggregated["by_day"]:
            aggregated["by_day"][day] = {
                "steps": 0, "distance": 0, "kcal": 0,
                "insulin": [], "workouts": []
            }
        
        if event_type not in aggregated["by_type"]:
            aggregated["by_type"][event_type] = []
        
        if event_type == "StepsRecord":
            aggregated["by_day"][day]["steps"] += int(payload.get("count", 0))
        elif event_type == "DistanceRecord":
            aggregated["by_day"][day]["distance"] += float(payload.get("meters", 0)) / 1000
        elif event_type in ("TotalCaloriesBurnedRecord", "ActiveCaloriesBurnedRecord"):
            aggregated["by_day"][day]["kcal"] += float(payload.get("kcal", 0))
        elif event_type == "InsulinDoseRecord":
            aggregated["by_day"][day]["insulin"].append({
                "units": payload.get("units", 0),
                "type": payload.get("type", "?"),
                "time": when.strftime("%H:%M")
            })
        elif event_type == "ExerciseSessionRecord":
            aggregated["by_day"][day]["workouts"].append({
                "minutes": payload.get("duration_minutes", 0),
                "intensity": payload.get("intensity", "media"),
                "time": when.strftime("%H:%M")
            })
        
        aggregated["by_type"][event_type].append((when, payload))
    
    return aggregated

# ============================================================================
# GENERACIÓN HTML
# ============================================================================

def generate_html_report(end, start, metrics, daily_profiles, aggregated, renpho):
    """Generar HTML del informe completo."""
    
    # Preparar tabla de actividad
    activity_rows = ""
    for day in sorted(aggregated["by_day"].keys(), reverse=True):
        data = aggregated["by_day"][day]
        insulin_str = " | ".join([f"{i['units']}U {i['type']}" for i in data["insulin"]]) or "-"
        workout_str = " | ".join([f"{w['minutes']}min ({w['intensity']})" for w in data["workouts"]]) or "-"
        activity_rows += f"""
        <tr>
            <td><strong>{day.strftime('%d/%m/%Y')}</strong></td>
            <td>{data['steps']:,}</td>
            <td>{data['distance']:.1f}</td>
            <td>{data['kcal']:.0f}</td>
            <td><small>{insulin_str}</small></td>
            <td><small>{workout_str}</small></td>
        </tr>
        """
    
    # Preparar sección RENPHO
    renpho_html = ""
    if renpho:
        renpho_html = f"""
        <section class="composition">
            <h2>⚖️ Composición Corporal (RENPHO)</h2>
            <div class="cards">
                <div class="card">
                    <b>Peso</b>
                    <h3>{renpho.get('weight', '?'):.1f} kg</h3>
                </div>
                <div class="card">
                    <b>Grasa Corporal</b>
                    <h3>{renpho.get('fat_percentage', '?'):.1f}%</h3>
                </div>
                <div class="card">
                    <b>Masa Muscular</b>
                    <h3>{renpho.get('muscle_mass', '?'):.1f} kg</h3>
                </div>
                <div class="card">
                    <b>Agua Corporal</b>
                    <h3>{renpho.get('water_percentage', '?'):.1f}%</h3>
                </div>
            </div>
        </section>
        """
    
    # Preparar imágenes de perfiles diarios
    profiles_html = ""
    if daily_profiles:
        profiles_html = "<section class='daily-profiles'><h2>📊 Perfiles Diarios</h2>"
        for img in daily_profiles:
            profiles_html += f'<figure><img src="{img}" alt="Perfil diario"></figure>'
        profiles_html += "</section>"
    
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Health Report - Informe Integrado</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f9fafb;
            color: #1f2937;
            line-height: 1.6;
        }}
        main {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem 1rem;
        }}
        header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 2rem;
            border-radius: 12px;
            margin-bottom: 2rem;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }}
        header h1 {{ font-size: 2rem; margin-bottom: 0.5rem; }}
        header p {{ opacity: 0.9; font-size: 0.95rem; }}
        
        section {{ margin-bottom: 2rem; }}
        section h2 {{
            font-size: 1.5rem;
            margin-bottom: 1rem;
            border-bottom: 2px solid #e5e7eb;
            padding-bottom: 0.5rem;
        }}
        
        .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 1.5rem;
        }}
        .card {{
            background: white;
            border-radius: 12px;
            padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border-left: 4px solid #667eea;
        }}
        .card b {{ display: block; font-size: 0.85rem; color: #6b7280; margin-bottom: 0.5rem; }}
        .card h2, .card h3 {{ font-size: 2rem; color: #667eea; }}
        
        img {{ max-width: 100%; height: auto; border-radius: 8px; }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        th {{
            background: #f3f4f6;
            padding: 1rem;
            text-align: left;
            font-weight: 600;
            color: #374151;
            border-bottom: 2px solid #e5e7eb;
        }}
        td {{
            padding: 0.75rem 1rem;
            border-bottom: 1px solid #f3f4f6;
        }}
        tr:hover {{ background: #fafafa; }}
        
        .daily-profiles {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
        }}
        .daily-profiles figure {{
            background: white;
            padding: 1rem;
            border-radius: 8px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        
        .alert {{
            background: #fef2f2;
            border-left: 4px solid #dc2626;
            padding: 1rem;
            border-radius: 6px;
            margin-bottom: 1rem;
            font-size: 0.9rem;
        }}
        
        footer {{
            text-align: center;
            color: #9ca3af;
            font-size: 0.85rem;
            margin-top: 3rem;
            padding-top: 2rem;
            border-top: 1px solid #e5e7eb;
        }}
        
        @media (max-width: 768px) {{
            .cards {{ grid-template-columns: 1fr; }}
            main {{ padding: 1rem 0.5rem; }}
            header {{ padding: 1.5rem; }}
            header h1 {{ font-size: 1.5rem; }}
            th, td {{ padding: 0.5rem; font-size: 0.9rem; }}
        }}
    </style>
</head>
<body>
<main>
    <header>
        <h1>📊 Informe Integrado de 14 días</h1>
        <p>Generado {end.strftime('%d/%m/%Y %H:%M')} {TZ.key}</p>
        <p><small>Datos descriptivos; no constituyen recomendación clínica.</small></p>
    </header>
    
    <section class="glucose-summary">
        <h2>📈 Resumen Glucémico</h2>
        <div class="cards">
            <div class="card">
                <b>Media</b>
                <h2>{metrics['mean']:.0f} <small>mg/dL</small></h2>
            </div>
            <div class="card">
                <b>Desv. Est.</b>
                <h2>{metrics['std']:.0f} <small>mg/dL</small></h2>
            </div>
            <div class="card">
                <b>Variabilidad (CV)</b>
                <h2>{metrics['cv']:.1f}%</h2>
            </div>
            <div class="card">
                <b>Tiempo en Rango (70-180)</b>
                <h2>{metrics['tir']:.1f}%</h2>
            </div>
            <div class="card">
                <b>Hipoglucemias (&lt;70)</b>
                <h2>{metrics['hypo']:.1f}%</h2>
                <small style="color: #dc2626;">Bajo</small>
            </div>
            <div class="card">
                <b>Hiperglucemias (&gt;180)</b>
                <h2>{metrics['hyper']:.1f}%</h2>
                <small style="color: #ea580c;">Alto</small>
            </div>
        </div>
    </section>
    
    <section class="agp-section">
        <h2>📉 AGP - Arbol de Glicemia Promedio</h2>
        <img src="agp.png" alt="Gráfico AGP de glucosa">
        <p><small>Percentiles de glucosa por hora (últimos 14 días). P5-P95 (claro), P25-P75 (oscuro), P50 (línea central).</small></p>
    </section>
    
    {profiles_html}
    
    {renpho_html}
    
    <section class="activity-section">
        <h2>🏃 Actividad Diaria</h2>
        <table>
            <tr>
                <th>Día</th>
                <th>Pasos</th>
                <th>Distancia (km)</th>
                <th>Energía (kcal)</th>
                <th>Insulina</th>
                <th>Entrenamientos</th>
            </tr>
            {activity_rows}
        </table>
    </section>
    
    <section class="quality-section">
        <h2>✅ Calidad de Datos</h2>
        <div class="cards">
            <div class="card">
                <b>Lecturas de glucosa</b>
                <h3>{metrics['count']}</h3>
            </div>
            <div class="card">
                <b>Eventos registrados</b>
                <h3>{sum(len(v) for v in aggregated['by_type'].values())}</h3>
            </div>
            <div class="card">
                <b>Período cubierto</b>
                <h3>14 días</h3>
            </div>
        </div>
    </section>
    
    <footer>
        <p>Health Report © 2026 - Infraestructura privada de salud</p>
    </footer>
</main>
</body>
</html>
"""
    return html

# ============================================================================
# MAIN - LOOP DE GENERACIÓN
# ============================================================================

def write_report():
    """Generar informe completo."""
    log.info("Iniciando generación de informe...")
    
    end = datetime.now(TZ)
    start = end - timedelta(days=14)
    
    # Obtener datos
    log.info("Obteniendo datos de Nightscout...")
    glucose_data = glucose_from_nightscout(start, end)
    
    log.info("Obteniendo eventos de base de datos...")
    events = health_events_from_db(start)
    
    log.info("Leyendo composición RENPHO...")
    renpho = renpho_composition()
    
    # Calcular métricas
    log.info("Calculando métricas glucémicas...")
    metrics = calculate_glucose_metrics(glucose_data)
    
    # Generar gráficos
    log.info("Generando gráfico AGP...")
    plot_agp(glucose_data, OUT / "agp.png")
    
    log.info("Generando perfiles diarios...")
    daily_profiles = plot_daily_profiles(glucose_data, events, OUT)
    
    # Procesar eventos
    log.info("Procesando eventos...")
    aggregated = aggregate_events(events)
    
    # Generar HTML
    log.info("Generando HTML...")
    html = generate_html_report(end, start, metrics, daily_profiles, aggregated, renpho)
    
    # Guardar informe semanal
    week_file = OUT / "healthhub-semanal.html"
    week_file.write_text(html, encoding="utf-8")
    log.info(f"Informe semanal guardado: {week_file}")
    
    # Guardar informe histórico
    week_num = end.isocalendar()[1]
    year = end.year
    timestamped_file = OUT / f"healthhub-semanal-{year}-W{week_num:02d}.html"
    timestamped_file.write_text(html, encoding="utf-8")
    log.info(f"Informe histórico guardado: {timestamped_file}")
    
    # Crear último enlace
    latest_file = OUT / "ultimo.html"
    latest_file.write_text(html, encoding="utf-8")
    log.info("✅ Informe generado exitosamente")

def main():
    """Loop principal de generación."""
    log.info("Health Report Generator iniciado. Generando cada 5 minutos...")
    
    while True:
        try:
            write_report()
        except Exception as error:
            log.error(f"Error generando informe: {error}", exc_info=True)
        
        time.sleep(300)  # Cada 5 minutos

if __name__ == "__main__":
    main()
