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

## Herramientas del backend (no tocan datos reales)

- `armar_paquete_migraciones.py` — arma `db/APLICAR_EN_SUPABASE.sql` con las
  migraciones pendientes en una sola transacción y una tabla de verificación.
  `tests/test_migraciones.py` comprueba que esté al día y que, aplicado sobre
  el schema de producción, deje la base igual a `db/schema.sql`.
- `probar_llm.py` — llama al modelo configurado con **datos ficticios** y mide
  latencia y formato. También se lanza desde GitHub → Actions → "Probar LLM"
  (sirve desde el celular). Útil cuando Google retira un modelo: se prueba el
  nuevo nombre y luego se cambia `GEMINI_MODEL` en Render.
