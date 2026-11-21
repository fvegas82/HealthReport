# Health Report: estado y guia de continuidad

Fecha de cierre: 18 de noviembre de 2026.

Este documento permite retomar el mantenimiento sin depender del historial del
chat. No contiene secretos, datos clinicos, contrasenas ni claves de API.

## Resultado actual

El portal privado `https://reporthealth.fvegas.es/` muestra informes integrados
de 14 y 30 dias. El selector de la cabecera cambia el periodo que se consulta.
El acceso externo se realiza mediante Cloudflare Tunnel y Cloudflare Access;
no hay puertos entrantes publicados para el portal.

El informe de 14 dias incluye:

- resumen glucemico, control y seguridad, comparativa con el periodo previo y
  patron por franja horaria;
- AGP, comparativa de dias y perfiles diarios ampliados, con dosis de insulina
  y entrenamientos registrados;
- composicion corporal RENPHO leida localmente mediante OCR y grafica de
  evolucion, sin incrustar la captura original;
- actividad diaria: pasos, distancia, energia, insulina y entrenamientos;
- continuidad historica de Clarity cuando existe la exportacion CSV local.

El informe de 30 dias anade las vistas de tendencia, estabilidad nocturna y
patron horario semanal cuando hay datos suficientes.

## Componentes en produccion

| Componente | Funcion | Notas operativas |
| --- | --- | --- |
| `healthhub-portal` | Portal web, carga de RENPHO y Clarity, insulina, entrenamientos y PDF | Python/FastAPI; clona el repositorio al arrancar en `/tmp/HealthReport-portal`. |
| `healthhub-reporter-v2` | Obtiene glucosa, integra eventos y genera HTML/graficas | Python; clona en `/tmp/HealthReport-reporter`. |
| PostgreSQL `healthhub` | Eventos de Health Connect y registros manuales | No borrar ni recrear para diagnosticar. |
| Nightscout + MongoDB | Fuente de glucosa actual | El reporter usa la URL interna de Docker. |
| Android Health Hub Bridge | Envia datos autorizados de Health Connect a la API | La app no lee datos privados de OHealth directamente. |
| Cloudflare Tunnel + Access | Publicacion privada del portal | Mantener el acceso limitado a la cuenta autorizada. |

Los dos servicios montan el volumen Docker externo
`nightscout_nightscout_backups` como `/reports`. Las salidas relevantes son:

```text
/reports/healthhub/reports/semanal/healthhub-semanal.html
/reports/healthhub/reports/semanal/healthhub-mensual.html
/reports/healthhub/reports/semanal/agp.png
/reports/healthhub/reports/semanal/daily-YYYY-MM-DD.png
/reports/healthhub/reports/semanal/pdf/<carpeta>/<nombre>.pdf
/reports/healthhub/reports/renpho/history.json
```

Las rutas son internas del contenedor. En el NAS se ven a traves del recurso
compartido de informes o copias, segun el montaje configurado en el homelab.

## Repositorio y versiones

- Repositorio: `https://github.com/fvegas82/HealthReport`
- Rama de produccion: `master`
- Directorio de trabajo local: `/Users/FVegas/Dev/Health Report`
- Ultimo cambio funcional documentado: `ea20273 fix(renpho): restore image upload dependencies`

Cambios recientes relevantes:

- `8c55561`: el PDF ahora exporta el contenido completo del HTML del informe,
  no solo AGP y perfiles diarios. Incluye indicadores, tablas, RENPHO,
  actividad, calidad de datos y graficas. El dialogo de PDF exporta el periodo
  elegido por el selector de 14/30 dias.
- `ea20273`: restaura `io` y Pillow (`PIL.Image`) en el portal. Son necesarios
  para validar y abrir la imagen RENPHO antes de ejecutar el OCR local. La
  omision se introdujo involuntariamente al preparar la exportacion PDF.
- `1e853c1`: los pasos diarios usan el total agregado de Health Connect
  (`DailyStepsTotal`) para evitar sumar fuentes solapadas.
- `9b36827`, `7985fb8`, `26be16c`: OCR y evolucion de RENPHO.

## Despliegue de una nueva version

Antes de desplegar, comprobar el cambio y publicarlo:

```bash
cd "/Users/FVegas/Dev/Health Report"
git status
python3 -m py_compile portal/app/main.py
git diff --check
git add <archivos>
git commit -m "tipo: descripcion"
git push origin master
```

En Portainer, abrir `Stacks > healthhub > Editor`. El stack actual no usa el
modo Git de Portainer: sus comandos clonan el repositorio durante el arranque.
Por eso un `push` por si solo no cambia los contenedores en marcha.

1. Para cambios de `portal/`, cambiar `PORTAL_RELEASE` al hash corto del nuevo
   commit. Para cambios de `reporter/`, cambiar `REPORTER_RELEASE`.
2. Conservar todas las demas lineas, en especial variables y volumenes.
3. Pulsar **Update the stack** y confirmar la actualizacion forzada.
4. Esperar a que el contenedor haya terminado de instalar dependencias y los
   registros indiquen `Uvicorn running` para el portal o la generacion del
   informe para el reporter.
5. Abrir el portal y hacer una recarga forzada si el navegador conserva una
   version anterior.

La version funcional actualmente desplegada del portal es `ea20273`.
El reporter conserva `1e853c1` porque el ultimo cambio fue exclusivo del
portal.

### Validacion posterior

1. En Portainer, comprobar `healthhub-portal` y `healthhub-reporter-v2` en
   estado `running`.
2. Abrir `https://reporthealth.fvegas.es/` y comprobar que el informe carga.
3. Alternar entre **Ultimos 14 dias** y **Ultimos 30 dias**.
4. Pulsar **Descargar PDF**, elegir una carpeta y nombre no destructivos, y
   confirmar que se descarga y queda una copia en `pdf/<carpeta>/` del volumen.
5. Verificar que el PDF contiene al menos: resumen glucemico, AGP, perfiles,
   composicion RENPHO, actividad diaria y calidad de datos.

La exportacion PDF usa el mismo HTML que se ve en pantalla; no consulta una
fuente clinica distinta ni envia datos a servicios externos.

## Operacion diaria

- **Actualizar informe** solicita una regeneracion; esperar aproximadamente
  unos segundos antes de recargar el iframe.
- **Anadir insulina** y **Registrar entrenamiento** guardan eventos en
  PostgreSQL y solicitan regenerar las graficas.
- **Cargar historicos** importa una exportacion CSV de Clarity al volumen
  privado. No subir ese CSV a GitHub.
- **Cargar RENPHO** almacena la imagen en el NAS, extrae las metricas mediante
  OCR local y actualiza `history.json`. No se publica la imagen en el informe.
- **Descargar PDF** guarda la copia en el NAS y la entrega al navegador.

## Datos de actividad y OHealth

Health Hub Bridge solo puede leer los tipos que Health Connect entrega a la
aplicacion con permiso. Los pasos se obtienen con el agregado diario de Health
Connect, respetando la prioridad de fuentes definida alli. Esto evita dobles
conteos, pero implica que el total debe coincidir con la fuente prioritaria de
Health Connect, no necesariamente con el que muestre OHealth por separado.

La integracion directa con el almacenamiento interno de OHealth no esta
implementada ni se recomienda: Android aísla los datos de cada aplicacion. Si
OHealth no publica sueno o entrenamientos en Health Connect, esos datos no
llegaran al informe. Revisar en OHealth la sincronizacion con Health Connect,
la prioridad de fuentes y los permisos del puente antes de cambiar codigo.

## Seguridad y recuperacion

- Nunca copiar secretos desde Portainer al repositorio, documentacion, chat o
  capturas. Las variables sensibles permanecen en el stack o en `.env` local.
- No borrar volumenes, base de datos ni historicos como medida de diagnostico.
- Antes de cualquier restauracion, crear una copia adicional de los backups
  cifrados del NAS.
- Mantener Cloudflare Access para el portal y no sustituirlo por una URL
  publica sin autenticacion.
- El fichero local `docker-compose.yml.bak`, si aparece como no rastreado,
  es una copia del usuario: no se debe incluir en commits ni eliminar sin
  indicacion expresa.

## Pendientes razonables

1. Confirmar desde la configuracion de OHealth/Health Connect que sueno y
   entrenamientos se estan compartiendo; despues comprobar los tipos recibidos
   por la API antes de ampliar el informe.
2. Comparar ocasionalmente el total diario de pasos de Health Connect con la
   fuente prioritaria. Si no coincide con OHealth, resolverlo en la prioridad
   de Health Connect, no sumando registros brutos en el reporter.
3. Si se retoma el envio por correo, usar una integracion OAuth especifica y
   una autorizacion separada; no guardar una contrasena de Gmail en Portainer.
