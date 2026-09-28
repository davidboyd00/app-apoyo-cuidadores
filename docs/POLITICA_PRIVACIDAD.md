# Política de privacidad — versión 2026-09-11

> **PENDIENTE JURÍDICO** — el texto real de la política debe ser redactado y
> revisado por el área legal / DPO. Este archivo es un placeholder que
> mantiene operativa la mecánica de consentimiento versionado. La versión
> declarada aquí y la constante `POLICY_VERSION` en `.env` deben coincidir
> cada vez que se publique una nueva versión.

Al usar la aplicación aceptas que:

1. Registramos entradas de bitácora sobre el estado del paciente que declaras
   cuidar, y las compartimos con los demás miembros del grupo de cuidado
   (según el rol asignado por el admin del grupo).
2. Tus entradas quedan asociadas a tu autoría; puedes ejercer tus derechos
   ARCO en cualquier momento (`GET /me/data-export`, `DELETE /me`).
3. Enviamos texto derivado de la bitácora a un proveedor de IA externo
   (Gemini/Claude) como contexto transitorio para responder consultas. No
   almacenamos datos del paciente en esos servicios.
4. Cambios importantes a esta política generarán una nueva versión y se te
   pedirá aceptarla antes de continuar usando la aplicación.
