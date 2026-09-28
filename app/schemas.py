from datetime import datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

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
EntryKind = Literal["nota", "medicamento", "sintoma", "animo", "otro"]


class EntryCreate(BaseModel):
    kind: EntryKind
    content: str
    occurred_at: datetime
    source: EntrySource = "text"


class EntryUpdate(BaseModel):
    # Solo campos editables (no se puede cambiar author_id ni patient_id).
    kind: EntryKind | None = None
    content: str | None = None
    occurred_at: datetime | None = None


class Entry(EntryCreate):
    id: UUID
    patient_id: UUID
    author_id: UUID
    created_at: datetime


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
    entradas_bitacora: list[Entry]
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
