import hashlib, json, os, time, urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import psycopg

TZ=ZoneInfo(os.getenv("TZ","Europe/Madrid")); OUT=Path("/reports"); OUT.mkdir(exist_ok=True)
DSN=os.environ["DATABASE_URL"]; NIGHT=os.environ["NIGHTSCOUT_URL"].rstrip("/")

def glucose(start,end):
    token=hashlib.sha1(os.environ["NIGHTSCOUT_API_SECRET"].encode()).hexdigest()
    url=f"{NIGHT}/api/v1/entries.json?count=100000&find[date][$gte]={int(start.timestamp()*1000)}&find[date][$lte]={int(end.timestamp()*1000)}"
    req=urllib.request.Request(url,headers={"api-secret":token})
    with urllib.request.urlopen(req,timeout=60) as r: rows=json.load(r)
    return sorted([(datetime.fromtimestamp(x["date"]/1000,TZ),float(x["sgv"])) for x in rows if x.get("sgv")],key=lambda x:x[0])

def events(start):
    with psycopg.connect(DSN) as db:
        return db.execute("SELECT event_type,occurred_at,payload FROM health_events WHERE occurred_at >= %s",(start,)).fetchall()

def write_report():
    end=datetime.now(TZ); start=end-timedelta(days=14); values=glucose(start,end); ev=events(start)
    gs=np.array([v for _,v in values]); mean=float(np.mean(gs)) if len(gs) else 0; tir=float(np.mean((gs>=70)&(gs<=180))*100) if len(gs) else 0
    hours=[[] for _ in range(96)]
    for t,v in values: hours[(t.hour*60+t.minute)//15].append(v)
    p=np.array([[np.percentile(x,q) if x else np.nan for x in hours] for q in (5,25,50,75,95)]); x=np.arange(96)/4
    fig,ax=plt.subplots(figsize=(12,4)); ax.axhspan(70,180,color="#e7f5eb"); ax.fill_between(x,p[0],p[4],alpha=.25); ax.fill_between(x,p[1],p[3],alpha=.45); ax.plot(x,p[2],lw=2.5); ax.set(xlim=(0,24),ylim=(40,300),xlabel="Hora",ylabel="mg/dL",title="AGP - últimos 14 días"); ax.grid(alpha=.2); fig.tight_layout(); fig.savefig(OUT/"agp.png",dpi=160); plt.close(fig)
    byday={};
    for typ,when,payload in ev:
        day=when.astimezone(TZ).date().isoformat(); byday.setdefault(day,{"steps":0,"distance":0,"kcal":0})
        if typ=="StepsRecord": byday[day]["steps"]+=int(payload.get("count",0))
        if typ=="DistanceRecord": byday[day]["distance"]+=float(payload.get("meters",0))/1000
        if typ in ("TotalCaloriesBurnedRecord","ActiveCaloriesBurnedRecord"): byday[day]["kcal"]+=float(payload.get("kcal",0))
    rows="".join(f"<tr><td>{d}</td><td>{v['steps']:,}</td><td>{v['distance']:.1f} km</td><td>{v['kcal']:.0f} kcal</td></tr>" for d,v in sorted(byday.items(),reverse=True))
    html=f'''<!doctype html><html lang="es"><meta charset="utf-8"><title>Health Report</title><style>body{{font:16px system-ui;margin:3rem;color:#15253b}}main{{max-width:1000px;margin:auto}}.cards{{display:flex;gap:1rem}}.card{{border:1px solid #dbe4ef;border-radius:12px;padding:1rem;flex:1}}img{{max-width:100%}}table{{width:100%;border-collapse:collapse}}td,th{{padding:.6rem;border-bottom:1px solid #ddd;text-align:left}}</style><main><h1>Informe integrado de 14 días</h1><p>Generado {end:%d/%m/%Y %H:%M} {TZ.key}. Datos descriptivos; no constituye recomendación clínica.</p><section class="cards"><div class="card"><b>Glucosa media</b><h2>{mean:.0f} mg/dL</h2></div><div class="card"><b>Tiempo en rango</b><h2>{tir:.1f}%</h2></div><div class="card"><b>Lecturas</b><h2>{len(values):,}</h2></div></section><h2>AGP</h2><img src="agp.png" alt="AGP de glucosa"><h2>Actividad diaria</h2><table><tr><th>Día</th><th>Pasos</th><th>Distancia</th><th>Energía</th></tr>{rows}</table></main></html>'''
    (OUT/"healthhub-semanal.html").write_text(html,encoding="utf-8")

while True:
    try: write_report()
    except Exception as error: print(f"report error: {error}",flush=True)
    time.sleep(300)
