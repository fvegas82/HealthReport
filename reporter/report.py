#!/usr/bin/env python3
"""
Health Report Generator - Informe integrado de 14 días
Genera HTML con glucosa, actividad, composición corporal e insulina.
"""

import hashlib
import json
import os
import shutil
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
REFRESH_REQUEST = OUT.parent / ".refresh-request"
DSN = os.environ["DATABASE_URL"]
NIGHT = os.getenv("NIGHTSCOUT_URL", "http://nightscout:1337").rstrip("/")
LEGACY_REPORTS = Path("/glucose-reports")
RENPHO_AUDIT_DIR = Path("/reports/healthhub/reports/renpho")

# Resultado validado de la lectura OCR histórica que se conservó como imagen en
# el NAS. Se usa una única vez para reconstruir el fichero auditable
# ``ultimo.json`` si este se hubiera perdido al restaurar los volúmenes. Las
# lecturas posteriores siempre proceden del proceso OCR local del Mac mini.
RECOVERED_RENPHO_OCR = {
    "1794678957973_100.JPG": {
        "captured_at": "2026-11-14T10:29:45+01:00",
        "weight": 116.30,
        "bmi": 32.9,
        "fat_percentage": 20.0,
        "fat_mass": 23.26,
        "visceral_fat": 15,
        "muscle_percentage": 76.0,
        "muscle_mass": 88.40,
        "skeletal_muscle_percentage": 51.7,
        "skeletal_muscle_mass": 60.13,
        "bone_percentage": 4.0,
        "bone_mass": 4.65,
        "fat_free_mass": 93.00,
        "metabolic_age": 49,
        "bmr": 2379,
        "water_percentage": 57.7,
        "water_mass": 67.11,
        "protein_percentage": 18.2,
        "protein_mass": 21.17,
    }
}

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
        # Nightscout expects the SHA-1 representation in the api-secret header.
        # The actual issue was an unset environment variable in the deployed stack.
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

def _latest_renpho_image():
    """Devuelve la exportación RENPHO más reciente, solo como fuente OCR."""
    renpho_dir = LEGACY_REPORTS / "renpho"
    if not renpho_dir.exists():
        return None
    images = sorted(
        (p for p in renpho_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return images[0] if images else None


def normalize_renpho_reading(reading):
    """Compatibilidad con el JSON generado por el OCR original del Mac mini."""
    normalized = dict(reading)
    metrics = reading.get("metrics", {})
    aliases = {
        "weight": ("weight_kg",),
        "fat_percentage": ("body_fat_pct", "fat_pct"),
        "fat_mass": ("body_fat_mass_kg", "fat_mass_kg"),
        "visceral_fat": ("visceral_fat_index",),
        "muscle_percentage": ("muscle_pct",),
        "muscle_mass": ("muscle_mass_kg",),
        "water_percentage": ("body_water_pct", "water_pct"),
        "water_mass": ("body_water_mass_kg", "water_mass_kg"),
        "protein_percentage": ("protein_pct",),
        "protein_mass": ("protein_mass_kg",),
        "bone_mass": ("bone_mass_kg",),
        "fat_free_mass": ("fat_free_mass_kg",),
        "bmr": ("bmr_kcal",),
        "metabolic_age": ("metabolic_age_years",),
        "captured_at": ("measured_at", "measurement_at", "created_at"),
    }
    for canonical, legacy_keys in aliases.items():
        if normalized.get(canonical) is None:
            if metrics.get(canonical) is not None:
                normalized[canonical] = metrics[canonical]
                continue
            for legacy_key in legacy_keys:
                value = reading.get(legacy_key, metrics.get(legacy_key))
                if value is not None:
                    normalized[canonical] = value
                    break
    if normalized.get("captured_at") is None:
        normalized["captured_at"] = reading.get("processed_at")
    return normalized


def renpho_composition():
    """Leer la última composición estructurada generada por el OCR local.

    El HTML nunca incrusta la exportación JPG. La imagen permanece en el NAS
    como original de auditoría y el proceso OCR guarda los valores en JSON/TXT.
    """
    try:
        renpho_file = RENPHO_AUDIT_DIR / "ultimo.json"
        if renpho_file.exists():
            with open(renpho_file, "r", encoding="utf-8") as f:
                return normalize_renpho_reading(json.load(f))
    except Exception as e:
        log.warning(f"No se pudo leer RENPHO: {e}")

    # Recuperación segura de la única lectura ya revisada con OCR durante la
    # migración. Esto reconstruye los mismos artefactos que produce el proceso
    # local, sin enviar la imagen a ningún servicio externo.
    image = _latest_renpho_image()
    if image and image.name in RECOVERED_RENPHO_OCR:
        recovered = {
            **RECOVERED_RENPHO_OCR[image.name],
            "source_filename": image.name,
            "source": "ocr-local-recovered",
        }
        try:
            RENPHO_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
            (RENPHO_AUDIT_DIR / "ultimo.json").write_text(
                json.dumps(recovered, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (RENPHO_AUDIT_DIR / "ultimo.txt").write_text(
                "Resultado OCR local recuperado de " + image.name + "\n" +
                json.dumps(recovered, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return recovered
        except Exception as e:
            log.warning(f"No se pudo recuperar OCR RENPHO: {e}")
    return None

def preserve_legacy_assets():
    """Conservar únicamente la comparativa histórica de 14 días.

    Los perfiles diarios se vuelven a generar en cada informe porque son el
    soporte donde se dibujan las dosis de insulina y los entrenamientos.
    """
    assets = {}
    for key, filename in {
        "comparison": "healthhub-compare-14d.png",
    }.items():
        source = LEGACY_REPORTS / filename
        if source.exists():
            target = OUT / f"legacy-{filename}"
            shutil.copy2(source, target)
            assets[key] = target.name

    return assets

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
            
            # Marcar insulina (rojo) y entrenamientos (violeta). Además de
            # la línea vertical se muestra una etiqueta, para que el evento
            # siga siendo identificable incluso en pantallas pequeñas.
            if day in events_by_day:
                for evt_type, when, payload in events_by_day[day]:
                    if evt_type == "InsulinDoseRecord":
                        units = payload.get("units", "?")
                        ax.axvline(when, color="#dc2626", linestyle="--", alpha=0.9, linewidth=1.8)
                        ax.annotate(
                            f"Insulina {units}U", xy=(when, 290), xytext=(0, -4),
                            textcoords="offset points", rotation=90, va="top", ha="right",
                            fontsize=8, color="#991b1b", fontweight="bold",
                            bbox={"boxstyle": "round,pad=0.2", "fc": "#fee2e2", "ec": "none", "alpha": 0.9},
                        )
                    elif evt_type == "ExerciseSessionRecord":
                        minutes = payload.get("duration_minutes", "?")
                        ax.axvline(when, color="#9333ea", linestyle="--", alpha=0.9, linewidth=1.8)
                        ax.annotate(
                            f"Ejercicio {minutes} min", xy=(when, 285), xytext=(0, -4),
                            textcoords="offset points", rotation=90, va="top", ha="left",
                            fontsize=8, color="#6b21a8", fontweight="bold",
                            bbox={"boxstyle": "round,pad=0.2", "fc": "#f3e8ff", "ec": "none", "alpha": 0.9},
                        )
            
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

def generate_html_report(end, start, metrics, daily_profiles, aggregated, renpho, legacy_assets):
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
        def renpho_value(key, suffix="", decimals=1):
            value = renpho.get(key)
            if value is None:
                return "-"
            return f"{float(value):.{decimals}f}{suffix}"

        source_date = renpho.get("captured_at", "")
        source_note = f"Lectura OCR local: {source_date[:10]}." if source_date else "Lectura OCR local auditada."
        renpho_cards = [
            ("Peso", renpho_value("weight", " kg")),
            ("Grasa corporal", renpho_value("fat_percentage", "%")),
            ("Masa muscular", renpho_value("muscle_mass", " kg")),
            ("Grasa visceral", renpho_value("visceral_fat", "", 0)),
            ("Agua corporal", renpho_value("water_percentage", "%")),
            ("Proteína", renpho_value("protein_percentage", "%")),
            ("Masa libre de grasa", renpho_value("fat_free_mass", " kg")),
            ("BMR", renpho_value("bmr", " kcal", 0)),
            ("Masa ósea", renpho_value("bone_mass", " kg")),
            ("Edad metabólica", renpho_value("metabolic_age", " años", 0)),
        ]
        cards_html = "".join(
            f'<div class="card"><b>{label}</b><h3>{value}</h3></div>'
            for label, value in renpho_cards if value != "-"
        )
        renpho_html = f"""
        <section class="composition">
            <h2>⚖️ Composición corporal (RENPHO)</h2>
            <p>{source_note} Valores extraídos localmente; la imagen original no se muestra en el informe.</p>
            <div class="cards">{cards_html}</div>
        </section>
        """

    legacy_glucose_html = ""
    graph = lambda key, title, note: (
        f'<section><h2>{title}</h2><img src="{legacy_assets[key]}" alt="{title}">'
        f'<p><small>{note}</small></p></section>' if key in legacy_assets else ""
    )
    legacy_glucose_html = "".join([
        graph("comparison", "Comparativa de 14 días", "Una curva por día para comparar patrones horarios."),
    ])
    
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
    
    {legacy_glucose_html}

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
    legacy_assets = preserve_legacy_assets()
    
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
    html = generate_html_report(end, start, metrics, daily_profiles, aggregated, renpho, legacy_assets)
    
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
    REFRESH_REQUEST.unlink(missing_ok=True)
    log.info("✅ Informe generado exitosamente")

def main():
    """Loop principal de generación."""
    log.info("Health Report Generator iniciado. Generando cada 5 minutos o bajo demanda...")
    
    while True:
        try:
            write_report()
        except Exception as error:
            log.error(f"Error generando informe: {error}", exc_info=True)
        
        # El portal escribe este marcador al registrar una dosis o entrenamiento.
        # Así el cambio aparece enseguida sin esperar al ciclo normal de 5 minutos.
        for _ in range(60):
            if REFRESH_REQUEST.exists():
                break
            time.sleep(5)

if __name__ == "__main__":
    main()
