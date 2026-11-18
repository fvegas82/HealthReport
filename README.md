# Health Report

Infraestructura privada y reproducible para Nightscout, Health Connect, composición RENPHO e informes personales. Este repositorio es la fuente de verdad del código y la configuración de despliegue; Portainer debe desplegar desde él, no almacenar código embebido.

## Componentes

- `compose/nightscout.yml`: Nightscout, MongoDB, copias cifradas y generador glucémico.
- `compose/healthhub.yml`: PostgreSQL, API de ingesta, portal privado, generador y copias cifradas.
- `api/`: receptor de Health Connect con deduplicación por contenido.
- `portal/`: interfaz privada para consultar el informe y registrar contexto manual.
- `reporter/`: informe HTML de 14 días y gráficas de glucosa (v2.0 - completamente refactorizado).
- `android/`: Health Hub Bridge, aplicación Android que lee Health Connect.
- `docs/`: procedimientos de despliegue, recuperación y seguridad.

El estado operativo y el procedimiento de continuación más reciente están en
[docs/CONTINUIDAD_2026-11-18.md](docs/CONTINUIDAD_2026-11-18.md).

## Reporter v2.0 - Novedades

La versión 2.0 del generador de informes incluye:

- **Código refactorizado**: De 44 líneas minificadas a 611 líneas estructuradas y documentadas
- **8 métricas glucémicas**: Media, SD, CV%, TIR, hipoglucemias%, hiperglucemias%, conteo, eventos
- **7 perfiles diarios**: Gráficos con insulina (rojo) y entrenamientos (violeta)
- **Gráfico AGP**: Ambulatory Glucose Profile agregado
- **Composición corporal RENPHO**: Integración automática si está disponible
- **Diseño responsivo**: Funciona en móvil, tablet y escritorio
- **Histórico de informes**: Informes timestampeados (YYYY-WNN) para referencia histórica
- **Logging detallado**: Trazabilidad completa de ejecuciones

## Seguridad

No hay secretos ni datos de salud en Git. Copia `.env.example` a `.env`, rellénalo localmente y cárgalo como variables de entorno en Portainer. 

**Variables críticas** (nunca en Git):
- `HEALTHHUB_DB_PASSWORD`: Contraseña PostgreSQL
- `NIGHTSCOUT_API_SECRET`: Token Nightscout (⚠️ SENSIBLE)
- `HEALTHHUB_INGEST_KEY`: Clave de ingesta
- `HEALTHHUB_BACKUP_ENCRYPTION_PASSWORD`: Encriptación de backups

Cloudflare Tunnel y Cloudflare Access se administran fuera de este repositorio y son obligatorios para los endpoints públicos.

## Despliegue

### Quick Start
```bash
# Ver docker-compose.yml para deployment en Portainer
cat docker-compose.yml
```

### Documentación completa
Ver [DEPLOYMENT.md](DEPLOYMENT.md) para instrucciones paso a paso en Portainer.

### Validación local
Antes de modificar producción:
```bash
docker compose --env-file .env -f compose/healthhub.yml config
```

Revisar cambios en Git antes de hacer push.

## Actualizaciones

Los servicios `portal` y `reporter` clonan `master` al arrancar, pero no se
actualizan solos al hacer un `push`. Para activar una versión nueva hay que
redeplegar el stack `healthhub` en Portainer. El procedimiento seguro y la
versión de las variables `PORTAL_RELEASE` y `REPORTER_RELEASE` se describen en
[la guía de continuidad](docs/CONTINUIDAD_2026-11-18.md).

## Estado inicial importado

El puente Android incluye la corrección del 15/11/2026: las sincronizaciones automáticas cubren las últimas 3 horas; los 30 días son una reimportación manual para evitar superar el límite de WorkManager.
