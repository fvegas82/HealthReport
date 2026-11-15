# Health Report

Infraestructura privada y reproducible para Nightscout, Health Connect, composición RENPHO e informes personales. Este repositorio es la fuente de verdad del código y la configuración de despliegue; Portainer debe desplegar desde él, no almacenar código embebido.

## Componentes

- `compose/nightscout.yml`: Nightscout, MongoDB, copias cifradas y generador glucémico.
- `compose/healthhub.yml`: PostgreSQL, API de ingesta, portal privado, generador y copias cifradas.
- `api/`: receptor de Health Connect con deduplicación por contenido.
- `portal/`: interfaz privada para consultar el informe y registrar contexto manual.
- `reporter/`: informe HTML de 14 días y gráficas de glucosa.
- `android/`: Health Hub Bridge, aplicación Android que lee Health Connect.
- `docs/`: procedimientos de despliegue, recuperación y seguridad.

## Seguridad

No hay secretos ni datos de salud en Git. Copia `.env.example` a `.env`, rellénalo localmente y cárgalo como variables de entorno en Portainer. Cloudflare Tunnel y Cloudflare Access se administran fuera de este repositorio y son obligatorios para los endpoints públicos.

## Despliegue

Ver [docs/DEPLOY.md](docs/DEPLOY.md). Antes de modificar producción, validar `docker compose --env-file .env -f compose/healthhub.yml config` y revisar el cambio en Git.

## Estado inicial importado

El puente Android incluye la corrección del 15/11/2026: las sincronizaciones automáticas cubren las últimas 3 horas; los 30 días son una reimportación manual para evitar superar el límite de WorkManager.
