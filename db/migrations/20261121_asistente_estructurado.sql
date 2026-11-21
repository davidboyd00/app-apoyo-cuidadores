-- Descripción: asistente con salida estructurada: citas validadas, contexto visto y suficiencia.
--
-- Idempotente: se puede correr más de una vez.
-- Efecto en la API: POST /assistant/ask guarda en `cited_entry_ids` solo las
-- entradas que la respuesta cita (validadas contra el contexto), en
-- `contexto_entry_ids` todas las que vio el modelo y en `suficiente` si la
-- bitácora alcanzaba. Nuevo GET /metrics/asistente (KPI >90% verificable).

alter table assistant_messages add column if not exists contexto_entry_ids uuid[] not null default '{}';
alter table assistant_messages add column if not exists suficiente boolean;

-- Mensajes anteriores: lo que se guardaba en cited_entry_ids era el contexto completo.
update assistant_messages
   set contexto_entry_ids = cited_entry_ids
 where contexto_entry_ids = '{}' and cited_entry_ids <> '{}';

comment on column assistant_messages.cited_entry_ids is 'Entradas en que se basa la respuesta (citas del modelo validadas contra el contexto). Regla sagrada #4 · KPI >90% verificable.';
comment on column assistant_messages.contexto_entry_ids is 'Todas las entradas que vio el modelo como contexto. Regla sagrada #4 (auditoría de lo visto).';
comment on column assistant_messages.suficiente is 'False si el asistente declaró que la bitácora no alcanzaba para responder. Null en mensajes previos a la salida estructurada.';
