# Despliegue de Health Report Generator v2.0

## 📋 Resumen de Cambios

El generador de informes ha sido completamente refactorizado de v1 (44 líneas, código minificado) a v2 (611 líneas, código estructurado) con las siguientes mejoras:

### Nuevas Características
- 📊 **Métricas glucémicas ampliadas**: Desviación estándar, CV%, hipoglucemias, hiperglucemias
- 📈 **Perfiles diarios**: Gráficos individuales de últimos 7 días con insulina y ejercicio
- ⚖️ **Composición RENPHO integrada**: Peso, grasa, masa muscular, agua
- 🎨 **Diseño responsive**: Interfaz moderna adaptada a móvil
- 📝 **Informes históricos**: Guardado con timestamp (año-semana)
- 🔍 **Mejor logging**: Seguimiento de errores y progreso

### Backward Compatibility
✅ Genera `healthhub-semanal.html` compatible con v1
✅ No requiere cambios en base de datos
✅ No requiere cambios en configuración de red

---

## 🚀 Pasos de Despliegue

### 1. Actualizar Repositorio

```bash
cd /path/to/Health\ Report
git pull origin master
```

### 2. Validar Docker Compose

```bash
docker compose --env-file .env -f compose/healthhub.yml config
```

Verifica que no hay errores de sintaxis.

### 3. Reconstruir Imagen Reporter

```bash
# Opción A: Desde Portainer (recomendado)
# 1. Abierto Portainer
# 2. Stacks → healthhub
# 3. Editor → Pull & re-deploy

# Opción B: Desde línea de comandos
cd /path/to/Health\ Report
docker build -t health-report-reporter:local ./reporter
```

**Cambios en Dockerfile:**
- Instala libfreetype6 y libpng-dev (requeridos por matplotlib)
- Crea directorio /reports explícitamente

### 4. Redeploy en Portainer

1. **Acceder a Portainer**
   - Stack: `healthhub`
   - Ir a "Editor"

2. **Pull & redeploy**
   - Botón "Pull & redeploy"
   - El healthhub-reporter se reiniciará con la nueva imagen

3. **Validar salud**
   ```bash
   # En contenedor
   docker ps | grep healthhub-reporter
   docker logs healthhub-reporter
   ```

### 5. Validar Primera Generación

El primer informe tardará más tiempo (genera 7 gráficos diarios adicionales).

**Monitorear logs:**
```bash
docker logs -f healthhub-reporter
```

**Esperado:**
```
2026-11-15 21:00:00 - INFO - Iniciando generación de informe...
2026-11-15 21:00:01 - INFO - Obteniendo datos de Nightscout...
2026-11-15 21:00:05 - INFO - Obteniendo eventos de base de datos...
2026-11-15 21:00:06 - INFO - Leyendo composición RENPHO...
2026-11-15 21:00:06 - INFO - Calculando métricas glucémicas...
2026-11-15 21:00:07 - INFO - Generando gráfico AGP...
2026-11-15 21:00:10 - INFO - Generando perfiles diarios...
2026-11-15 21:00:25 - INFO - Procesando eventos...
2026-11-15 21:00:26 - INFO - Generando HTML...
2026-11-15 21:00:27 - INFO - Informe semanal guardado...
2026-11-15 21:00:27 - INFO - Informe histórico guardado...
2026-11-15 21:00:27 - INFO - ✅ Informe generado exitosamente
```

### 6. Verificar Resultado en Portal

1. Abrir `https://reportHealth.fvegas.es/`
2. Pulsar "Abrir informe actualizado"
3. Verificar que:
   - ✅ Aparecen 6 cards de resumen glucémico (media, SD, CV%, TIR, hypo%, hyper%)
   - ✅ Gráfico AGP con mejor escala y etiquetas
   - ✅ Sección de "Perfiles Diarios" con 7 gráficos (si hay datos de 7 días)
   - ✅ Sección "⚖️ Composición Corporal" (si RENPHO disponible)
   - ✅ Tabla de actividad ahora con columnas "Insulina" y "Entrenamientos"
   - ✅ Estilos mejorados, responsive en móvil

### 7. Verificar Archivos Generados

En NAS (`Nightscout-Backups/healthhub/reports/`):

```
├── agp.png                              (gráfico AGP)
├── daily-2026-11-15.png                 (perfil diario)
├── daily-2026-11-14.png
├── ...
├── healthhub-semanal.html              (actual)
├── healthhub-semanal-2026-W46.html     (histórico con timestamp)
└── ultimo.html                         (copia del actual)
```

---

## ⚙️ Configuración Especial

### Variables de Entorno (sin cambios)
```env
TZ=Europe/Madrid
DATABASE_URL=postgresql://healthhub:PASSWORD@postgres:5432/healthhub
NIGHTSCOUT_URL=http://nightscout:1337
NIGHTSCOUT_API_SECRET=<your_secret>
```

### Ruta RENPHO (en Docker)
El generador busca composición en:
```
/backups/renpho/ultimo.json
```

**En docker-compose.yml:**
```yaml
reporter:
  volumes:
    - nightscout_backups:/backups:ro  # Acceso de solo lectura
```

El archivo JSON se genera automáticamente en `healthhub-postgres` con este formato:
```json
{
  "weight": 75.5,
  "fat_percentage": 22.3,
  "muscle_mass": 58.2,
  "water_percentage": 60.1,
  "timestamp": "2026-11-15T20:00:00"
}
```

---

## 🐛 Troubleshooting

### Error: "No module named 'matplotlib.dates'"

**Causa**: Versión vieja de matplotlib.

**Solución**:
```bash
docker build --no-cache -t health-report-reporter:local ./reporter
```

### Error: "ConnectionError" en Nightscout

**Causa**: Nightscout no responde o URL incorrecta.

**Solución**:
```bash
# Verificar desde dentro del contenedor
docker exec healthhub-reporter \
  curl -s http://nightscout:1337/api/v1/entries.json?count=1 | head
```

### Informe se ve incompleto

**Causa**: Datos faltantes de RENPHO o sin eventos.

**Solución**:
- Verificar que PostgreSQL tiene eventos: `SELECT COUNT(*) FROM health_events;`
- Verificar que `/backups/renpho/ultimo.json` existe
- Esperar a que Health Connect envíe datos

### Los perfiles diarios no aparecen

**Causa**: No hay 7 días de datos glucémicos.

**Solución**:
- Normale: esperado primeros días
- Verificar: `SELECT COUNT(*) FROM health_events WHERE event_type='StepsRecord';`

---

## 🔄 Rollback a v1 (si es necesario)

```bash
cd /path/to/Health\ Report/reporter
cp report.py.backup report.py
docker build -t health-report-reporter:local .
# Redeploy en Portainer
```

---

## 📊 Monitoreo Continuo

### Tamaño de /reports

Los gráficos diarios aumentan el tamaño:
```bash
du -sh /path/to/reports
```

Limpieza manual si es necesario (mantener últimos 30 días):
```bash
find /reports -name "daily-*.png" -mtime +30 -delete
```

### Frecuencia de generación

El script genera cada 5 minutos. Si necesitas cambiar:
```python
# En report.py, línea final
time.sleep(300)  # Cambiar a 600 para 10 minutos, etc.
```

---

## ✅ Checklist de Validación Post-Despliegue

- [ ] Container `healthhub-reporter` está `running`
- [ ] Logs no muestran errores
- [ ] Portal muestra informe sin errores 404
- [ ] Nuevas métricas (SD, CV%, hypo%, hyper%) visibles
- [ ] Perfiles diarios generados (si hay datos)
- [ ] RENPHO integrado (si JSON existe)
- [ ] Tabla de actividad incluye insulina/entrenamientos
- [ ] Archivos en NAS tienen timestamps correctos
- [ ] CSS responsive funciona en móvil (probar con DevTools)

---

## 📞 Soporte

Para problemas:
1. Revisar logs: `docker logs healthhub-reporter`
2. Verificar database: `docker exec healthhub-postgres psql -U healthhub -d healthhub -c "SELECT COUNT(*) FROM health_events;"`
3. Forzar generación manual: `docker exec healthhub-reporter python /app/report.py` (una vez)

