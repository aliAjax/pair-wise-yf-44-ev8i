from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _impacted_unit_ids(data):
    ids = []
    for entry in data.get("impacted_units") or []:
        unit_id = entry.get("unit_id") if isinstance(entry, dict) else entry
        if unit_id and unit_id not in ids:
            ids.append(unit_id)
    if not ids and data.get("unit_id"):
        ids.append(data["unit_id"])
    return ids


def _snapshot_units(lookup, unit_ids):
    snapshots = []
    for unit_id in unit_ids:
        unit = _find_one(lookup, "unit", "id", unit_id)
        if not unit:
            raise ValidationError("impacted unit does not exist: " + str(unit_id))
        snapshots.append(
            {
                "unit_id": unit["id"],
                "unit_name": unit["data"].get("name", unit["id"]),
                "status_at_registration": unit["status"],
            }
        )
    return snapshots


def _invalidation_record(reason, actor, message, unit=None, extra=None):
    record = {
        "reason": reason,
        "message": message,
        "by": actor.user_id,
        "at": _now_iso(),
    }
    if unit:
        record["unit_id"] = unit["id"]
        record["unit_name"] = unit["data"].get("name", unit["id"])
    if extra:
        record.update(extra)
    return record


def _apply_invalidation(entity, patch, record):
    history = list(entity["data"].get("invalidations") or [])
    history.append(record)
    patch["invalidations"] = history
    patch["review_invalidated"] = record
    patch["approvals"] = []
    patch["approved_by"] = ""
    patch["permit_id"] = ""


def _validate_change(actor, data, lookup):
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")
    unit_ids = _impacted_unit_ids(data)
    if not unit_ids:
        raise ValidationError("impacted_units is required")
    data["impacted_units"] = _snapshot_units(lookup, unit_ids)


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _validate_assess(actor, entity, data, lookup):
    required = required_approval_level(data.get("risk_level"))
    patch = {"required_approvals": required}
    if entity["status"] in ("approved", "implemented"):
        previous = int(entity["data"].get("required_approvals", 1))
        if required > previous:
            patch["_next_status"] = "assessed"
            record = _invalidation_record(
                "risk_level_raised",
                actor,
                "风险等级由 %s 调高为 %s，原评审作废"
                % (entity["data"].get("risk_level", "?"), data.get("risk_level")),
            )
            _apply_invalidation(entity, patch, record)
        else:
            patch["_next_status"] = entity["status"]
    return patch


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    stale = []
    for snap in entity["data"].get("impacted_units") or []:
        unit = _find_one(lookup, "unit", "id", snap.get("unit_id"))
        if unit and unit["status"] != snap.get("status_at_registration"):
            stale.append(
                "%s(登记时 %s，当前 %s)"
                % (
                    unit["data"].get("name", unit["id"]),
                    snap.get("status_at_registration"),
                    unit["status"],
                )
            )
    if stale:
        raise ValidationError(
            "impacted unit status changed since registration: " + ", ".join(stale)
        )
    return {"approved_by": actor.user_id, "review_invalidated": None}


def _validate_update_scope(actor, entity, data, lookup):
    unit_ids = _impacted_unit_ids(data)
    if not unit_ids:
        raise ValidationError("impacted_units is required")
    snapshots = _snapshot_units(lookup, unit_ids)
    patch = {"impacted_units": snapshots}
    old_ids = [s.get("unit_id") for s in entity["data"].get("impacted_units") or []]
    new_ids = [s["unit_id"] for s in snapshots]
    added = [u for u in new_ids if u not in old_ids]
    removed = [u for u in old_ids if u not in new_ids]
    if entity["status"] in ("approved", "implemented") and (added or removed):
        patch["_next_status"] = "assessed"
        record = _invalidation_record(
            "scope_changed",
            actor,
            "影响装置发生增删，原评审作废",
            extra={"added_units": added, "removed_units": removed},
        )
        _apply_invalidation(entity, patch, record)
    else:
        patch["_next_status"] = entity["status"]
    return patch


def _validate_invalidate_review(actor, entity, data, lookup):
    unit = None
    if data.get("unit_id"):
        unit = _find_one(lookup, "unit", "id", data["unit_id"])
    unit_status = data.get("unit_status") or (unit["status"] if unit else "")
    reason = "unit_frozen" if unit_status == "frozen" else "unit_shutdown"
    if unit:
        name = unit["data"].get("name", unit["id"])
    else:
        name = data.get("unit_id") or "未知装置"
    default = "影响装置 %s 已%s，变更退回待评审" % (
        name,
        "冻结" if reason == "unit_frozen" else "停机",
    )
    record = _invalidation_record(reason, actor, data.get("note") or default, unit)
    if not unit and data.get("unit_id"):
        record["unit_id"] = data["unit_id"]
    patch = {}
    _apply_invalidation(entity, patch, record)
    return patch


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


CUSTOM_CREATE = {'change': _validate_change}
CUSTOM_TRANSITIONS = {('change', 'assess'): _validate_assess, ('change', 'approve'): _validate_approve, ('change', 'update_scope'): _validate_update_scope, ('change', 'invalidate_review'): _validate_invalidate_review, ('change', 'commission'): _validate_commission}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft', 'assessed', 'approved', 'implemented'), 'assessed'), 'update_scope': (('draft', 'assessed', 'approved', 'implemented'), 'assessed'), 'invalidate_review': (('approved', 'implemented'), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('description',), 'action_item': ('change_id', 'description', 'owner')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'update_scope'): ('impacted_units',), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'update_scope': ('admin', 'engineer'), ('change', 'invalidate_review'): ('admin', 'operator', 'safety'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            next_status = extra.pop("_next_status", next_status)
            patch.update(extra)
        return next_status, patch


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
