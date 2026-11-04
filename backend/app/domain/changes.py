"""Canonical Agent transaction boundary; validation and apply share one contract."""
from app.services.agent_crud import prepare_changes, apply_changes, canonical_digest, exact_edit
