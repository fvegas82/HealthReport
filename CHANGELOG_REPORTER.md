# Changelog - Health Report Generator

## [v2.0] - 2026-11-15 - Refactor Completo y Mejoras Mayores

### 🎨 Mejoras de Diseño
- **CSS completamente rediseñado**: Responsive design para móvil
- **Header mejorado**: Gradiente visual y mejor legibilidad
- **Cards de resumen**: Diseño moderno con sombras y bordes
- **Tabla de actividad**: Mejor formateo y estilos hover
- **Iconos y emojis**: Mejor visualización de secciones

### 📊 Nuevas Métricas Glucémicas
- **Desviación estándar (SD)**: Variabilidad absoluta
- **Coeficiente de variación (CV%)**: Variabilidad relativa
- **Hipoglucemias (<70)**: % de tiempo por debajo de seguro
- **Hiperglucemias (>180)**: % de tiempo por encima de rango
- **Recuento de lecturas**: Total de muestras del período

### 📈 Perfiles Diarios
- **Gráficos por día**: Últimos 7 días con detalles
- **Eventos integrados**: 
  - Insulina marcada en rojo (---)
  - Entrenamientos marcados en violeta (---)
- **Mejor escala**: Eje X con horas, mejor resolución

### 🏗️ Refactorización de Código
- **44 líneas → 611 líneas**: Código estructurado y legible
- **Funciones reutilizables**: Cada tarea en su propia función
- **Gestión de errores**: Try/except en cada operación
- **Logging mejorado**: Seguimiento de progreso y errores
- **Docstrings**: Documentación de cada función
- **Configuración centralizada**: Variables al inicio

### 🔍 Nuevas Funcionalidades
- **Composición corporal RENPHO**: Integración con OCR local
  - Peso, grasa corporal, masa muscular, agua
  - Últimos valores disponibles
- **Insulina y entrenamientos**: Mostrados en tabla de actividad
- **Informe histórico**: Guarda con timestamp (año-semana)
- **Datos manuales del portal**: Integrados en análisis

### 🐛 Mejoras de Robustez
- **Validación de datos**: Manejo de valores faltantes
- **Timeouts mejorados**: Mejor control de errores Nightscout
- **Recuperación de fallos**: Continúa si falta RENPHO
- **Logging estructurado**: Mensajes informativos en cada paso

### 🔐 Seguridad
- Sin cambios en seguridad (heredada del diseño original)
- Continúa sin exponer credenciales
- Datos locales en /reports

### 📋 Estructura del Informe
```
1. Header
2. Resumen glucémico (6 cards)
3. Gráfico AGP mejorado
4. Perfiles diarios (últimos 7 días)
5. Composición corporal (si disponible)
6. Tabla de actividad diaria
7. Calidad de datos
8. Footer
```

### ⚙️ Cambios Técnicos
- Nuevas dependencias: `Pillow==11.0.0`, `jinja2==3.1.4`
- Python 3.12 en Docker
- Mejor gestión de matplotlib (Agg backend)
- Timeouts y manejo de conexión mejorados

### 📝 Backward Compatibility
- Genera `healthhub-semanal.html` igual que antes
- Añade `healthhub-semanal-YYYY-WWW.html` (histórico)
- Mantiene `ultimo.html` (último generado)
- Base de datos sin cambios

### 🚀 Próximas Mejoras Sugeridas
- [ ] Mensual integrado (últimos 30 días)
- [ ] Comparativa semana a semana
- [ ] Exportar a PDF
- [ ] API REST para consultar datos
- [ ] Gráfico de tendencia de peso
- [ ] Alertas de patrones anómalos
- [ ] Tema oscuro

### 🔄 Migración
1. Actualizar `requirements.txt` en repositorio
2. Reconstruir imagen: `docker build -t health-report-reporter:local .`
3. Redeploy en Portainer (stack healthhub)
4. Primer informe tardará un poco más (genera perfiles)
5. Verificar que `/reports` tiene espacio (adicional para gráficos)
