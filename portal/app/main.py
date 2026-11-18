import hashlib, html, json, os, re, subprocess, unicodedata
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import psycopg
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Image as PDFImage, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

app=FastAPI(docs_url=None,redoc_url=None); DSN=os.environ["DATABASE_URL"]; REPORTS=Path("/reports/healthhub/reports/semanal")
REFRESH_REQUEST = REPORTS.parent / ".refresh-request"
CLARITY_EXPORT = Path("/reports/healthhub/clarity/clarity-export.csv")
RENPHO_DIR = Path("/reports/healthhub/reports/renpho")
RENPHO_UPLOADS = RENPHO_DIR / "uploads"
RENPHO_HISTORY = RENPHO_DIR / "history.json"

def request_refresh():
    REFRESH_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    REFRESH_REQUEST.touch()

def _number_after(label, text, required=False):
    """Extrae un valor RENPHO aunque el OCR intercale texto o saltos de linea."""
    # RENPHO dibuja etiquetas y valores en columnas distintas. El OCR puede
    # insertar puntos, estados como "Average" o una nueva linea entre ambos.
    label_pattern = re.escape(label).replace(r"\ ", r"\s+").replace(r"\-", r"[-\s]?")
    match = re.search(label_pattern + r"[^0-9]{0,80}([0-9]{1,4}(?:[.,][0-9]{1,2})?)", text, re.I)
    if not match:
        if required:
            raise HTTPException(422, f"No se pudo leer {label} de la captura RENPHO.")
        return None
    return float(match.group(1).replace(",", "."))

def parse_renpho_ocr(text, captured_at):
    """Convierte el OCR local de la imagen RENPHO en una lectura estructurada."""
    reading = {
        "captured_at": captured_at,
        "source": "portal-ocr-local",
        "weight": _number_after("Weight", text, required=True),
        "bmi": _number_after("BMI", text),
        "fat_percentage": _number_after("Body Fat Percentage", text),
        "fat_mass": _number_after("Body Fat Mass", text),
        "visceral_fat": _number_after("Visceral Fat", text),
        "muscle_percentage": _number_after("Muscle Percentage", text),
        "muscle_mass": _number_after("Muscle Mass", text),
        "skeletal_muscle_percentage": _number_after("Skeletal Muscle Percentage", text),
        "skeletal_muscle_mass": _number_after("Skeletal Muscle Mass", text),
        "bone_percentage": _number_after("Bone Percentage", text),
        "bone_mass": _number_after("Bone Mass", text),
        "fat_free_mass": _number_after("Fat-Free Mass", text),
        "metabolic_age": _number_after("Metabolic Age", text),
        "bmr": _number_after("BMR", text),
        "water_percentage": _number_after("Body Water Percentage", text),
        "water_mass": _number_after("Body Water Mass", text),
        "protein_percentage": _number_after("Protein Percentage", text),
        "protein_mass": _number_after("Protein Mass", text),
    }
    return {key: value for key, value in reading.items() if value is not None}

def save_renpho_reading(reading):
    """Guarda ultima lectura e historico, una sola medicion por instante."""
    RENPHO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(RENPHO_HISTORY.read_text(encoding="utf-8")) if RENPHO_HISTORY.exists() else []
    except (json.JSONDecodeError, OSError):
        history = []
    history = [item for item in history if item.get("captured_at") != reading["captured_at"]]
    history.append(reading)
    history.sort(key=lambda item: item.get("captured_at", ""))
    temporary = RENPHO_HISTORY.with_suffix(".tmp")
    temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(RENPHO_HISTORY)
    (RENPHO_DIR / "ultimo.json").write_text(json.dumps(reading, ensure_ascii=False, indent=2), encoding="utf-8")

def safe_pdf_path(folder, filename):
    """Limita el guardado a una carpeta PDF dentro del volumen del NAS."""
    relative = PurePosixPath(folder.strip() or "semanal")
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(400, "La carpeta debe ser una ruta relativa dentro de PDF.")
    clean_name = re.sub(r"[^A-Za-z0-9._-]", "-", filename.strip() or "health-report")
    if not clean_name.endswith(".pdf"):
        clean_name += ".pdf"
    base = (REPORTS / "pdf").resolve()
    target_dir = (base / relative).resolve()
    if base not in target_dir.parents and target_dir != base:
        raise HTTPException(400, "Ruta PDF no permitida.")
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / clean_name

def safe_report_path(report_name):
    """Solo permite exportar los informes HTML generados por el reporter."""
    relative = PurePosixPath(report_name.strip() or "healthhub-semanal.html")
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".html":
        raise HTTPException(400, "Informe no permitido.")
    target = (REPORTS / relative).resolve()
    if REPORTS not in target.parents or not target.is_file():
        raise HTTPException(404, "El informe seleccionado aun no esta disponible.")
    return target

def pdf_text(value):
    """Evita glifos no disponibles en las fuentes PDF basicas sin perder datos."""
    value = re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")

class ReportParser(HTMLParser):
    """Extrae las secciones, tablas e imagenes del mismo HTML que ve el portal."""
    def __init__(self):
        super().__init__()
        self.sections = [{"title": "", "items": []}]
        self.tag_stack, self.capture, self.capture_tag = [], [], None
        self.table_rows, self.row, self.cell = None, None, None
        self.card_stack = []

    @property
    def section(self):
        return self.sections[-1]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tag_stack.append(tag)
        if tag == "section":
            self.sections.append({"title": "", "items": []})
        elif tag == "img":
            source = urlsplit(attrs.get("src", "")).path
            if source:
                self.section["items"].append(("image", source))
        elif tag == "table":
            self.table_rows = []
        elif tag == "tr" and self.table_rows is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "div" and "card" in attrs.get("class", "").split():
            self.card_stack.append({"label": "", "value": "", "note": ""})
        elif tag in ("h1", "h2", "h3", "p", "b", "small"):
            self.capture, self.capture_tag = [], tag

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        if self.capture_tag:
            self.capture.append(data)

    def handle_endtag(self, tag):
        text = pdf_text("".join(self.capture)) if tag == self.capture_tag else ""
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(pdf_text("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table_rows.append(self.row)
            self.row = None
        elif tag == "table" and self.table_rows is not None:
            self.section["items"].append(("table", self.table_rows))
            self.table_rows = None
        elif tag == "div" and self.card_stack:
            card = self.card_stack.pop()
            if card["label"] or card["value"] or card["note"]:
                self.section["items"].append(("card", card))
        elif text:
            if tag == "h2" and not self.card_stack:
                self.section["title"] = text
            elif tag == "h1":
                self.section["items"].append(("title", text))
            elif self.card_stack:
                key = "label" if tag == "b" else "value" if tag in ("h2", "h3") else "note"
                self.card_stack[-1][key] = " ".join(filter(None, (self.card_stack[-1][key], text)))
            elif tag in ("h3", "p", "small", "b"):
                self.section["items"].append(("text", text))
        if tag == self.capture_tag:
            self.capture, self.capture_tag = [], None
        if self.tag_stack:
            self.tag_stack.pop()

def report_image(html_path, source, max_width):
    image_path = (html_path.parent / source).resolve()
    if html_path.parent not in image_path.parents or not image_path.is_file():
        return None
    try:
        image = PDFImage(str(image_path))
        ratio = min(max_width / image.imageWidth, 15 * cm / image.imageHeight)
        image.drawWidth, image.drawHeight = image.imageWidth * ratio, image.imageHeight * ratio
        return image
    except Exception:
        return None

def build_pdf(output_path, report_name):
    """Exporta el contenido completo del informe web elegido, con sus graficas."""
    report_path = safe_report_path(report_name)
    parser = ReportParser()
    parser.feed(report_path.read_text(encoding="utf-8"))
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], textColor=colors.HexColor("#17243a"), spaceAfter=12))
    styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], textColor=colors.HexColor("#102a43"), spaceBefore=14, spaceAfter=8))
    styles.add(ParagraphStyle(name="Body", parent=styles["BodyText"], leading=15, spaceAfter=7))
    doc = SimpleDocTemplate(str(output_path), pagesize=A4, rightMargin=1.4*cm, leftMargin=1.4*cm, topMargin=1.3*cm, bottomMargin=1.3*cm, title="Health Report")
    usable_width = A4[0] - doc.leftMargin - doc.rightMargin
    story = [Paragraph("Health Report - Informe integrado", styles["ReportTitle"]), Paragraph(f"Exportado {datetime.now().astimezone().strftime('%d/%m/%Y %H:%M')}. Datos descriptivos; no constituyen recomendacion clinica.", styles["Body"])]
    for section in parser.sections:
        if section["title"]:
            story.extend([Spacer(1, 4), Paragraph(html.escape(section["title"]), styles["Section"])])
        card_buffer = []
        def flush_cards():
            nonlocal card_buffer
            if not card_buffer:
                return
            cells = []
            for card in card_buffer:
                content = "<b>{}</b><br/><font size=14>{}</font>{}".format(html.escape(card["label"]), html.escape(card["value"]), f"<br/><font size=8>{html.escape(card['note'])}</font>" if card["note"] else "")
                cells.append(Paragraph(content, styles["Body"]))
            rows = [cells[index:index + 2] for index in range(0, len(cells), 2)]
            if len(rows[-1]) == 1:
                rows[-1].append("")
            grid = Table(rows, colWidths=[usable_width / 2 - 4, usable_width / 2 - 4], hAlign="LEFT")
            grid.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#cbd5e1")), ("INNERGRID", (0, 0), (-1, -1), .4, colors.HexColor("#e2e8f0")), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
            story.extend([grid, Spacer(1, 9)])
            card_buffer = []
        for kind, payload in section["items"]:
            if kind == "card":
                card_buffer.append(payload)
                continue
            flush_cards()
            if kind == "title":
                story.append(Paragraph(html.escape(payload), styles["ReportTitle"]))
            elif kind == "text":
                story.append(Paragraph(html.escape(payload), styles["Body"]))
            elif kind == "image":
                image = report_image(report_path, payload, usable_width)
                if image:
                    story.extend([image, Spacer(1, 9)])
            elif kind == "table" and payload:
                column_count = max(len(row) for row in payload)
                rows = [row + [""] * (column_count - len(row)) for row in payload]
                table = LongTable([[Paragraph(html.escape(cell), styles["Body"]) for cell in row] for row in rows], colWidths=[usable_width / column_count] * column_count, repeatRows=1)
                table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")), ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#cbd5e1")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
                story.extend([table, Spacer(1, 9)])
        flush_cards()
    doc.build(story)
def event(kind, occurred_at, payload):
    record={"source":"portal","event_type":kind,"occurred_at":occurred_at,"payload":payload}
    raw=json.dumps(record,sort_keys=True,separators=(",",":")); digest=hashlib.sha256(raw.encode()).hexdigest()
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO health_events(source,event_type,occurred_at,payload,digest) VALUES(%s,%s,%s,%s::jsonb,%s) ON CONFLICT(digest) DO NOTHING",("portal",kind,occurred_at,json.dumps(payload),digest))
    REFRESH_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    REFRESH_REQUEST.touch()

@app.get("/legacy",response_class=HTMLResponse)
def index():
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M")
    return f'''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Health Report</title><style>
    *{{box-sizing:border-box}} body{{margin:0;background:#f3f6fb;color:#17243a;font:16px system-ui}} .bar{{background:#102a43;color:#fff;padding:14px 22px;display:flex;gap:14px;align-items:center}}.bar b{{font-size:20px}}.bar span{{opacity:.85;flex:1}} main{{max-width:1280px;margin:auto;padding:20px}}.forms{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:18px}}form{{background:#fff;padding:18px;border-radius:12px;box-shadow:0 2px 10px #102a4318}}h2{{margin:0 0 12px;font-size:18px}}label{{display:block;font-size:13px;font-weight:650;margin-top:9px}}input,select,button{{width:100%;padding:10px;margin-top:4px;border:1px solid #ccd6e3;border-radius:8px;font:inherit}}button{{border:0;background:#38b66b;color:#062a1b;font-weight:750;cursor:pointer;margin-top:15px}}iframe{{width:100%;min-height:82vh;border:0;border-radius:12px;background:#fff;box-shadow:0 2px 10px #102a4320}}small{{color:#53647d}}</style>
    <div class="bar"><b>Health Report</b><span>Acceso personal protegido</span><button type="button" id="refresh-report" style="width:auto;margin:0;background:#d8f3e3;color:#062a1b">Actualizar informe</button><a href="/report/healthhub-semanal.html" style="color:#fff">Abrir informe</a></div><main><div class="forms">
    <form method="post" action="/insulin"><h2>Registrar dosis de insulina</h2><label>Fecha y hora<input required type="datetime-local" name="at" value="{now}"></label><label>Unidades<input required type="number" step="0.1" min="0.1" name="units" placeholder="Ej. 4.5"></label><label>Tipo<select name="kind"><option value="rapida">Rapida</option><option value="basal">Basal</option><option value="otra">Otra</option></select></label><button>Guardar dosis y actualizar informe</button></form>
    <form method="post" action="/workout"><h2>Registrar entrenamiento</h2><label>Inicio<input required type="datetime-local" name="at" value="{now}"></label><label>Duracion (minutos)<input required type="number" min="1" name="minutes" placeholder="Ej. 45"></label><label>Intensidad<select name="intensity"><option value="ligera">Ligera</option><option value="media" selected>Media</option><option value="alta">Alta</option></select></label><button>Guardar entrenamiento y actualizar informe</button></form>
    <form method="post" action="/clarity/upload" enctype="multipart/form-data"><h2>Importar histórico Clarity</h2><label>Exportación CSV<input required type="file" accept=".csv,text/csv" name="csv_file"></label><button>Guardar histórico y generar informe</button></form>
    </div><p><small id="refresh-status">Al guardar, el informe se regenera automáticamente y los eventos se marcan en los perfiles de glucosa.</small></p><iframe id="report-frame" src="/report/healthhub-semanal.html" title="Informe integrado"></iframe></main><script>
    const button=document.getElementById('refresh-report'),status=document.getElementById('refresh-status'),frame=document.getElementById('report-frame');
    button.addEventListener('click',async()=>{{
      button.disabled=true; button.textContent='Generando informe...'; status.textContent='Actualización solicitada. Se recargarán los datos al terminar.';
      try {{
        const response=await fetch('/refresh',{{method:'POST'}});
        if(!response.ok) throw new Error('refresh failed');
        setTimeout(()=>{{frame.src='/report/healthhub-semanal.html?v='+Date.now();button.disabled=false;button.textContent='Actualizar informe';status.textContent='Informe actualizado.';}},18000);
      }} catch (_) {{button.disabled=false;button.textContent='Actualizar informe';status.textContent='No se pudo solicitar la actualización. Inténtalo de nuevo.';}}
    }});
    </script></html>'''
@app.get("/", response_class=HTMLResponse)
def dashboard():
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M")
    pdf_name = f"health-report-{datetime.now().astimezone().strftime('%Y-%m-%d')}.pdf"
    return f'''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Health Report</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fb;color:#17243a;font:16px system-ui}}.bar{{background:#102a43;color:#fff;padding:18px 24px}}.bar-inner,main{{max-width:1280px;margin:auto}}.bar-inner{{display:flex;gap:14px;align-items:center}}.brand{{font-size:21px;font-weight:750;flex:1}}.sub{{opacity:.82;font-size:14px}}main{{padding:22px 20px}}.actions{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:18px}}button,.link{{border:0;border-radius:10px;padding:13px 16px;font:inherit;font-weight:750;cursor:pointer;text-decoration:none;text-align:center}}.action{{background:#fff;color:#17243a;border:1px solid #d8e2ef;box-shadow:0 2px 8px #102a4312}}.action:hover{{border-color:#2a7a5a;background:#f0fdf4}}.secondary{{background:#d8f3e3;color:#062a1b;padding:9px 13px;font-size:14px}}.link{{color:#fff;border:1px solid #ffffff55;padding:8px 12px;font-size:14px}}.report-select{{width:auto;margin:0;background:#fff;color:#17243a;font-weight:700}}dialog{{border:0;border-radius:16px;padding:0;max-width:460px;width:calc(100% - 32px);box-shadow:0 18px 56px #102a4380}}dialog::backdrop{{background:#102a4366}}.dialog-body{{padding:24px}}h2{{margin:0 0 8px;font-size:21px}}p{{margin:0 0 16px;color:#53647d}}label{{display:block;font-size:13px;font-weight:700;margin-top:12px}}input,select{{width:100%;padding:11px;margin-top:5px;border:1px solid #ccd6e3;border-radius:8px;font:inherit}}.dialog-actions{{display:flex;gap:10px;justify-content:flex-end;margin-top:20px}}.cancel{{background:#edf2f7;color:#34455c}}.submit{{background:#38b66b;color:#062a1b}}#refresh-status{{display:block;margin:0 0 14px;color:#53647d}}iframe{{width:100%;min-height:82vh;border:0;border-radius:12px;background:#fff;box-shadow:0 2px 10px #102a4320}}@media(max-width:760px){{.actions{{grid-template-columns:repeat(2,1fr)}}.bar-inner{{flex-wrap:wrap}}.sub{{order:3;width:100%}}}}
</style>
<header class="bar"><div class="bar-inner"><div class="brand">Health Report</div><span class="sub">Acceso personal protegido</span><select id="report-select" class="report-select" aria-label="Informe a mostrar"><option value="healthhub-semanal.html">Ultimos 14 dias</option><option value="healthhub-mensual.html">Ultimos 30 dias</option></select><button class="secondary" id="refresh-report" type="button">Actualizar informe</button><a class="link" id="open-report" href="/report/healthhub-semanal.html">Abrir informe</a></div></header>
<main><section class="actions"><button class="action" data-dialog="insulin">Anadir insulina</button><button class="action" data-dialog="workout">Registrar entrenamiento</button><button class="action" data-dialog="history">Cargar historicos</button><button class="action" data-dialog="renpho">Cargar RENPHO</button><button class="action" data-dialog="pdf">Descargar PDF</button></section><small id="refresh-status">Selecciona una accion para registrar informacion, cargar un historico o generar un PDF.</small><iframe id="report-frame" src="/report/healthhub-semanal.html?v={int(datetime.now().timestamp())}" title="Informe integrado"></iframe></main>
<dialog id="insulin"><div class="dialog-body"><h2>Anadir insulina</h2><p>La dosis se marcara en las graficas diarias.</p><form data-refresh-form method="post" action="/insulin"><label>Fecha y hora<input required type="datetime-local" name="at" value="{now}"></label><label>Unidades<input required type="number" step="0.1" min="0.1" name="units" placeholder="Ej. 4.5"></label><label>Tipo<select name="kind"><option value="rapida">Rapida</option><option value="basal">Basal</option><option value="otra">Otra</option></select></label><div class="dialog-actions"><button class="cancel" type="button" data-close>Cancelar</button><button class="submit" type="submit">Guardar y actualizar</button></div></form></div></dialog>
<dialog id="workout"><div class="dialog-body"><h2>Registrar entrenamiento</h2><p>Se mostrara con una marca violeta en las graficas diarias.</p><form data-refresh-form method="post" action="/workout"><label>Inicio<input required type="datetime-local" name="at" value="{now}"></label><label>Duracion (minutos)<input required type="number" min="1" name="minutes" placeholder="Ej. 45"></label><label>Intensidad<select name="intensity"><option value="ligera">Ligera</option><option value="media" selected>Media</option><option value="alta">Alta</option></select></label><div class="dialog-actions"><button class="cancel" type="button" data-close>Cancelar</button><button class="submit" type="submit">Guardar y actualizar</button></div></form></div></dialog>
<dialog id="history"><div class="dialog-body"><h2>Cargar historicos</h2><p>Selecciona una exportacion CSV de Clarity.</p><form data-refresh-form method="post" action="/clarity/upload" enctype="multipart/form-data"><label>Fichero CSV<input required type="file" accept=".csv,text/csv" name="csv_file"></label><div class="dialog-actions"><button class="cancel" type="button" data-close>Cancelar</button><button class="submit" type="submit">Cargar y actualizar</button></div></form></div></dialog>
<dialog id="renpho"><div class="dialog-body"><h2>Cargar reporte RENPHO</h2><p>La imagen se guarda en el NAS y se lee localmente. Revisa el resultado en el informe; no se envia a servicios externos.</p><form data-refresh-form method="post" action="/renpho/upload" enctype="multipart/form-data"><label>Captura RENPHO (JPG o PNG)<input required type="file" accept="image/jpeg,image/png,.jpg,.jpeg,.png" name="report_file"></label><label>Fecha y hora de la medicion<input type="datetime-local" name="captured_at" value="{now}"></label><div class="dialog-actions"><button class="cancel" type="button" data-close>Cancelar</button><button class="submit" type="submit">Leer, guardar y actualizar</button></div></form></div></dialog>
<dialog id="pdf"><div class="dialog-body"><h2>Descargar PDF</h2><p>Incluye las mismas secciones, indicadores, tablas y graficas del informe seleccionado. Tambien se guardara en el NAS e iniciara la descarga.</p><form method="post" action="/pdf"><input id="pdf-report-name" type="hidden" name="report_name" value="healthhub-semanal.html"><label>Carpeta en el NAS (ruta relativa)<input name="folder" value="semanal" placeholder="Ej. semana-47"></label><label>Nombre del PDF<input name="filename" value="{pdf_name}"></label><div class="dialog-actions"><button class="cancel" type="button" data-close>Cancelar</button><button class="submit" type="submit">Generar y descargar</button></div></form></div></dialog>
<script>
const status=document.getElementById('refresh-status'),frame=document.getElementById('report-frame'),reportSelect=document.getElementById('report-select'),openReport=document.getElementById('open-report'),pdfReportName=document.getElementById('pdf-report-name');
function selectedReport(){{return '/report/'+reportSelect.value;}}function showSelectedReport(){{const url=selectedReport();frame.src=url+'?v='+Date.now();openReport.href=url;pdfReportName.value=reportSelect.value;}}
reportSelect.addEventListener('change',showSelectedReport);
document.querySelectorAll('[data-dialog]').forEach(button=>button.addEventListener('click',()=>document.getElementById(button.dataset.dialog).showModal()));
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
async function refresh(){{const response=await fetch('/refresh',{{method:'POST'}});if(!response.ok)throw Error('refresh');setTimeout(()=>{{showSelectedReport();status.textContent='Informe actualizado.';}},18000);}}
document.getElementById('refresh-report').addEventListener('click',async event=>{{event.currentTarget.disabled=true;status.textContent='Actualizacion solicitada. Se recargaran los datos al terminar.';try{{await refresh();}}catch(_){{status.textContent='No se pudo solicitar la actualizacion.';}}finally{{setTimeout(()=>event.currentTarget.disabled=false,18000);}}}});
async function requestError(response){{const body=await response.json().catch(()=>null);return body?.detail||'No se pudo guardar la informacion.';}}
document.querySelectorAll('form[data-refresh-form]').forEach(form=>form.addEventListener('submit',async event=>{{event.preventDefault();const submit=form.querySelector('[type=submit]');submit.disabled=true;status.textContent='Guardando y regenerando informe...';try{{const response=await fetch(form.action,{{method:'POST',body:new FormData(form)}});if(!response.ok)throw Error(await requestError(response));form.closest('dialog').close();await refresh();}}catch(error){{status.textContent=error.message||'No se pudo guardar la informacion.';submit.disabled=false;}}}}));
</script></html>'''

@app.post("/insulin")
def insulin(at:str=Form(...),units:float=Form(...),kind:str=Form(...)):
    event("InsulinDoseRecord",datetime.fromisoformat(at).astimezone().isoformat(),{"units":units,"type":kind}); return RedirectResponse("/",303)
@app.post("/workout")
def workout(at:str=Form(...),minutes:int=Form(...),intensity:str=Form(...)):
    event("ExerciseSessionRecord",datetime.fromisoformat(at).astimezone().isoformat(),{"duration_minutes":minutes,"intensity":intensity}); return RedirectResponse("/",303)

@app.post("/refresh")
def refresh_report():
    """Solicita una regeneración sin crear ningún dato clínico nuevo."""
    REFRESH_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    REFRESH_REQUEST.touch()
    return {"accepted": True}

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

@app.post("/renpho/upload")
async def upload_renpho(report_file: UploadFile = File(...), captured_at: str = Form("")):
    """Recibe una captura RENPHO, la procesa con OCR local y actualiza el historico."""
    content = await report_file.read()
    if not content or len(content) > 15 * 1024 * 1024:
        raise HTTPException(413, "La imagen debe tener entre 1 byte y 15 MB.")
    try:
        image = Image.open(io.BytesIO(content))
        image.verify()
        image = Image.open(io.BytesIO(content))
        image.load()
    except Exception as error:
        raise HTTPException(400, "El archivo no es una imagen JPG o PNG valida.") from error
    if image.format not in {"JPEG", "PNG"}:
        raise HTTPException(400, "Solo se aceptan imagenes JPG o PNG.")

    try:
        when = datetime.fromisoformat(captured_at).astimezone().isoformat() if captured_at else datetime.now().astimezone().isoformat()
    except ValueError as error:
        raise HTTPException(400, "La fecha de la medicion no es valida.") from error

    RENPHO_UPLOADS.mkdir(parents=True, exist_ok=True)
    extension = ".jpg" if image.format == "JPEG" else ".png"
    saved_image = RENPHO_UPLOADS / f"renpho-{datetime.now().strftime('%Y%m%d-%H%M%S')}{extension}"
    temporary = saved_image.with_suffix(saved_image.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(saved_image)
    try:
        result = subprocess.run(
            ["tesseract", str(saved_image), "stdout", "-l", "eng", "--psm", "6"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        reading = parse_renpho_ocr(result.stdout, when)
        reading["source_filename"] = saved_image.name
        save_renpho_reading(reading)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(504, "El OCR local ha tardado demasiado.") from error
    except subprocess.CalledProcessError as error:
        raise HTTPException(422, "No se pudo leer texto de la captura RENPHO.") from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(422, "No se pudieron extraer valores RENPHO de la captura.") from error
    request_refresh()
    return {"accepted": True, "weight": reading["weight"], "captured_at": reading["captured_at"]}

@app.post("/pdf")
def download_pdf(folder: str = Form("semanal"), filename: str = Form("health-report.pdf"), report_name: str = Form("healthhub-semanal.html")):
    output_path = safe_pdf_path(folder, filename)
    build_pdf(output_path, report_name)
    return FileResponse(output_path, media_type="application/pdf", filename=output_path.name)

@app.get("/report/{name:path}")
def report(name:str):
    path=(REPORTS/name).resolve()
    if REPORTS not in path.parents or not path.is_file(): return HTMLResponse("Informe aún no generado",404)
    return FileResponse(path)
