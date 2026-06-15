import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import get_object_or_404

from apps.labor.models import Labor
from apps.organizations.models import Organization
from apps.rmct.models import RMCMModel


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


# -------------------------------------------------------
# Add Labor (organization-scoped, legacy)
# POST /api/labor/add
# -------------------------------------------------------
@csrf_exempt
def add_labor(request):

    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    try:
        data = json.loads(request.body)

        organization_id = data.get("organization_id")

        if not organization_id:
            return JsonResponse({"error": "organization_id required"}, status=400)

        organization = get_object_or_404(Organization, id=organization_id)

        labor = Labor.objects.create(
            organization=organization,
            name=data.get("name"),
            count=data.get("count", 1),
            overtime_percent=data.get("overtime_percent", 0),
            unavailability_percent=data.get("unavailability_percent", 0),
            department=data.get("department"),

            setup_factor=data.get("setup_factor", 1),
            run_factor=data.get("run_factor", 1),
            variable_factor=data.get("variable_factor", 1),

            prioritize=data.get("prioritize", False),

            lab1=data.get("lab1"),
            lab2=data.get("lab2"),
            lab3=data.get("lab3"),
            lab4=data.get("lab4"),

            notes=data.get("notes")
        )

        return JsonResponse({
            "message": "Labor added successfully",
            "labor": {
                "id": str(labor.id),
                "name": labor.name,
                "count": labor.count,
                "department": labor.department,
                "overtime_percent": labor.overtime_percent,
                "unavailability_percent": labor.unavailability_percent,
            }
        }, status=201)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


# -------------------------------------------------------
# Get All Labors
# GET /api/labor
# -------------------------------------------------------
def get_labors(request):

    organization_id = request.GET.get("organization_id")

    labors = Labor.objects.filter(
        organization_id=organization_id,
        deleted_at__isnull=True
    ).order_by("-created_at")

    data = []

    for labor in labors:
        data.append({
            "id": str(labor.id),
            "name": labor.name,
            "count": labor.count,
            "department": labor.department,
            "overtime_percent": labor.overtime_percent,
            "unavailability_percent": labor.unavailability_percent,
            "setup_factor": labor.setup_factor,
            "run_factor": labor.run_factor,
            "variable_factor": labor.variable_factor,
            "prioritize": labor.prioritize,
            "lab1": labor.lab1,
            "lab2": labor.lab2,
            "lab3": labor.lab3,
            "lab4": labor.lab4,
            "notes": labor.notes,
            "created_at": labor.created_at
        })

    return JsonResponse({"labors": data})


# -------------------------------------------------------
# Get Single Labor
# GET /api/labor/{id}
# -------------------------------------------------------
def get_labor(request, labor_id):

    labor = get_object_or_404(Labor, id=labor_id)

    data = {
        "id": str(labor.id),
        "name": labor.name,
        "count": labor.count,
        "department": labor.department,
        "overtime_percent": labor.overtime_percent,
        "unavailability_percent": labor.unavailability_percent,
        "setup_factor": labor.setup_factor,
        "run_factor": labor.run_factor,
        "variable_factor": labor.variable_factor,
        "prioritize": labor.prioritize,
        "lab1": labor.lab1,
        "lab2": labor.lab2,
        "lab3": labor.lab3,
        "lab4": labor.lab4,
        "notes": labor.notes
    }

    return JsonResponse({"labor": data})


# -------------------------------------------------------
# Update Labor
# PUT /api/labor/{id}
# -------------------------------------------------------
@csrf_exempt
def update_labor(request, labor_id):

    if request.method != "PUT":
        return JsonResponse({"error": "PUT method required"}, status=405)

    try:
        labor = get_object_or_404(Labor, id=labor_id)

        data = json.loads(request.body)

        labor.name = data.get("name", labor.name)
        labor.count = data.get("count", labor.count)
        labor.department = data.get("department", labor.department)

        labor.overtime_percent = data.get("overtime_percent", labor.overtime_percent)
        labor.unavailability_percent = data.get("unavailability_percent", labor.unavailability_percent)

        labor.setup_factor = data.get("setup_factor", labor.setup_factor)
        labor.run_factor = data.get("run_factor", labor.run_factor)
        labor.variable_factor = data.get("variable_factor", labor.variable_factor)

        labor.prioritize = data.get("prioritize", labor.prioritize)

        labor.lab1 = data.get("lab1", labor.lab1)
        labor.lab2 = data.get("lab2", labor.lab2)
        labor.lab3 = data.get("lab3", labor.lab3)
        labor.lab4 = data.get("lab4", labor.lab4)

        labor.notes = data.get("notes", labor.notes)

        labor.save()

        return JsonResponse({
            "message": "Labor updated successfully",
            "labor_id": str(labor.id)
        })

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


# -------------------------------------------------------
# Delete Labor (Soft Delete)
# DELETE /api/labor/{id}
# -------------------------------------------------------
@csrf_exempt
def delete_labor(request, labor_id):

    if request.method != "DELETE":
        return JsonResponse({"error": "DELETE method required"}, status=405)

    labor = get_object_or_404(Labor, id=labor_id)

    labor.deleted_at = labor.updated_at
    labor.save()

    return JsonResponse({
        "message": "Labor deleted successfully"
    })


# -------------------------------------------------------
# Model-scoped Labor CRUD used by RMCT frontend
# /api/models/:modelId/labor/...
# -------------------------------------------------------

@csrf_exempt
def model_labor_create(request, model_id):
    """
    POST /api/models/:modelId/labor/
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    m = get_object_or_404(RMCMModel, id=model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    owner = m.owner
    if owner is None or not hasattr(owner, "profile") or not getattr(owner.profile, "organization_id", None):
        return JsonResponse({"error": "Owner organization not configured for model"}, status=400)

    org = owner.profile.organization

    labor_id = data.get("id")

    labor_kwargs = {
        "organization": org,
        "model": m,
        "name": data.get("name", "").upper(),
        "count": data.get("count", 1),
        "overtime_percent": data.get("overtime_pct", 0),
        "unavailability_percent": data.get("unavail_pct", 0),
        "department": data.get("dept_code") or None,
        "setup_factor": data.get("setup_factor", 1),
        "run_factor": data.get("run_factor", 1),
        "variable_factor": data.get("var_factor", 1),
        "prioritize": data.get("prioritize_use", False),
        "lab1": data.get("lab1", 0),
        "lab2": data.get("lab2", 0),
        "lab3": data.get("lab3", 0),
        "lab4": data.get("lab4", 0),
        "notes": data.get("comments", ""),
    }

    if labor_id:
        labor_kwargs["id"] = labor_id

    from django.db import IntegrityError

    try:
        labor = Labor.objects.create(**labor_kwargs)
    except IntegrityError:
        # A row with the same (organization, model, name) already exists.
        # Look it up ignoring deleted_at so we can either return or "undelete" it.
        existing = Labor.objects.filter(
            organization=org,
            model=m,
            name=labor_kwargs["name"],
        ).first()
        if not existing:
            # If we cannot find the existing row, re-raise so it surfaces during debugging.
            raise

        # If the row was soft-deleted, revive it and update fields from the payload.
        if existing.deleted_at is not None:
            existing.deleted_at = None
            existing.count = labor_kwargs["count"]
            existing.overtime_percent = labor_kwargs["overtime_percent"]
            existing.unavailability_percent = labor_kwargs["unavailability_percent"]
            existing.department = labor_kwargs["department"]
            existing.setup_factor = labor_kwargs["setup_factor"]
            existing.run_factor = labor_kwargs["run_factor"]
            existing.variable_factor = labor_kwargs["variable_factor"]
            existing.prioritize = labor_kwargs["prioritize"]
            existing.lab1 = labor_kwargs["lab1"]
            existing.lab2 = labor_kwargs["lab2"]
            existing.lab3 = labor_kwargs["lab3"]
            existing.lab4 = labor_kwargs["lab4"]
            existing.notes = labor_kwargs["notes"]
            existing.save()

        return JsonResponse(
            {
                "id": str(existing.id),
                "detail": "Labor with this name already exists for this model and organization.",
            },
            status=200,
        )

    return JsonResponse({"id": str(labor.id)}, status=201)


@csrf_exempt
def model_labor_update(request, model_id, labor_id):
    """
    PATCH /api/models/:modelId/labor/:laborId/
    """
    if request.method != "PATCH":
        return JsonResponse({"error": "PATCH method required"}, status=405)

    m = get_object_or_404(RMCMModel, id=model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    labor = get_object_or_404(Labor, id=labor_id, model=m)

    if "name" in data:
        labor.name = data["name"]
    if "count" in data:
        labor.count = data["count"]
    if "overtime_pct" in data:
        labor.overtime_percent = data["overtime_pct"]
    if "unavail_pct" in data:
        labor.unavailability_percent = data["unavail_pct"]
    if "dept_code" in data:
        labor.department = data["dept_code"] or None
    if "setup_factor" in data:
        labor.setup_factor = data["setup_factor"]
    if "run_factor" in data:
        labor.run_factor = data["run_factor"]
    if "var_factor" in data:
        labor.variable_factor = data["var_factor"]
    if "prioritize_use" in data:
        labor.prioritize = data["prioritize_use"]
    for key, field in (("lab1", "lab1"), ("lab2", "lab2"), ("lab3", "lab3"), ("lab4", "lab4")):
        if key in data:
            setattr(labor, field, data[key])
    if "comments" in data:
        labor.notes = data["comments"]

    labor.save()

    return JsonResponse({})


@csrf_exempt
def model_labor_delete(request, model_id, labor_id):
    """
    DELETE /api/models/:modelId/labor/:laborId/delete/
    """
    if request.method != "DELETE":
        return JsonResponse({"error": "DELETE method required"}, status=405)

    m = get_object_or_404(RMCMModel, id=model_id)
    try:
        labor = Labor.objects.get(id=labor_id, model=m)
    except Labor.DoesNotExist:
        return JsonResponse({"success": True}, status=200)

    from django.utils import timezone

    labor.deleted_at = timezone.now()
    labor.save()

    return JsonResponse({"success": True}, status=200)