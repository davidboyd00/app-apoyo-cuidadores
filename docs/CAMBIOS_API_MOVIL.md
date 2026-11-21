# Cambios de la API para el cliente móvil (noviembre 2026)

Para Desarrollo Móvil. Todo está en `openapi.json` (regenerar tipos) y en
`/docs` de la API. Lo que **rompe** compatibilidad va primero.

## Rompe compatibilidad

1. **Ya no existen `PATCH` ni `DELETE /patients/{id}/entries/{entry_id}`**
   (historial inmutable, solicitud de cambio aceptada).
   - Corregir: `POST /patients/{id}/entries/{entry_id}/revisiones` con
     `{motivo, kind, content, occurred_at}` (estado nuevo completo; `motivo`
     de 3+ caracteres). Responde 201 con la entrada vigente.
   - Borrar: `POST /patients/{id}/entries/{entry_id}/anulacion` con
     `{motivo}`. Es definitivo; responde 201.
   - Solo el autor o un admin pueden hacerlo (RLS → 403/404).
2. **`GET /entries` oculta las anuladas** por defecto
   (`?incluir_anuladas=true` para verlas). Cada entrada trae `anulada`,
   `editada` y `registrado_en`.
3. **`POST /patients/{id}/summaries` responde 202** con el resumen en
   `estado: "generando"` y `contenido: null`. Seguirlo con
   `GET /summaries/{summary_id}` cada 3 s (o Realtime sobre `summaries`)
   hasta `listo` o `error`. Si hay uno `listo` de las últimas 24 h con el
   mismo período, responde **200** con ese (sin IA); `?forzar=true` pide otro.
4. **`cited_entry_ids` del asistente ahora son las entradas que la
   respuesta cita** (validadas), no todo el contexto. Puede venir vacío.

## Nuevo (no rompe)

- **Offline (bitácora y dosis):** `client_id` (uuid generado en el
  teléfono) y `registrado_en` (hora del teléfono) en
  `POST /entries` y `POST /medications/{mid}/doses`. Reintentar con el mismo
  `client_id` devuelve el mismo registro con **200** (no duplica).
  `registrado_en` no puede estar más de 5 min en el futuro.
  Reintentar ante: sin red, 5xx, 401, 409, 429. No reintentar ante 4xx de
  validación (mostrar el error).
- **Historial:** `GET /entries/{entry_id}/historial` →
  `{original, revisiones[]}` (versión original + cada corrección/anulación
  con autor, hora y motivo).
- **Asistente:** `AssistantAnswer.suficiente` (false = la bitácora no
  alcanza; mostrarlo). `AssistantMessage.suficiente` (null en mensajes
  antiguos). Si el proveedor falla: **503** con `detail` en español.
- **KPI asistente:** `GET /patients/{id}/metrics/asistente`.
- **Turnos de cuidado:** `GET /patients/{id}/turnos?desde&hasta` (ventana
  máx. 62 días; default ayer → +14 días), `POST /patients/{id}/turnos`
  `{cuidador_id?, inicio, fin}` (sin `cuidador_id` = yo; fin > inicio;
  máx. 7 días), `DELETE /patients/{id}/turnos/{turno_id}` (204).
  Caregiver solo se agenda a sí mismo; viewer no agenda (403).
- **ARCO:** `GET /me/data-export` agrega `revisiones_bitacora` y `turnos`.
- **Realtime:** publicadas `log_entry_revisions`, `summaries`, `care_shifts`.
- **Voz:** se mantiene `source: "voice"`; la transcripción es en el teléfono.

## Mensaje sugerido para el canal

> María Paz: la API de noviembre ya está en `main`. Lo que rompe:
> (1) editar/borrar entradas ahora es `POST .../revisiones` y
> `POST .../anulacion`, ambos con motivo; (2) `POST /summaries` responde
> 202 y hay que consultar hasta `listo`; (3) `cited_entry_ids` ahora son
> solo las entradas citadas. Nuevo: `client_id` + `registrado_en` para la
> cola offline (también en dosis), historial por entrada, turnos de cuidado
> y `suficiente` en el asistente. Detalle en `docs/CAMBIOS_API_MOVIL.md` y
> tipos en `openapi.json`. La web ya está adaptada si quieres mirar cómo
> quedó (`src/lib/sincronizacion.ts`, `src/hooks/use-api.ts`).
