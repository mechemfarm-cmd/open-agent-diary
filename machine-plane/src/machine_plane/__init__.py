"""Agent Diary machine plane — portable record, per-platform producers.

The format is universal; the sensor is not. This package holds the parts that
are the same everywhere: the event record, neutral identity, capability and
coverage reporting, producer-side redaction, and the protected ingestion
boundary.

It deliberately does not import Agent Diary internals and does not write to the
Diary database. Platform sensors live in separate producer packages.
"""

from .capabilities import Capability, CapabilityReport, CapabilitySet
from .chain import (
    CHAIN_VERSION,
    GENESIS,
    ChainLink,
    Checkpoint,
    HashChain,
    IntegrityStatus,
    VerifyResult,
    verify,
)
from .correlation import (
    ACTION_ALIASES,
    INVESTIGABLE,
    AgentClaim,
    CorrelatedRecord,
    CorrelationReport,
    CorrelationStatus,
    CoverageMap,
    RunRegistration,
    RunRegistry,
    correlate,
)
from .events import (
    ACTION_EVENT_TYPES,
    GAP_EVENT_TYPES,
    PATH_EVENT_TYPES,
    SCHEMA_VERSION,
    CoverageState,
    EventKind,
    EventType,
    MachineEvent,
    PrivacyMode,
    SchemaMismatch,
    event_kind,
)
from .identity import (
    DomainType,
    IdentityConfidence,
    IsolationDomain,
    ProcessIdentity,
)
from .importer import Importer, ImportResult, MemoryStore, Store
from .privacy import (
    DEFAULT_EXCLUDED_FRAGMENTS,
    FORBIDDEN_CONTENT_KEYS,
    PathDisplay,
    PrivacyPolicy,
    RedactionResult,
)
from .producer import Producer, RawEvent, Sink
from .spool import SPOOL_VERSION, Spool, SpoolEnvelope, SpoolError
from .view import RANKING, View, ViewEntry, build_view, describe

__all__ = [
    "ACTION_ALIASES",
    "ACTION_EVENT_TYPES",
    "AgentClaim",
    "CHAIN_VERSION",
    "Capability",
    "CapabilityReport",
    "CapabilitySet",
    "ChainLink",
    "Checkpoint",
    "GENESIS",
    "HashChain",
    "IntegrityStatus",
    "VerifyResult",
    "verify",
    "CorrelatedRecord",
    "CorrelationReport",
    "CorrelationStatus",
    "CoverageMap",
    "CoverageState",
    "DEFAULT_EXCLUDED_FRAGMENTS",
    "DomainType",
    "EventKind",
    "EventType",
    "FORBIDDEN_CONTENT_KEYS",
    "GAP_EVENT_TYPES",
    "INVESTIGABLE",
    "IdentityConfidence",
    "ImportResult",
    "Importer",
    "IsolationDomain",
    "MachineEvent",
    "MemoryStore",
    "PATH_EVENT_TYPES",
    "PathDisplay",
    "PrivacyMode",
    "PrivacyPolicy",
    "ProcessIdentity",
    "Producer",
    "RawEvent",
    "RedactionResult",
    "RunRegistration",
    "RunRegistry",
    "SCHEMA_VERSION",
    "SPOOL_VERSION",
    "SchemaMismatch",
    "Sink",
    "Spool",
    "SpoolEnvelope",
    "SpoolError",
    "RANKING",
    "Store",
    "View",
    "ViewEntry",
    "build_view",
    "correlate",
    "describe",
    "event_kind",
]

__version__ = "0.0.1"
