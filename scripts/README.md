# scripts/

Jobs batch que corren por GitHub Actions cron. **Único lugar** del repo donde
se puede usar la `SUPABASE_SERVICE_ROLE_KEY`.

Escriben tablas públicas de solo-lectura (por ejemplo `centers`) y NUNCA
tocan datos clínicos. La API (`app/`) no depende de estos scripts.

**Excepción — `resumen_diario.py`** (backend dev, cron diario): lee la bitácora
vigente con la service role para generar resúmenes médicos sin usuario. Solo
escribe filas nuevas en `summaries`; no modifica entradas ni otras tablas. El
contexto va al LLM como en la API y no se persiste allá. Decisión registrada en
`docs/REGISTRO_TRATAMIENTO.md` (§ resumen diario). Cambia la regla de arriba
para este caso puntual.

Este archivo lo mantiene el integrante a cargo de scrapers (no el backend dev).

**Excepción — `retention.py`**: este script es del backend dev (David) porque
implementa cumplimiento de la Ley 21.719 (retención). Vive aquí por usar
`SUPABASE_SERVICE_ROLE_KEY`, no porque sea scraping.
