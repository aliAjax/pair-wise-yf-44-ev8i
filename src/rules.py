from datetime import datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


RISK_LEVELS = ("low", "medium", "high", "critical")
RISK_RANK = {name: index + 1 for index, name in enumerate(RISK_LEVELS)}
UNIT_DOWN_STATUSES = {"shutdown": "停机", "frozen": "冻结"}
POST_APPROVAL_STATUSES = ("approved", "implemented")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def affected_unit_ids(data):
    ids = data.get("affected_unit_ids")
    if ids:
        return list(ids)
    single = data.get("unit_id")
    return [single] if single else []


def unit_block(unit):
    """Build the active block record for a shutdown/frozen affected unit."""
    status = unit["status"]
    name = unit["data"].get("name", unit["id"])
    word = UNIT_DOWN_STATUSES[status]
    return {
        "code": "unit_" + status,
        "unit_id": unit["id"],
        "unit_name": name,
        "unit_status": status,
        "message": "受影响装置 %s（%s）已%s，原评审作废，变更退回待评审"
        % (name, unit["id"], word),
        "detected_at": _now_iso(),
    }


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _live_unit_blocks(data, lookup):
    """Blocks computed from the units' current status (not stored state)."""
    if lookup is None:
        return []
    blocks = []
    for unit_id in affected_unit_ids(data):
        unit = _find_one(lookup, "unit", "id", unit_id)
        if unit and unit["status"] in UNIT_DOWN_STATUSES:
            blocks.append(unit_block(unit))
    return blocks


def _require_units_available(data, lookup, action_word):
    blocked = _live_unit_blocks(data, lookup)
    if blocked:
        raise ValidationError(
            "受影响装置当前不可用，无法%s：%s"
            % (action_word, "；".join(block["message"] for block in blocked))
        )


def _snapshot_units(raw_ids, lookup):
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValidationError("受影响装置不能为空，建单时至少选择一台装置")
    ids = []
    snapshots = []
    for unit_id in raw_ids:
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise ValidationError("受影响装置标识无效")
        if unit_id in ids:
            continue
        unit = _find_one(lookup, "unit", "id", unit_id)
        if not unit:
            raise ValidationError("unit does not exist: " + unit_id)
        ids.append(unit_id)
        snapshots.append(
            {
                "unit_id": unit_id,
                "unit_name": unit["data"].get("name", unit_id),
                "status": unit["status"],
                "captured_at": _now_iso(),
            }
        )
    return ids, snapshots


def _validate_change(actor, data, lookup):
    if not str(data.get("description", "")).strip():
        raise ValidationError("change description is required")
    raw_ids = data.get("affected_unit_ids")
    if raw_ids is None and data.get("unit_id"):
        raw_ids = [data["unit_id"]]
    ids, snapshots = _snapshot_units(raw_ids, lookup)
    data["affected_unit_ids"] = ids
    data["unit_id"] = ids[0]
    data["affected_units"] = snapshots


def _validate_assess(actor, entity, data, lookup):
    level = str(data.get("risk_level", "")).lower()
    if level not in RISK_RANK:
        raise ValidationError("unknown risk level: " + str(data.get("risk_level")))
    return {"required_approvals": required_approval_level(level)}


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    _require_units_available(entity["data"], lookup, "通过评审")
    return {"approved_by": actor.user_id, "blocks": [], "void_reason": None}


def _validate_implement(actor, entity, data, lookup):
    _require_units_available(entity["data"], lookup, "实施变更")
    return {}


def _validate_commission(actor, entity, data, lookup):
    if lookup:
        items = lookup("action_item", "change_id", entity["id"]) or []
        unresolved = [item["id"] for item in items if item["status"] != "verified"]
        if unresolved:
            raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    _require_units_available(entity["data"], lookup, "投产")
    return {"commissioned_by": actor.user_id}


def _validate_revise(actor, entity, data, lookup):
    current = entity["data"]
    patch = {}
    trigger_messages = []

    if "description" in data:
        if not str(data["description"]).strip():
            raise ValidationError("change description is required")
        patch["description"] = data["description"]

    if "affected_unit_ids" in data:
        ids, snapshots = _snapshot_units(data["affected_unit_ids"], lookup)
        old_ids = affected_unit_ids(current)
        added = [unit_id for unit_id in ids if unit_id not in old_ids]
        removed = [unit_id for unit_id in old_ids if unit_id not in ids]
        if added or removed:
            prior = {item["unit_id"]: item.get("unit_name", item["unit_id"])
                     for item in current.get("affected_units", [])}
            fresh = {item["unit_id"]: item["unit_name"] for item in snapshots}
            added_names = [fresh.get(unit_id, unit_id) for unit_id in added]
            removed_names = [prior.get(unit_id, unit_id) for unit_id in removed]
            trigger_messages.append(
                "影响装置发生变更（新增：%s；移除：%s），原评审作废"
                % ("、".join(added_names) or "无", "、".join(removed_names) or "无")
            )
        patch["affected_unit_ids"] = ids
        patch["unit_id"] = ids[0]
        patch["affected_units"] = snapshots
        # Recompute active blocks against the new scope; keep original
        # detection time for units that were already blocking.
        existing = {block["unit_id"]: block
                    for block in current.get("blocks", [])}
        rescoped = []
        for block in _live_unit_blocks({"affected_unit_ids": ids}, lookup):
            rescoped.append(existing.get(block["unit_id"], block))
        patch["blocks"] = rescoped

    if "risk_level" in data:
        level = str(data["risk_level"]).lower()
        if level not in RISK_RANK:
            raise ValidationError("unknown risk level: " + str(data["risk_level"]))
        old_level = str(current.get("risk_level", "")).lower()
        patch["risk_level"] = level
        patch["required_approvals"] = required_approval_level(level)
        if RISK_RANK.get(level, 0) > RISK_RANK.get(old_level, 0):
            trigger_messages.append(
                "风险等级由 %s 调高为 %s，原评审作废"
                % (old_level or "未评估", level)
            )

    if not patch:
        raise ValidationError(
            "revise requires at least one of description, affected_unit_ids, risk_level"
        )

    target_status = entity["status"]
    if entity["status"] in POST_APPROVAL_STATUSES and trigger_messages:
        target_status = "assessed"
        patch["void_reason"] = {
            "code": "scope_or_risk_changed",
            "message": "；".join(trigger_messages),
            "at": _now_iso(),
            "by": actor.user_id,
        }
    return target_status, patch


CUSTOM_CREATE = {'change': _validate_change}
CUSTOM_TRANSITIONS = {
    ('change', 'assess'): _validate_assess,
    ('change', 'approve'): _validate_approve,
    ('change', 'implement'): _validate_implement,
    ('change', 'commission'): _validate_commission,
    ('change', 'revise'): _validate_revise,
}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'revise': (('draft', 'assessed', 'approved', 'implemented'), None), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('description',), 'action_item': ('change_id', 'description', 'owner')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'commission': ('admin', 'engineer'), 'revise': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier')}

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
        result = custom(actor, entity, data, lookup) if custom else {}
        if isinstance(result, tuple):
            override, extra = result
        else:
            override, extra = None, result
        patch = dict(data)
        if extra:
            patch.update(extra)
        next_status = override or next_status
        if not next_status:
            raise InvalidTransition("action %s did not resolve a target status" % action)
        return next_status, patch
