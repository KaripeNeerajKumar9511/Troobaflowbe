"""Helpers for nested model rows (labor, equipment, products, etc.) with legacy org ids."""


def sync_row_organization(row, org) -> None:
    if row is None or org is None:
        return
    org_id = org.id if hasattr(org, "id") else org
    if row.organization_id != org_id:
        row.organization_id = org_id
        row.save(update_fields=["organization_id", "updated_at"])


def revive_soft_deleted(row) -> None:
    if row is None:
        return
    if getattr(row, "deleted_at", None) is not None:
        row.deleted_at = None
        row.save(update_fields=["deleted_at", "updated_at"])
