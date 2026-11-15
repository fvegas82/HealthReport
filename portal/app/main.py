import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
import psycopg
from fastapi import FastAPI, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

app=FastAPI(docs_url=None,redoc_url=None); DSN=os.environ["DATABASE_URL"]; REPORTS=Path("/reports/healthhub/reports/semanal")
def event(kind, occurred_at, payload):
    record={"source":"portal","event_type":kind,"occurred_at":occurred_at,"payload":payload}
    raw=json.dumps(record,sort_keys=True,separators=(",",":")); digest=hashlib.sha256(raw.encode()).hexdigest()
    with psycopg.connect(DSN) as db: db.execute("INSERT INTO health_events(source,event_type,occurred_at,payload,digest) VALUES(%s,%s,%s,%s::jsonb,%s) ON CONFLICT(digest) DO NOTHING",("portal",kind,occurred_at,json.dumps(payload),digest))

@app.get("/",response_class=HTMLResponse)
def index():
    return '''<!doctype html><html lang="es"><meta charset="utf-8"><title>Health Report</title><style>body{font:16px system-ui;margin:2rem auto;max-width:1050px}form{display:inline-block;vertical-align:top;padding:1rem;margin:0 1rem 1rem 0;border:1px solid #ddd;border-radius:10px}input,select,button{padding:.55rem;margin:.25rem}</style><h1>Health Report</h1><p>Acceso personal protegido. Los datos son descriptivos y no constituyen recomendaciones clínicas.</p><form method="post" action="/insulin"><h2>Registrar insulina</h2><input required type="datetime-local" name="at"><input required type="number" step="0.1" min="0" name="units" placeholder="Unidades"><select name="kind"><option>rapida</option><option>basal</option><option>otra</option></select><button>Guardar</button></form><form method="post" action="/workout"><h2>Registrar entrenamiento</h2><input required type="datetime-local" name="at"><input required type="number" min="1" name="minutes" placeholder="Minutos"><select name="intensity"><option>ligera</option><option selected>media</option><option>alta</option></select><button>Guardar</button></form><p><a href="/report/healthhub-semanal.html">Abrir informe actualizado</a> (se regenera cada cinco minutos).</p></html>'''
@app.post("/insulin")
def insulin(at:str=Form(...),units:float=Form(...),kind:str=Form(...)):
    event("InsulinDoseRecord",datetime.fromisoformat(at).astimezone().isoformat(),{"units":units,"type":kind}); return RedirectResponse("/",303)
@app.post("/workout")
def workout(at:str=Form(...),minutes:int=Form(...),intensity:str=Form(...)):
    event("ExerciseSessionRecord",datetime.fromisoformat(at).astimezone().isoformat(),{"duration_minutes":minutes,"intensity":intensity}); return RedirectResponse("/",303)
@app.get("/report/{name}")
def report(name:str):
    path=(REPORTS/name).resolve()
    if REPORTS not in path.parents or not path.is_file(): return HTMLResponse("Informe aún no generado",404)
    return FileResponse(path)
