from datetime import UTC, datetime, time, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, field_validator

# Tolerancia para relojes de celular levemente desfasados. Un registro offline
# nunca puede venir de "el futuro": eso corrompe el orden de la bitácora.
TOLERANCIA_RELOJ = timedelta(minutes=5)


def _validar_registrado_en(v: datetime | None) -> datetime | None:
    if v is not None and v > datetime.now(UTC) + TOLERANCIA_RELOJ:
        raise ValueError(
            "registrado_en no puede estar en el futuro (revisa la hora del dispositivo)"
        )
    return v


# --- Pacientes ---


class PatientBase(BaseModel):
    nombre: str
    fecha_nacimiento: datetime | None = None
    notas: str | None = None


class PatientCreate(PatientBase):
    pass


class Patient(PatientBase):
    id: UUID
    created_at: datetime


# --- Bitácora ---

EntrySource = Literal["text", "voice"]
EntryKind = Literal[
    "nota",
    "medicamento",
    "sintoma",
    "animo",
    "otro",
    "alimentacion",
    "ejercicio",
]


# Entrada (crear): `content` no vacío y `occurred_at` con zona horaria
# obligatoria. Sin zona, "15:30" es ambiguo (¿hora de Chile o UTC?) y la
# entrada podía quedar corrida horas en la bitácora.
# `client_id` (opcional, uuid generado por el cliente) hace el POST idempotente:
# reintentar desde la cola offline devuelve la misma entrada, no una duplicada.
# `registrado_en` es la hora en el dispositivo cuando se registró sin conexión.
class EntryCreate(BaseModel):
    kind: EntryKind
    content: str = Field(min_length=1)
    occurred_at: AwareDatetime
    source: EntrySource = "text"
    client_id: UUID | None = None
    registrado_en: AwareDatetime | None = None

    @field_validator("registrado_en")
    @classmethod
    def _registrado_no_futuro(cls, v: datetime | None) -> datetime | None:
        return _validar_registrado_en(v)


def _motivo_limpio(v: str) -> str:
    # Espacios no cuentan: "   " no es una justificación. Mismo criterio que el CHECK de la base.
    limpio = v.strip()
    if len(limpio) < 3:
        raise ValueError("El motivo debe tener al menos 3 caracteres (sin contar espacios)")
    return limpio


# Corrección de una entrada. Siempre trae motivo y el estado NUEVO completo:
# la versión anterior queda en la revisión previa (o en la entrada original).
class EntryRevisionEditar(BaseModel):
    motivo: str = Field(min_length=1, max_length=500)
    kind: EntryKind
    content: str = Field(min_length=1)
    occurred_at: AwareDatetime

    @field_validator("motivo")
    @classmethod
    def _valida_motivo(cls, v: str) -> str:
        return _motivo_limpio(v)

    @field_validator("content")
    @classmethod
    def _content_no_vacio(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content no puede estar vacío")
        return v


# Anulación: la entrada sigue visible en el historial, marcada como anulada.
class EntryAnulacion(BaseModel):
    motivo: str = Field(min_length=1, max_length=500)

    @field_validator("motivo")
    @classmethod
    def _valida_motivo(cls, v: str) -> str:
        return _motivo_limpio(v)


# Revisión tal como se guarda (append-only). Sin content en audit_log, pero
# sí aquí: el historial es el dato que el grupo necesita ver.
class EntryRevision(BaseModel):
    id: UUID
    entry_id: UUID
    patient_id: UUID
    author_id: UUID
    accion: Literal["editar", "anular"]
    motivo: str
    kind: EntryKind | None = None
    content: str | None = None
    occurred_at: datetime | None = None
    created_at: datetime


# Respuesta: sin las restricciones de entrada, para no fallar con filas
# históricas (p. ej. content vacío guardado antes de esta validación).
# `anulada`/`editada` vienen de la vista log_entries_vigentes; son None cuando
# la fila es el registro original sin estado (p. ej. exportación ARCO).
class Entry(BaseModel):
    kind: EntryKind
    content: str
    occurred_at: datetime
    source: EntrySource = "text"
    id: UUID
    patient_id: UUID
    author_id: UUID
    created_at: datetime
    registrado_en: datetime | None = None
    anulada: bool | None = None
    editada: bool | None = None


# --- Medicamentos ---


class MedicationBase(BaseModel):
    nombre: str
    dosis: str | None = None
    frecuencia_horas: int | None = Field(default=None, gt=0)
    horarios: list[time] = Field(default_factory=list)
    notas: str | None = None


class MedicationCreate(MedicationBase):
    pass


class MedicationUpdate(BaseModel):
    nombre: str | None = None
    dosis: str | None = None
    frecuencia_horas: int | None = Field(default=None, gt=0)
    horarios: list[time] | None = None
    notas: str | None = None
    activo: bool | None = None


class Medication(MedicationBase):
    id: UUID
    patient_id: UUID
    activo: bool = True


# --- Dosis (adherencia) ---

DoseStatus = Literal["tomada", "omitida", "postpuesta"]


class MedicationDoseCreate(BaseModel):
    scheduled_for: datetime
    taken_at: datetime | None = None
    status: DoseStatus
    notas: str | None = None
    # Offline: mismo contrato que EntryCreate (idempotencia y hora de dispositivo).
    client_id: UUID | None = None
    registrado_en: AwareDatetime | None = None

    @field_validator("registrado_en")
    @classmethod
    def _registrado_no_futuro(cls, v: datetime | None) -> datetime | None:
        return _validar_registrado_en(v)


class MedicationDose(BaseModel):
    id: UUID
    patient_id: UUID
    medication_id: UUID
    author_id: UUID
    scheduled_for: datetime
    taken_at: datetime | None = None
    status: DoseStatus
    notas: str | None = None
    created_at: datetime
    registrado_en: datetime | None = None


class UpcomingDose(BaseModel):
    """Próxima dosis proyectada desde `medications.horarios`. La usa el cliente
    para agendar notificaciones locales (expo-notifications)."""

    medication_id: UUID
    nombre: str
    dosis: str | None = None
    scheduled_for: datetime


class AdherenceReport(BaseModel):
    """Adherencia calculada sobre dosis registradas en el rango. No proyecta
    dosis esperadas: si la familia no registró, no cuenta como omisión."""

    desde: datetime
    hasta: datetime
    total_dosis_registradas: int
    tomadas: int
    omitidas: int
    postpuestas: int
    porcentaje_adherencia: float


# --- Asistente IA ---


class AssistantQuery(BaseModel):
    pregunta: str = Field(min_length=3)
    ventana_dias: int = 30
    # Opcional: continúa una conversación existente. Si no viene, el POST
    # inicia una nueva conversación.
    conversation_id: UUID | None = None


class AssistantAnswer(BaseModel):
    respuesta: str
    cited_entry_ids: list[UUID]
    conversation_id: UUID


class AssistantMessage(BaseModel):
    id: UUID
    patient_id: UUID
    author_id: UUID
    conversation_id: UUID
    pregunta: str
    respuesta: str
    cited_entry_ids: list[UUID]
    created_at: datetime


class ConversationSummary(BaseModel):
    """Resumen para el listado. Sin `respuesta` para no cargar el payload."""

    conversation_id: UUID
    primera_pregunta: str
    inicio: datetime
    ultimo: datetime
    total_mensajes: int


# --- Resúmenes ---


class Summary(BaseModel):
    id: UUID
    patient_id: UUID
    desde: datetime
    hasta: datetime
    contenido: str
    created_at: datetime


# --- Métricas ---


class VentanaTemporal(BaseModel):
    desde: datetime
    hasta: datetime


class RelevoMetric(BaseModel):
    """Relevo familiar (KPI comprometido: >60% de pacientes con ≥2 autores/semana)."""

    ventana: VentanaTemporal
    total_entries: int
    autores_distintos: int
    cumple_kpi: bool  # True si autores_distintos >= 2


# --- Derechos ARCO (Ley 21.719) ---


class MembershipExport(BaseModel):
    patient_id: UUID
    role: str
    created_at: datetime


class DataExport(BaseModel):
    """Todo lo que el sistema tiene del usuario autenticado (derecho de acceso
    y portabilidad, Ley 21.719). Formato estable para el front."""

    exportado_en: datetime
    usuario: dict  # {id, email}
    membresias: list[MembershipExport]
    # Registros originales tal como se escribieron (sin estado: anulada/editada = None).
    entradas_bitacora: list[Entry]
    # Todas las revisiones y anulaciones hechas por el usuario. Junto con las
    # originales permiten reconstruir qué se escribió, cuándo y por qué cambió.
    revisiones_bitacora: list[EntryRevision]
    mensajes_asistente: list[AssistantMessage]
    pacientes_creados: list[Patient]


# --- Centros / farmacias (tablas públicas pobladas por scrapers) ---


class Center(BaseModel):
    id: UUID
    nombre: str
    tipo: str
    region: str
    comuna: str | None = None
    direccion: str | None = None
    telefono: str | None = None


# --- Perfiles de cuidadores ---


CareRole = Literal["admin", "caregiver", "viewer"]


class ProfileUpdate(BaseModel):
    nombre: str | None = None
    telefono: str | None = None


class Profile(BaseModel):
    id: UUID
    nombre: str | None = None
    telefono: str | None = None
    updated_at: datetime


# --- Red de cuidado (care_members) ---


class CareMember(BaseModel):
    """Miembro del grupo de cuidado tal como lo expone `list_care_members`."""

    user_id: UUID
    email: str
    role: CareRole
    nombre: str | None = None
    telefono: str | None = None
    joined_at: datetime


class MemberInvite(BaseModel):
    email: str
    role: CareRole = "caregiver"
