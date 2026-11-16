import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
import psycopg
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

app=FastAPI(docs_url=None,redoc_url=None); DSN=os.environ["DATABASE_URL"]; REPORTS=Path("/reports/healthhub/reports/semanal")
REFRESH_REQUEST = REPORTS.parent / ".refresh-request"
CLARITY_EXPORT = Path("/reports/healthhub/clarity/clarity-export.csv")
def event(kind, occurred_at, payload):
    record={"source":"portal","event_type":kind,"occurred_at":occurred_at,"payload":payload}
    raw=json.dumps(record,sort_keys=True,separators=(",",":")); digest=hashlib.sha256(raw.encode()).hexdigest()
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO health_events(source,event_type,occurred_at,payload,digest) VALUES(%s,%s,%s,%s::jsonb,%s) ON CONFLICT(digest) DO NOTHING",("portal",kind,occurred_at,json.dumps(payload),digest))
    REFRESH_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    REFRESH_REQUEST.touch()

@app.get("/",response_class=HTMLResponse)
def index():
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M")
    return f'''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Health Report</title><style>
    *{{box-sizing:border-box}} body{{margin:0;background:#f3f6fb;color:#17243a;font:16px system-ui}} .bar{{background:#102a43;color:#fff;padding:14px 22px;display:flex;gap:14px;align-items:center}}.bar b{{font-size:20px}}.bar span{{opacity:.85;flex:1}} main{{max-width:1280px;margin:auto;padding:20px}}.forms{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:18px}}form{{background:#fff;padding:18px;border-radius:12px;box-shadow:0 2px 10px #102a4318}}h2{{margin:0 0 12px;font-size:18px}}label{{display:block;font-size:13px;font-weight:650;margin-top:9px}}input,select,button{{width:100%;padding:10px;margin-top:4px;border:1px solid #ccd6e3;border-radius:8px;font:inherit}}button{{border:0;background:#38b66b;color:#062a1b;font-weight:750;cursor:pointer;margin-top:15px}}iframe{{width:100%;min-height:82vh;border:0;border-radius:12px;background:#fff;box-shadow:0 2px 10px #102a4320}}small{{color:#53647d}}</style>
    <div class="bar"><b>Health Report</b><span>Acceso personal protegido</span><a href="/report/healthhub-semanal.html" style="color:#fff">Abrir informe</a></div><main><div class="forms">
    <form method="post" action="/insulin"><h2>Registrar dosis de insulina</h2><label>Fecha y hora<input required type="datetime-local" name="at" value="{now}"></label><label>Unidades<input required type="number" step="0.1" min="0.1" name="units" placeholder="Ej. 4.5"></label><label>Tipo<select name="kind"><option value="rapida">Rapida</option><option value="basal">Basal</option><option value="otra">Otra</option></select></label><button>Guardar dosis y actualizar informe</button></form>
    <form method="post" action="/workout"><h2>Registrar entrenamiento</h2><label>Inicio<input required type="datetime-local" name="at" value="{now}"></label><label>Duracion (minutos)<input required type="number" min="1" name="minutes" placeholder="Ej. 45"></label><label>Intensidad<select name="intensity"><option value="ligera">Ligera</option><option value="media" selected>Media</option><option value="alta">Alta</option></select></label><button>Guardar entrenamiento y actualizar informe</button></form>
    <form method="post" action="/clarity/upload" enctype="multipart/form-data"><h2>Importar histórico Clarity</h2><label>Exportación CSV<input required type="file" accept=".csv,text/csv" name="csv_file"></label><button>Guardar histórico y generar informe</button></form>
    </div><p><small>Al guardar, el informe se regenera automáticamente y los eventos se marcan en los perfiles de glucosa.</small></p><iframe src="/report/healthhub-semanal.html" title="Informe integrado"></iframe></main></html>'''
@app.post("/insulin")
def insulin(at:str=Form(...),units:float=Form(...),kind:str=Form(...)):
    event("InsulinDoseRecord",datetime.fromisoformat(at).astimezone().isoformat(),{"units":units,"type":kind}); return RedirectResponse("/",303)
@app.post("/workout")
def workout(at:str=Form(...),minutes:int=Form(...),intensity:str=Form(...)):
    event("ExerciseSessionRecord",datetime.fromisoformat(at).astimezone().isoformat(),{"duration_minutes":minutes,"intensity":intensity}); return RedirectResponse("/",303)

@app.post("/clarity/upload")
async def upload_clarity(csv_file: UploadFile = File(...)):
    content = await csv_file.read()
    if len(content) > 5 * 1024 * 1024:
        return HTMLResponse("El CSV supera el límite de 5 MB.", 413)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return HTMLResponse("El archivo debe ser un CSV UTF-8 exportado por Clarity.", 400)
    if "Timestamp (YYYY-MM-DDThh:mm:ss)" not in text or "Glucose Value (mg/dL)" not in text:
        return HTMLResponse("El archivo no parece una exportación de Clarity válida.", 400)
    CLARITY_EXPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = CLARITY_EXPORT.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(CLARITY_EXPORT)
    REFRESH_REQUEST.touch()
    return RedirectResponse("/", 303)
@app.get("/report/{name}")
def report(name:str):
    path=(REPORTS/name).resolve()
    if REPORTS not in path.parents or not path.is_file(): return HTMLResponse("Informe aún no generado",404)
    return FileResponse(path)
