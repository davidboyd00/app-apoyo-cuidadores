"""Cliente Supabase falso para tests de endpoints.

Registra cada operación (tabla, operación, payload, filtros) y devuelve las
respuestas encoladas por (tabla, operación). Sin respuesta encolada devuelve
lista vacía, como PostgREST cuando RLS no deja ver nada.
"""

from __future__ import annotations

from types import SimpleNamespace


class _Consulta:
    def __init__(self, db: DBFalsa, tabla: str, op: str = "select", payload=None):
        self._db = db
        self.tabla = tabla
        self.op = op
        self.payload = payload
        self.filtros: list[tuple[str, tuple]] = []

    def __getattr__(self, nombre):
        def metodo(*args, **kwargs):
            if nombre in ("insert", "update", "upsert"):
                self.op = nombre
                self.payload = args[0] if args else kwargs
            elif nombre == "delete":
                self.op = "delete"
            elif nombre != "select":
                self.filtros.append((nombre, args))
            return self

        return metodo

    def execute(self):
        self._db.llamadas.append(self)
        cola = self._db.respuestas.get((self.tabla, self.op), [])
        return SimpleNamespace(data=cola.pop(0) if cola else [])


class DBFalsa:
    def __init__(self):
        self.respuestas: dict[tuple[str, str], list] = {}
        self.llamadas: list[_Consulta] = []

    def responder(self, tabla: str, op: str, *datos) -> DBFalsa:
        self.respuestas.setdefault((tabla, op), []).extend(datos)
        return self

    def table(self, nombre: str) -> _Consulta:
        return _Consulta(self, nombre)

    def rpc(self, nombre: str, params=None) -> _Consulta:
        return _Consulta(self, f"rpc:{nombre}", "rpc", params)

    def operaciones(self, tabla: str, op: str) -> list[_Consulta]:
        return [c for c in self.llamadas if c.tabla == tabla and c.op == op]
