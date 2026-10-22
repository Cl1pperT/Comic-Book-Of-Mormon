from ..models import AuditLog


def log(db, actor, action, entity="", entity_id="", detail=""):
    db.add(AuditLog(actor_id=actor.id if actor else None, action=action, entity=entity,
                    entity_id=str(entity_id), detail=detail))
