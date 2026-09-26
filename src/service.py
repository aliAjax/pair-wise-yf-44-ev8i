from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import (
    POST_APPROVAL_STATUSES,
    UNIT_DOWN_STATUSES,
    RuleEngine,
    affected_unit_ids,
    unit_block,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        cleaned = self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(cleaned.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, cleaned, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if updated["kind"] == "unit":
            self._sync_affected_changes(actor, updated)
        return updated

    def _affected_changes(self, unit_id):
        changes = self.repository.list_entities(kind="change")
        return [
            change
            for change in changes
            if unit_id in affected_unit_ids(change["data"])
        ]

    def _sync_affected_changes(self, actor, unit):
        """A unit status change must be reflected on every change that lists it.

        shutdown/freeze: approved/implemented changes fall back to assessed and
        carry a block naming the unit; startup/unfreeze: the unit's active
        blocks are removed (re-approval is still required afterwards).
        """
        if unit["status"] in UNIT_DOWN_STATUSES:
            for change in self._affected_changes(unit["id"]):
                if change["status"] not in POST_APPROVAL_STATUSES:
                    continue
                block = unit_block(unit)
                blocks = [
                    existing
                    for existing in change["data"].get("blocks", [])
                    if existing.get("unit_id") != unit["id"]
                ]
                blocks.append(block)
                merged = dict(change["data"])
                merged["blocks"] = blocks
                merged["void_reason"] = {
                    "code": "affected_unit_" + unit["status"],
                    "message": block["message"],
                    "at": block["detected_at"],
                    "by": actor.user_id,
                }
                updated = self.repository.update_entity(
                    change["id"], change["version"], "assessed", merged
                )
                self.audit.record(
                    change["id"],
                    actor,
                    "auto_return",
                    change["status"],
                    "assessed",
                    {"block": block},
                )
        else:
            for change in self._affected_changes(unit["id"]):
                blocks = [
                    block
                    for block in change["data"].get("blocks", [])
                    if block.get("unit_id") != unit["id"]
                ]
                if len(blocks) == len(change["data"].get("blocks", [])):
                    continue
                merged = dict(change["data"])
                merged["blocks"] = blocks
                updated = self.repository.update_entity(
                    change["id"], change["version"], change["status"], merged
                )
                self.audit.record(
                    change["id"],
                    actor,
                    "block_cleared",
                    change["status"],
                    change["status"],
                    {"unit_id": unit["id"], "unit_status": unit["status"]},
                )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
