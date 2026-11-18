#!/usr/bin/env python3
"""
Health Report Generator - Informe integrado de 14 días
Genera HTML con glucosa, actividad, composición corporal e insulina.
"""

import csv
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
import matplotlib.ticker as mticker
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
CLARITY_DIR = Path("/reports/healthhub/clarity")
CLARITY_EXPORT = CLARITY_DIR / "clarity-export.csv"

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

def glucose_with_clarity_continuity(nightscout_values, start, end):
    """Completa días sin Nightscout con el histórico privado de Clarity.

    Nightscout tiene prioridad cuando hay cobertura suficiente. Si solo hay
    lecturas aisladas durante la transición, se usa el día completo de
    Clarity: así se evitan perfiles diarios prácticamente vacíos.
    """
    by_day = {}
    for when, value in nightscout_values:
        by_day.setdefault(when.astimezone(TZ).date(), []).append((when, value))
    clarity_by_day = {}
    for when, value in clarity_glucose_from_csv():
        day = when.astimezone(TZ).date()
        if start.date() <= day <= end.date():
            clarity_by_day.setdefault(day, []).append((when, value))
    def has_complete_nightscout_coverage(readings):
        """Una cantidad alta no implica un día completo: se exige continuidad."""
        if len(readings) < 180:
            return False
        ordered = sorted(when for when, _ in readings)
        covered_seconds = (ordered[-1] - ordered[0]).total_seconds()
        return covered_seconds >= 20 * 60 * 60

    for day, clarity_values in clarity_by_day.items():
        nightscout_day = by_day.get(day, [])
        if not has_complete_nightscout_coverage(nightscout_day):
            # Clarity es la referencia histórica durante la transición. No
            # obstante, conserva una lectura de Nightscout cuando Clarity no
            # tiene nada alrededor: así un corte parcial (como la mañana del
            # 13/11) no deja un hueco artificial en el perfil diario.
            clarity_times = [when for when, _ in clarity_values]
            additional = [
                item for item in nightscout_day
                if not any(abs((item[0] - clarity_time).total_seconds()) <= 10 * 60
                               for clarity_time in clarity_times)
            ]
            by_day[day] = sorted(clarity_values + additional, key=lambda item: item[0])
    return sorted((item for values in by_day.values() for item in values), key=lambda item: item[0])

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

def clarity_glucose_from_csv():
    """Carga únicamente las lecturas EGV de la exportación privada de Clarity."""
    if not CLARITY_EXPORT.exists():
        return []
    try:
        readings = []
        with CLARITY_EXPORT.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("Event Type") != "EGV":
                    continue
                timestamp = row.get("Timestamp (YYYY-MM-DDThh:mm:ss)", "")
                glucose = row.get("Glucose Value (mg/dL)", "")
                if not timestamp or not glucose:
                    continue
                when = datetime.fromisoformat(timestamp).replace(tzinfo=TZ)
                readings.append((when, float(glucose)))
        return sorted(readings, key=lambda item: item[0])
    except Exception as e:
        log.error(f"Error leyendo el histórico de Clarity: {e}")
        return []

def write_clarity_history():
    """Genera una página histórica local a partir del CSV privado de Clarity."""
    values = clarity_glucose_from_csv()
    if not values:
        return None

    history_dir = OUT / "historico-clarity"
    history_dir.mkdir(parents=True, exist_ok=True)
    plot_agp(values, history_dir / "agp.png")
    daily_profiles = plot_daily_profiles(values, [], history_dir)
    start_day = values[0][0].astimezone(TZ).date().strftime("%d/%m/%Y")
    end_day = values[-1][0].astimezone(TZ).date().strftime("%d/%m/%Y")
    metrics = calculate_glucose_metrics(values)
    asset_version = int(time.time())
    profile_images = "".join(
        f'<figure><img src="historico-clarity/{name}?v={asset_version}" alt="Perfil diario Clarity"></figure>'
        for name in daily_profiles
    )
    html = f"""<!doctype html><html lang="es"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Histórico Clarity</title><style>
    body{{font-family:system-ui;margin:0;background:#f7fafc;color:#17243a}}main{{max-width:1280px;margin:auto;padding:24px}}
    section{{background:#fff;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 2px 10px #102a4318}}
    .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card{{border:1px solid #d9e2ec;border-radius:10px;padding:12px}}
    img{{max-width:100%;height:auto}}.profiles figure{{margin:18px 0}}a{{color:#075985;font-weight:700}}
    </style><main><a href="healthhub-semanal.html">← Volver al informe actual</a>
    <section><h1>Histórico Clarity</h1><p>{start_day} – {end_day}. Datos previos al cambio a Nightscout; fuente privada, no subida a GitHub.</p>
    <div class="cards"><div class="card"><b>Media</b><h2>{metrics['mean']:.0f} mg/dL</h2></div><div class="card"><b>Variabilidad</b><h2>{metrics['cv']:.1f}%</h2></div><div class="card"><b>En rango</b><h2>{metrics['tir']:.1f}%</h2></div><div class="card"><b>Lecturas</b><h2>{metrics['count']}</h2></div></div></section>
    <section><h2>AGP histórico</h2><img src="historico-clarity/agp.png?v={asset_version}" alt="AGP histórico Clarity"></section>
    <section class="profiles"><h2>Perfiles diarios</h2>{profile_images}</section></main></html>"""
    filename = "clarity-historico.html"
    (OUT / filename).write_text(html, encoding="utf-8")
    return filename

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

def renpho_history():
    """Lee el historico local e incorpora la lectura auditada pre-migracion."""
    history_file = RENPHO_AUDIT_DIR / "history.json"
    readings = []
    try:
        if history_file.exists():
            raw = json.loads(history_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                readings = [normalize_renpho_reading(item) for item in raw if isinstance(item, dict)]
    except Exception as error:
        log.warning("No se pudo leer el historico RENPHO: %s", error)
    latest = renpho_composition()
    if latest and not any(item.get("captured_at") == latest.get("captured_at") for item in readings):
        readings.append(latest)

    # ``ultimo.json`` pasa a contener la ultima carga del portal, por lo que
    # no debe ocultar la medicion RENPHO validada antes de la migracion. La
    # incorporamos como primer punto solo cuando no este ya en el historico.
    for filename, recovered_values in RECOVERED_RENPHO_OCR.items():
        recovered = {
            **recovered_values,
            "source_filename": filename,
            "source": "ocr-local-recovered",
        }
        if not any(item.get("captured_at") == recovered["captured_at"] for item in readings):
            readings.append(recovered)
    return sorted((item for item in readings if item.get("captured_at")), key=lambda item: item["captured_at"])

def plot_renpho_evolution(readings, output_path):
    """Genera el grafico RENPHO desde la primera medicion disponible.

    La primera carga actua como linea base. Con las cargas posteriores el mismo
    grafico se convierte en una evolucion temporal, sin tener que esperar a
    disponer de un segundo informe para que la seccion sea visible.
    """
    dated = []
    for reading in readings:
        try:
            dated.append((datetime.fromisoformat(reading["captured_at"]), reading))
        except (ValueError, TypeError):
            continue
    if not dated:
        output_path.unlink(missing_ok=True)
        return None
    dates = [when for when, _ in dated]
    series = [
        ("Peso (kg)", "weight", "#2563eb"),
        ("Grasa corporal (%)", "fat_percentage", "#ea580c"),
        ("Masa muscular (kg)", "muscle_mass", "#059669"),
    ]
    available = [item for item in series if any(reading.get(item[1]) is not None for _, reading in dated)]
    if not available:
        output_path.unlink(missing_ok=True)
        return None
    fig, axes = plt.subplots(len(available), 1, figsize=(12, 3.2 * len(available)), sharex=True)
    if len(available) == 1:
        axes = [axes]
    for axis, (label, key, color) in zip(axes, available):
        x = [when for when, reading in dated if reading.get(key) is not None]
        y = [float(reading[key]) for _, reading in dated if reading.get(key) is not None]
        axis.plot(x, y, marker="o", linewidth=2.4 if len(x) > 1 else 0, markersize=7, color=color)
        axis.set_ylabel(label, fontweight="bold")
        if len(x) == 1:
            axis.annotate(
                "Linea base",
                (x[0], y[0]),
                xytext=(8, 10),
                textcoords="offset points",
                fontsize=9,
                color="#475569",
            )
        axis.grid(alpha=.22)
        axis.spines[["top", "right"]].set_visible(False)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.suptitle("Evolucion de la composicion corporal", fontsize=16, fontweight="bold")
    fig.tight_layout()
    fig.savefig(output_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return output_path.name

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

def calculate_glucose_metrics(values, period_days=14):
    """Calcula las metricas descriptivas de CGM para el informe.

    Los porcentajes se calculan sobre las lecturas disponibles; la cobertura
    indica por separado cuanto del periodo de 14 dias tiene datos. Asi no se
    confunde una mejora aparente con un periodo incompleto.
    """
    if not values:
        return {
            "mean": 0, "std": 0, "cv": 0, "tir": 0,
            "hypo": 0, "hyper": 0, "very_hypo": 0, "very_hyper": 0,
            "tight_range": 0, "gmi": 0, "count": 0,
            "coverage": 0, "days_with_data": 0,
            "low_events": {"count": 0, "minutes": 0, "longest": 0},
            "time_bands": [],
        }

    gs = np.array([v for _, v in values])
    mean = float(np.mean(gs))
    std = float(np.std(gs))
    cv = (std / mean * 100) if mean > 0 else 0
    tir = float(np.mean((gs >= 70) & (gs <= 180)) * 100)
    hypo = float(np.mean(gs < 70) * 100)
    hyper = float(np.mean(gs > 180) * 100)
    very_hypo = float(np.mean(gs < 54) * 100)
    very_hyper = float(np.mean(gs > 250) * 100)
    tight_range = float(np.mean((gs >= 70) & (gs <= 140)) * 100)

    # Cobertura: una lectura por cada bloque de cinco minutos. El valor no se
    # usa para decisiones clinicas; solo califica la fiabilidad del resumen.
    period_seconds = period_days * 24 * 60 * 60
    slots = {
        int((when.astimezone(TZ).timestamp()) // 300)
        for when, _ in values
    }
    coverage = min(100.0, len(slots) / (period_seconds / 300) * 100)
    # Una ventana movil de 14 x 24 horas puede tocar quince fechas del
    # calendario (un fragmento del primer y ultimo dia). El informe expresa
    # dias completos de la ventana, no fechas del calendario.
    days_with_data = min(period_days, len({when.astimezone(TZ).date() for when, _ in values}))

    def low_episode_summary():
        episodes = []
        started = previous = None
        for when, value in values:
            local = when.astimezone(TZ)
            is_low = value < 70
            contiguous = previous and (local - previous).total_seconds() <= 15 * 60
            if is_low:
                if started is None or not contiguous:
                    if started is not None:
                        episodes.append((started, previous))
                    started = local
                previous = local
            elif started is not None:
                episodes.append((started, previous))
                started = previous = None
        if started is not None:
            episodes.append((started, previous))
        durations = [max(5, round((last - first).total_seconds() / 60) + 5) for first, last in episodes]
        return {
            "count": len(durations),
            "minutes": sum(durations),
            "longest": max(durations, default=0),
        }

    time_bands = []
    for label, first_hour, last_hour in [
        ("Noche (00-06)", 0, 6),
        ("Manana (06-12)", 6, 12),
        ("Tarde (12-18)", 12, 18),
        ("Noche (18-24)", 18, 24),
    ]:
        band = np.array([value for when, value in values if first_hour <= when.astimezone(TZ).hour < last_hour])
        if len(band):
            time_bands.append({
                "label": label,
                "mean": float(np.mean(band)),
                "tir": float(np.mean((band >= 70) & (band <= 180)) * 100),
                "low": float(np.mean(band < 70) * 100),
                "high": float(np.mean(band > 180) * 100),
            })

    return {
        "mean": mean,
        "std": std,
        "cv": cv,
        "tir": tir,
        "hypo": hypo,
        "hyper": hyper,
        "very_hypo": very_hypo,
        "very_hyper": very_hyper,
        "tight_range": tight_range,
        # Formula CGM internacional (GMI); no equivale necesariamente a una
        # HbA1c obtenida en laboratorio.
        "gmi": 3.31 + (0.02392 * mean),
        "count": len(values),
        "coverage": coverage,
        "days_with_data": days_with_data,
        "low_events": low_episode_summary(),
        "time_bands": time_bands,
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

def label_reference_lines(ax):
    """Muestra el umbral junto a cada línea de rango, sin depender del eje."""
    for value, color in ((70, "#15803d"), (180, "#c2410c")):
        ax.annotate(
            f"{value} mg/dL", xy=(1, value), xycoords=("axes fraction", "data"),
            xytext=(7, 0), textcoords="offset points", va="center", ha="left",
            fontsize=9, fontweight="bold", color=color, clip_on=False,
            bbox={"boxstyle": "round,pad=0.18", "fc": "white", "ec": "none", "alpha": 0.92},
        )

def plot_agp(values, output_path, period_days=14):
    """Genera un AGP de alto contraste y legible en pantalla."""
    percentiles, x = agp_data(values)
    if percentiles is None:
        return False
    
    try:
        # El AGP debe leerse igual de bien que los perfiles diarios: una
        # proporción alta y una escala temporal explícita, no fechas.
        fig, ax = plt.subplots(figsize=(14, 7.2), dpi=120)
        ax.set_facecolor("#ffffff")
        ax.axhspan(70, 180, color="#dcfce7", alpha=0.82, label="Objetivo 70-180")
        ax.fill_between(x, percentiles[0], percentiles[4], alpha=0.26, color="#93c5fd", label="P5-P95")
        ax.fill_between(x, percentiles[1], percentiles[3], alpha=0.70, color="#14b8a6", label="P25-P75")
        ax.plot(x, percentiles[0], lw=1.0, ls="--", color="#2563eb", alpha=0.85)
        ax.plot(x, percentiles[4], lw=1.0, ls="--", color="#2563eb", alpha=0.85)
        ax.plot(x, percentiles[2], lw=3.4, color="#0f172a", label="Mediana (P50)")
        ax.axhline(70, color="#15803d", lw=1.4)
        ax.axhline(180, color="#c2410c", lw=1.4)
        label_reference_lines(ax)
        
        ax.set(xlim=(0, 24), ylim=(40, 300), xlabel="Hora del día", 
               ylabel="mg/dL", title=f"AGP - Ultimos {period_days} dias")
        ax.xaxis.set_major_locator(mticker.MultipleLocator(2))
        ax.xaxis.set_minor_locator(mticker.MultipleLocator(1))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda hour, _: f"{int(hour):02d}:00"))
        ax.grid(which="major", alpha=0.34, color="#94a3b8", linewidth=0.8)
        ax.grid(which="minor", axis="x", alpha=0.16, color="#cbd5e1", linewidth=0.5)
        ax.legend(loc="upper right", fontsize=11, frameon=True, facecolor="white", framealpha=0.98)
        
        fig.tight_layout()
        fig.savefig(output_path, dpi=100, bbox_inches='tight')
        plt.close(fig)
        return True
    except Exception as e:
        log.error(f"Error generando AGP: {e}")
        return False

def plot_daily_profiles(values, events, output_dir, max_days=14):
    """Generar gráficos de perfiles diarios del periodo solicitado."""
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
    for day in sorted(by_day.keys(), reverse=True)[:max_days]:
        try:
            fig, ax = plt.subplots(figsize=(14, 7.2), dpi=120)
            
            # Datos del día
            times = [t for t, _ in by_day[day]]
            glu = [v for _, v in by_day[day]]
            daily_mean = float(np.mean(glu))
            daily_min = float(np.min(glu))
            daily_max = float(np.max(glu))
            
            ax.axhspan(70, 180, color="#dcfce7", alpha=0.78)
            ax.axhline(70, color="#15803d", lw=1.2)
            ax.axhline(180, color="#c2410c", lw=1.2)
            label_reference_lines(ax)
            ax.plot(times, glu, marker='o', lw=2.2, markersize=3.2, color="#0f3b8f")
            ax.fill_between(times, 40, glu, alpha=0.13, color="#60a5fa")
            
            # Marcar insulina (rojo) y entrenamientos (violeta). Además de
            # la línea vertical se muestra una etiqueta, para que el evento
            # siga siendo identificable incluso en pantallas pequeñas.
            if day in events_by_day:
                insulin_markers = 0
                workout_markers = 0
                for evt_type, when, payload in events_by_day[day]:
                    if evt_type == "InsulinDoseRecord":
                        units = payload.get("units", "?")
                        insulin_type = str(payload.get("type", "otra")).strip().lower()
                        insulin_label = {
                            "rapida": "rapida",
                            "basal": "basal",
                            "otra": "otra",
                        }.get(insulin_type, insulin_type or "otra")
                        ax.axvline(when, color="#dc2626", linestyle="--", alpha=0.9, linewidth=1.8)
                        ax.annotate(
                            f"Insulina {units}U - {insulin_label}", xy=(when, 290 - 24 * (insulin_markers % 3)), xytext=(-4, -3),
                            textcoords="offset points", rotation=0, va="top", ha="right",
                            fontsize=8, color="#991b1b", fontweight="bold",
                            bbox={"boxstyle": "round,pad=0.2", "fc": "#fee2e2", "ec": "none", "alpha": 0.9},
                        )
                        insulin_markers += 1
                    elif evt_type == "ExerciseSessionRecord":
                        minutes = payload.get("duration_minutes", "?")
                        ax.axvline(when, color="#9333ea", linestyle="--", alpha=0.9, linewidth=1.8)
                        ax.annotate(
                            f"Ejercicio {minutes} min", xy=(when, 285 - 24 * (workout_markers % 3)), xytext=(4, -3),
                            textcoords="offset points", rotation=0, va="top", ha="left",
                            fontsize=8, color="#6b21a8", fontweight="bold",
                            bbox={"boxstyle": "round,pad=0.2", "fc": "#f3e8ff", "ec": "none", "alpha": 0.9},
                        )
                        workout_markers += 1
            
            ax.set(xlim=(datetime.combine(day, datetime.min.time(), tzinfo=TZ),
                         datetime.combine(day, datetime.max.time(), tzinfo=TZ)),
                   ylim=(40, 300), ylabel="mg/dL",
                   title=(f"Perfil diario - {day.strftime('%d/%m/%Y')}\n"
                          f"Media {daily_mean:.0f} mg/dL   |   "
                          f"Min {daily_min:.0f} mg/dL   |   "
                          f"Max {daily_max:.0f} mg/dL"))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.grid(alpha=0.28, color="#94a3b8", linewidth=0.7)
            
            filename = f"daily-{day.isoformat()}.png"
            filepath = output_dir / filename
            fig.tight_layout()
            fig.savefig(filepath, dpi=100, bbox_inches='tight')
            plt.close(fig)
            saved.append(filename)
        except Exception as e:
            log.error(f"Error generando perfil del día {day}: {e}")
    
    return saved

def plot_hour_week_heatmap(values, output_path):
    """Mapa de medias por hora y dia de la semana para periodos largos."""
    buckets = [[[] for _ in range(24)] for _ in range(7)]
    for when, value in values:
        local = when.astimezone(TZ)
        buckets[local.weekday()][local.hour].append(value)
    matrix = np.array([
        [np.mean(cell) if cell else np.nan for cell in weekday]
        for weekday in buckets
    ])
    if np.isnan(matrix).all():
        output_path.unlink(missing_ok=True)
        return None
    fig, ax = plt.subplots(figsize=(14, 5.8), dpi=120)
    image = ax.imshow(np.ma.masked_invalid(matrix), aspect="auto", cmap="RdYlGn_r", vmin=70, vmax=220)
    ax.set_xticks(range(24))
    ax.set_xticklabels([f"{hour:02d}" for hour in range(24)], fontsize=8)
    ax.set_yticks(range(7))
    ax.set_yticklabels(["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"])
    ax.set_xlabel("Hora del dia")
    ax.set_title("Patron horario por dia de la semana - media de glucosa", loc="left", fontweight="bold")
    for day in range(7):
        for hour in range(24):
            if not np.isnan(matrix[day, hour]):
                ax.text(hour, day, f"{matrix[day, hour]:.0f}", ha="center", va="center", fontsize=6.5, color="#17243a")
    colorbar = fig.colorbar(image, ax=ax, pad=0.015)
    colorbar.set_label("Media mg/dL")
    fig.tight_layout()
    fig.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return output_path.name

def long_window_insights(values, end, period_days=30):
    """Resume tendencia semanal y estabilidad nocturna sin inferir causalidad."""
    weeks = []
    for offset in range(3, -1, -1):
        week_end = end - timedelta(days=offset * 7)
        week_start = week_end - timedelta(days=7)
        week_values = [(when, value) for when, value in values if week_start <= when < week_end]
        metrics = calculate_glucose_metrics(week_values, period_days=7)
        weeks.append({
            "label": f"{week_start.strftime('%d/%m')} - {week_end.strftime('%d/%m')}",
            "metrics": metrics,
        })
    night_values = [(when, value) for when, value in values if 0 <= when.astimezone(TZ).hour < 6]
    if night_values:
        readings = np.array([value for _, value in night_values])
        night = {
            "mean": float(np.mean(readings)),
            "cv": float(np.std(readings) / np.mean(readings) * 100) if np.mean(readings) else 0,
            "tir": float(np.mean((readings >= 70) & (readings <= 180)) * 100),
            "hypo": float(np.mean(readings < 70) * 100),
            "days": len({when.astimezone(TZ).date() for when, _ in night_values}),
        }
    else:
        night = None
    return {"weeks": weeks, "night": night, "period_days": period_days}

# ============================================================================
# PROCESAMIENTO DE EVENTOS
# ============================================================================

def aggregate_events(events):
    """Agregar eventos por tipo y día."""
    aggregated = {
        "by_day": {},
        "by_type": {}
    }
    # Los totales diarios los calcula Health Connect aplicando su prioridad de
    # fuentes. Si existen, sustituyen la suma bruta de StepsRecord, que puede
    # incluir registros solapados de varias aplicaciones.
    daily_step_totals = {}
    
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
        elif event_type == "DailyStepsTotal":
            daily_step_totals[day] = max(daily_step_totals.get(day, 0), int(payload.get("count", 0)))
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

    for day, total in daily_step_totals.items():
        aggregated["by_day"][day]["steps"] = total
    
    return aggregated

# ============================================================================
# GENERACIÓN HTML
# ============================================================================

def generate_html_report(end, start, metrics, previous_metrics, daily_profiles, aggregated, renpho, renpho_chart, legacy_assets, clarity_history=None, period_days=14, asset_prefix="", include_legacy=True, long_window=None):
    """Generar HTML del informe completo."""
    # Las rutas de las imágenes diarias se reutilizan. Esta versión evita que
    # el navegador muestre una copia en caché después de actualizar el informe.
    asset_version = int(time.time())
    asset = lambda name: f"{asset_prefix}{name}"
    
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

    def delta(value, previous, suffix="", decimals=1, inverted=False):
        """Texto de variacion respecto al periodo anterior, si hay datos."""
        if previous is None:
            return "Sin periodo previo comparable"
        change = value - previous
        if abs(change) < 0.05:
            return "Sin cambios relevantes"
        arrow = "↑" if change > 0 else "↓"
        color = "#16a34a" if ((change > 0) != inverted) else "#dc2626"
        return f'<span style="color:{color};font-weight:700">{arrow} {abs(change):.{decimals}f}{suffix}</span> vs. {period_days} dias previos'

    comparison_html = ""
    if previous_metrics and previous_metrics.get("count"):
        comparison_html = f"""
        <section class="control-section">
            <h2>Comparativa con los {period_days} dias previos</h2>
            <p class="section-note">Cambio descriptivo entre dos periodos consecutivos. Interpretar junto a la cobertura de datos.</p>
            <div class="cards compact-cards">
                <div class="card"><b>Tiempo en rango</b><h3>{metrics['tir']:.1f}%</h3><small>{delta(metrics['tir'], previous_metrics['tir'], '%')}</small></div>
                <div class="card"><b>Glucosa media</b><h3>{metrics['mean']:.0f} mg/dL</h3><small>{delta(metrics['mean'], previous_metrics['mean'], ' mg/dL', 0, inverted=True)}</small></div>
                <div class="card"><b>Variabilidad (CV)</b><h3>{metrics['cv']:.1f}%</h3><small>{delta(metrics['cv'], previous_metrics['cv'], '%', 1, inverted=True)}</small></div>
            </div>
        </section>
        """

    band_rows = "".join(
        f"<tr><td><strong>{band['label']}</strong></td><td>{band['mean']:.0f}</td><td>{band['tir']:.1f}%</td><td>{band['low']:.1f}%</td><td>{band['high']:.1f}%</td></tr>"
        for band in metrics["time_bands"]
    )
    band_html = f"""
        <section class="time-section">
            <h2>Patron por franja horaria</h2>
            <p class="section-note">Media y distribucion de las lecturas disponibles en cada franja.</p>
            <table>
                <tr><th>Franja</th><th>Media (mg/dL)</th><th>En rango</th><th>&lt;70</th><th>&gt;180</th></tr>
                {band_rows}
            </table>
        </section>
    """ if band_rows else ""

    long_window_html = ""
    if long_window:
        weekly_rows = "".join(
            f"<tr><td><strong>{week['label']}</strong></td><td>{week['metrics']['mean']:.0f}</td><td>{week['metrics']['tir']:.1f}%</td><td>{week['metrics']['cv']:.1f}%</td><td>{week['metrics']['coverage']:.1f}%</td></tr>"
            for week in long_window["weeks"]
        )
        night = long_window.get("night")
        night_html = (
            f"<div class='cards compact-cards'><div class='card'><b>Media nocturna</b><h3>{night['mean']:.0f} mg/dL</h3><small>00:00 - 06:00, {night['days']} dias con datos.</small></div>"
            f"<div class='card'><b>Variabilidad nocturna</b><h3>{night['cv']:.1f}%</h3><small>CV de lecturas nocturnas.</small></div>"
            f"<div class='card'><b>En rango nocturno</b><h3>{night['tir']:.1f}%</h3><small>Rango 70 - 180 mg/dL.</small></div>"
            f"<div class='card'><b>Bajo 70 nocturno</b><h3>{night['hypo']:.1f}%</h3><small>Lecturas, no episodios.</small></div></div>"
        ) if night else "<p class='section-note'>Sin lecturas nocturnas suficientes para resumir este periodo.</p>"
        heatmap = long_window.get("heatmap")
        heatmap_html = (
            f"<img src=\"{asset(heatmap)}?v={asset_version}\" alt=\"Patron horario por dia de la semana\"><p class='section-note'>Cada celda muestra la media de glucosa disponible; una celda vacia indica falta de datos.</p>"
            if heatmap else "<p class='section-note'>No hay suficientes datos para el patron horario.</p>"
        )
        long_window_html = f"""
        <section class="trend-section">
            <h2>Tendencia de 4 semanas</h2>
            <p class="section-note">Resumen descriptivo de cuatro bloques consecutivos de siete dias.</p>
            <table><tr><th>Semana</th><th>Media (mg/dL)</th><th>En rango</th><th>CV</th><th>Cobertura</th></tr>{weekly_rows}</table>
        </section>
        <section class="night-section"><h2>Estabilidad nocturna</h2>{night_html}</section>
        <section class="heatmap-section"><h2>Patron horario semanal</h2>{heatmap_html}</section>
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
            {f'<img src="{renpho_chart}?v={asset_version}" alt="Evolucion de la composicion corporal RENPHO">' if renpho_chart else '<p class="section-note">La grafica aparecera al cargar el primer reporte RENPHO valido.</p>'}
        </section>
        """

    legacy_glucose_html = ""
    graph = lambda key, title, note: (
        f'<section><h2>{title}</h2><img src="{legacy_assets[key]}?v={asset_version}" alt="{title}">'
        f'<p><small>{note}</small></p></section>' if key in legacy_assets else ""
    )
    legacy_glucose_html = "".join([
        graph("comparison", "Comparativa de 14 días", "Una curva por día para comparar patrones horarios."),
    ]) if include_legacy else ""
    clarity_history_html = (
        '<section><h2>Histórico Clarity</h2><p>Periodo previo al cambio de aplicación: '
        f'<a href="{clarity_history}">abrir los 14 días históricos</a>.</p></section>'
        if clarity_history else ""
    )
    
    # Preparar imágenes de perfiles diarios
    profiles_html = ""
    if daily_profiles:
        profiles_html = f"<section class='daily-profiles'><h2>📊 Perfiles Diarios - ultimos {period_days} dias</h2><div class='profile-list'>"
        for img in daily_profiles:
            profiles_html += f'<figure><img src="{asset(img)}?v={asset_version}" alt="Perfil diario"></figure>'
        profiles_html += "</div></section>"
    
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
        .compact-cards .card {{ padding: 1rem; }}
        .compact-cards .card h3 {{ font-size: 1.45rem; }}
        .section-note {{ color: #4b5563; font-size: .92rem; margin: -.35rem 0 1rem; }}
        .metric-note {{ color: #4b5563; font-size: .88rem; margin-top: -.75rem; }}
        
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
        
        .profile-list {{ display: grid; grid-template-columns: 1fr; gap: 1.5rem; }}
        .daily-profiles figure {{
            background: white;
            padding: 1.25rem;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(15,23,42,0.10);
        }}
        .daily-profiles figure img {{ width: 100%; display: block; }}
        
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
        <h1>📊 Informe Integrado de {period_days} dias</h1>
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
        <p class="metric-note">Porcentajes calculados sobre las lecturas disponibles. Los objetivos, si se usan, deben ser personalizados con el equipo clinico.</p>
    </section>

    <section class="control-section">
        <h2>Control y seguridad</h2>
        <div class="cards compact-cards">
            <div class="card">
                <b>GMI estimado</b>
                <h3>{metrics['gmi']:.1f}%</h3>
                <small>Estimacion CGM; no sustituye HbA1c de laboratorio.</small>
            </div>
            <div class="card">
                <b>Cobertura CGM</b>
                <h3>{metrics['coverage']:.1f}%</h3>
                <small>{metrics['days_with_data']} dias con lecturas.</small>
            </div>
            <div class="card">
                <b>Rango estrecho (70-140)</b>
                <h3>{metrics['tight_range']:.1f}%</h3>
                <small>Indicador descriptivo adicional.</small>
            </div>
            <div class="card">
                <b>Muy bajo (&lt;54)</b>
                <h3>{metrics['very_hypo']:.1f}%</h3>
                <small>Parte del tiempo bajo total.</small>
            </div>
            <div class="card">
                <b>Muy alto (&gt;250)</b>
                <h3>{metrics['very_hyper']:.1f}%</h3>
                <small>Parte del tiempo alto total.</small>
            </div>
            <div class="card">
                <b>Episodios bajo 70</b>
                <h3>{metrics['low_events']['count']}</h3>
                <small>{metrics['low_events']['minutes']} min estimados; max. {metrics['low_events']['longest']} min.</small>
            </div>
        </div>
    </section>

    {comparison_html}

    {band_html}

    {long_window_html}
    
    <section class="agp-section">
        <h2>📉 AGP - Arbol de Glicemia Promedio</h2>
        <img src="{asset('agp.png')}?v={asset_version}" alt="Gráfico AGP de glucosa">
        <p><small>Percentiles de glucosa por hora (ultimos {period_days} dias). P5-P95 (claro), P25-P75 (oscuro), P50 (línea central).</small></p>
    </section>
    
    {legacy_glucose_html}

    {profiles_html}

    {clarity_history_html}
    
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
                <h3>{metrics['days_with_data']}/{period_days} dias</h3>
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
    previous_start = start - timedelta(days=14)
    
    # Obtener datos
    log.info("Obteniendo datos de Nightscout...")
    nightscout_data = glucose_from_nightscout(start, end)
    glucose_data = glucose_with_clarity_continuity(nightscout_data, start, end)
    log.info("Continuidad de glucosa: %s lecturas Nightscout, %s lecturas combinadas", len(nightscout_data), len(glucose_data))

    log.info("Obteniendo periodo anterior para la comparativa...")
    previous_nightscout_data = glucose_from_nightscout(previous_start, start)
    previous_glucose_data = glucose_with_clarity_continuity(previous_nightscout_data, previous_start, start)
    
    log.info("Obteniendo eventos de base de datos...")
    events = health_events_from_db(start)
    
    log.info("Leyendo composición RENPHO...")
    renpho = renpho_composition()
    renpho_chart = plot_renpho_evolution(renpho_history(), OUT / "renpho-evolution.png")
    legacy_assets = preserve_legacy_assets()
    clarity_history = write_clarity_history()
    
    # Calcular métricas
    log.info("Calculando métricas glucémicas...")
    metrics = calculate_glucose_metrics(glucose_data)
    previous_metrics = calculate_glucose_metrics(previous_glucose_data)
    
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
    html = generate_html_report(end, start, metrics, previous_metrics, daily_profiles, aggregated, renpho, renpho_chart, legacy_assets, clarity_history)
    
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

    # Informe ampliado independiente: sus recursos viven en ``mensual/`` para
    # que no sustituyan los graficos del informe de 14 dias.
    monthly_days = 30
    monthly_start = end - timedelta(days=monthly_days)
    monthly_previous_start = monthly_start - timedelta(days=monthly_days)
    log.info("Generando informe ampliado de %s dias...", monthly_days)
    monthly_nightscout = glucose_from_nightscout(monthly_start, end)
    monthly_glucose = glucose_with_clarity_continuity(monthly_nightscout, monthly_start, end)
    monthly_previous_nightscout = glucose_from_nightscout(monthly_previous_start, monthly_start)
    monthly_previous_glucose = glucose_with_clarity_continuity(
        monthly_previous_nightscout, monthly_previous_start, monthly_start
    )
    monthly_events = health_events_from_db(monthly_start)
    monthly_assets = OUT / "mensual"
    monthly_assets.mkdir(parents=True, exist_ok=True)
    plot_agp(monthly_glucose, monthly_assets / "agp.png", period_days=monthly_days)
    monthly_profiles = plot_daily_profiles(monthly_glucose, monthly_events, monthly_assets, max_days=monthly_days)
    monthly_insights = long_window_insights(monthly_glucose, end, period_days=monthly_days)
    monthly_insights["heatmap"] = plot_hour_week_heatmap(monthly_glucose, monthly_assets / "pattern-week-hour.png")
    monthly_html = generate_html_report(
        end, monthly_start,
        calculate_glucose_metrics(monthly_glucose, period_days=monthly_days),
        calculate_glucose_metrics(monthly_previous_glucose, period_days=monthly_days),
        monthly_profiles, aggregate_events(monthly_events), renpho, renpho_chart,
        legacy_assets, clarity_history, period_days=monthly_days,
        asset_prefix="mensual/", include_legacy=False, long_window=monthly_insights,
    )
    monthly_file = OUT / "healthhub-mensual.html"
    monthly_file.write_text(monthly_html, encoding="utf-8")
    monthly_timestamped = OUT / f"healthhub-mensual-{year}-W{week_num:02d}.html"
    monthly_timestamped.write_text(monthly_html, encoding="utf-8")
    log.info("Informe ampliado guardado: %s", monthly_file)

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
