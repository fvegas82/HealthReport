# Despliegue y recuperación

## Preparación

1. Crear una copia local de `.env.example` llamada `.env` y completar los valores.
2. Crear o verificar la red externa del túnel de Cloudflare indicada por `CLOUDFLARE_TUNNEL_NETWORK`.
3. Verificar acceso NFS al NAS y las carpetas `Nightscout-Backups` y `Nightscout-Informes`.
4. Configurar Cloudflare Tunnel hacia `healthhub-api:8000` para la ingesta y `healthhub-portal:8080` para el portal. Proteger ambos con Access; la ingesta debe requerir una credencial de servicio.

## Portainer

Crear dos stacks desde repositorio Git, usando el mismo repositorio y estas rutas:

| Stack | Ruta de compose |
|---|---|
| `nightscout` | `compose/nightscout.yml` |
| `healthhub` | `compose/healthhub.yml` |

Configurar las variables de `.env` como variables de stack. No pegar secretos en el fichero Compose ni activar la opción de contenido estático si el stack procede de Git.

## Validación

```bash
docker compose --env-file .env -f compose/healthhub.yml config
docker compose --env-file .env -f compose/nightscout.yml config
```

Después del despliegue, PostgreSQL y MongoDB deben estar saludables. Abrir el portal y pulsar **Actualizar informe**. Para validar actividad, consultar la última fecha de `StepsRecord` en PostgreSQL antes de cambiar permisos de Android.

## Recuperación

Las copias son volcados lógicos cifrados en el NAS. Restaurar únicamente en un entorno detenido y después de crear una copia adicional. No borrar los volúmenes de producción para diagnosticar un fallo.
