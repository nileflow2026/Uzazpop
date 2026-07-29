"""
routers/medicines.py
─────────────────────
Medicine catalogue endpoints + WHO/OpenFDA sync trigger.

ACCESS:
  • Search/read  → any authenticated user
  • Create/update/delete → Admin or Pharmacist
  • Sync trigger → Admin only
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from sqlalchemy import literal

from database import get_db
from models.orm import AuditLog, Medicine, User
from schemas.schemas import (
    MedicineCreateIn, MedicineOut, MedicineSearchOut,
    MedicineUpdateIn, MedicineSyncResultOut,
)
from services.medicine_sync import sync_ppb_bulk
from utils.security import (
    get_current_user, require_admin,
    require_admin_or_pharmacist,
)

router = APIRouter(prefix="/medicines", tags=["Medicines"])


@router.get("", response_model=list[MedicineSearchOut], summary="Search medicines")
def search_medicines(
    q: str = Query(None, description="Search by name, generic name, or ATC code"),
    requires_prescription: bool = Query(None),
    is_controlled: bool = Query(None),
    is_active: bool = Query(True),
    item_type: str = Query(None, description="'medicine' or 'non_medical'"),
    category: str = Query(None, description="Exact category match"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=3000),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Full-text search on medicine name and generic name.
    Returns lightweight search results (use GET /{id} for full detail).
    """
    query = db.query(Medicine)

    if is_active is not None:
        query = query.filter(Medicine.is_active == literal(is_active))
    if requires_prescription is not None:
        query = query.filter(Medicine.requires_prescription == literal(requires_prescription))
    if is_controlled is not None:
        query = query.filter(Medicine.is_controlled == literal(is_controlled))
    if item_type:
        query = query.filter(Medicine.item_type == item_type)
    if category:
        query = query.filter(Medicine.category == category)
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(
                Medicine.name.ilike(pattern),
                Medicine.generic_name.ilike(pattern),
                Medicine.brand_name.ilike(pattern),
                Medicine.atc_code.ilike(pattern),
                Medicine.barcode.ilike(pattern),
            )
        )

    return query.order_by(Medicine.name).offset(skip).limit(limit).all()


@router.get("/meta/categories", response_model=list[str], summary="List distinct categories in use")
def list_categories(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns distinct, non-empty categories currently used in the catalogue —
    used to populate the category suggestion list in Add/Edit Medicine forms."""
    rows = (
        db.query(Medicine.category)
        .filter(Medicine.category.isnot(None), Medicine.category != "")
        .distinct()
        .order_by(Medicine.category)
        .all()
    )
    return [r[0] for r in rows]


@router.get("/barcode/{barcode}", response_model=MedicineOut, summary="Look up by barcode")
def get_by_barcode(
    barcode: str,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Used by barcode scanner at POS terminal."""
    med = db.query(Medicine).filter(Medicine.barcode == barcode).first()
    if not med:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No medicine found with barcode '{barcode}'")
    return med


@router.get("/{medicine_id}", response_model=MedicineOut, summary="Get medicine detail")
def get_medicine(
    medicine_id: int,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    med = db.get(Medicine, medicine_id)
    if not med:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
    return med


@router.post("", response_model=MedicineOut, status_code=status.HTTP_201_CREATED,
             summary="Add medicine manually (admin/pharmacist)")
def create_medicine(
    payload: MedicineCreateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    """Add a medicine that isn't in the WHO catalogue."""
    # Barcode uniqueness check
    if payload.barcode:
        existing = db.query(Medicine).filter(Medicine.barcode == payload.barcode).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Medicine with barcode '{payload.barcode}' already exists",
            )

    med = Medicine(**payload.model_dump(), source="manual")
    db.add(med)
    db.flush()

    db.add(AuditLog(
        user_id=current_user.id,
        action="MEDICINE_CREATED",
        entity="Medicine",
        entity_id=med.id,
        detail=f"Created medicine: {med.name}",
    ))
    db.commit()
    db.refresh(med)
    return med


@router.patch("/{medicine_id}", response_model=MedicineOut,
              summary="Update medicine (admin/pharmacist)")
def update_medicine(
    medicine_id: int,
    payload: MedicineUpdateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    med = db.get(Medicine, medicine_id)
    if not med:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

    changes = payload.model_dump(exclude_none=True)

    # Barcode uniqueness check if changing
    if "barcode" in changes and changes["barcode"]:
        dup = db.query(Medicine).filter(
            Medicine.barcode == changes["barcode"],
            Medicine.id != medicine_id,
        ).first()
        if dup:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Barcode '{changes['barcode']}' belongs to another medicine",
            )

    for field, value in changes.items():
        setattr(med, field, value)

    db.add(AuditLog(
        user_id=current_user.id,
        action="MEDICINE_UPDATED",
        entity="Medicine",
        entity_id=med.id,
        detail=f"Updated fields: {list(changes.keys())}",
    ))
    db.commit()
    db.refresh(med)
    return med


@router.delete("/{medicine_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Deactivate medicine (admin)")
def deactivate_medicine(
    medicine_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Soft-delete: marks medicine as inactive (preserves sales history)."""
    med = db.get(Medicine, medicine_id)
    if not med:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

    med.is_active = False
    db.add(AuditLog(
        user_id=current_user.id,
        action="MEDICINE_DEACTIVATED",
        entity="Medicine",
        entity_id=med.id,
        detail=f"Deactivated medicine: {med.name}",
    ))
    db.commit()


@router.post("/sync/ppb-bulk", response_model=MedicineSyncResultOut,
             summary="Bulk import medicines from Kenya PPB public register (admin)")
def trigger_ppb_bulk_sync(
    target_count: int = Query(500, ge=1, le=5000,
        description="How many new medicines to import this call"),
    start_skip: int = Query(0, ge=0,
        description="Resume point from a previous call's next_skip (0 = start from the beginning)"),
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Bulk-import medicines from the Kenya Pharmacy and Poisons Board (PPB)
    public register — medicines actually registered for use in Kenya,
    with real trade names, generic (INN) names, strengths, manufacturers,
    and PPB registration details. No manual typing required.

    Safe to re-run — already-imported medicines (by name or PPB
    registration number) are skipped. Once your catalogue is populated
    there's no need to sync again: everything is saved permanently to
    the database, so it's still there the next time you log in. Only
    run this again if you specifically want to pull in anything newly
    registered with PPB since your last sync.

    This is not an official published API — it's the internal endpoint
    the PPB's own public register page uses. If PPB changes their site,
    this may need updating; failures are reported clearly rather than
    silently reported as success.

    Because a full crawl can take a while, this endpoint processes one
    bounded batch per call and returns `next_skip` + `exhausted`. Call
    it again with `start_skip=<next_skip>` to continue exactly where it
    left off — the frontend's "Import All" button does this
    automatically in a loop until `exhausted=true`.
    """
    db.add(AuditLog(
        user_id=current_user.id,
        action="MEDICINE_SYNC_TRIGGERED",
        detail=f"PPB bulk sync started (target_count={target_count}, start_skip={start_skip})",
    ))
    db.commit()

    result = asyncio.run(sync_ppb_bulk(db, target_count=target_count, start_skip=start_skip))

    if result.get("fetch_error"):
        # A real connection/format failure happened — don't report this as
        # success or "reached the end", or the person will think it worked.
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not sync from PPB. {result['fetch_error']} "
                f"({result['imported']} medicine(s) were imported before the failure, "
                f"if any — you can resume from start_skip={result['next_skip']}.)"
            ),
        )

    total_note = f" (PPB register reports {result['records_total']} total entries.)" if result.get("records_total") else ""
    return MedicineSyncResultOut(
        imported=result["imported"],
        skipped=result["skipped"],
        errors=result["errors"],
        next_skip=result["next_skip"],
        exhausted=result["exhausted"],
        records_total=result.get("records_total"),
        message=(
            f"{result['imported']} imported, "
            f"{result['skipped']} already existed/unusable, "
            f"{result['errors']} error(s), "
            f"across {result['pages_fetched']} page(s)."
            + (" Reached the end of the register." if result["exhausted"] else "")
            + total_note
        ),
    )