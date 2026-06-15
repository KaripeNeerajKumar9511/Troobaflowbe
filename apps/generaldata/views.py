import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.rmct.models import RMCMModel

from .models import GeneralData


def _parse_json(request):
    try:
        return json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return None


def _update_general(model: RMCMModel, data: dict) -> None:
    """
    Apply a partial update to GeneralData for the given model.

    Creates a GeneralData row if needed and updates only provided fields.
    """
    gd, _ = GeneralData.objects.get_or_create(model=model)
    field_map = {
        'model_title': 'model_title',
        'author': 'author',
        'comments': 'comments',
        'ops_time_unit': 'ops_time_unit',
        'mct_time_unit': 'mct_time_unit',
        'prod_period_unit': 'prod_period_unit',
        'conv1': 'conv1',
        'conv2': 'conv2',
        'util_limit': 'util_limit',
        'var_equip': 'var_equip',
        'var_labor': 'var_labor',
        'var_prod': 'var_prod',
        'gen1': 'gen1',
        'gen2': 'gen2',
        'gen3': 'gen3',
        'gen4': 'gen4',
        'output_view_mode': 'output_view_mode',
    }
    for payload_key, model_field in field_map.items():
        if payload_key in data:
            value = data[payload_key]
            if payload_key == 'output_view_mode' and value not in ('normal', 'premium'):
                continue
            setattr(gd, model_field, value)
    gd.save()


@csrf_exempt
@require_http_methods(['PATCH'])
def model_general(request, model_id):
    """
    PATCH /api/models/:id/general — update general settings for a model.
    """
    m = get_object_or_404(RMCMModel, id=model_id)
    data = _parse_json(request)
    if data is None:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    _update_general(m, data)
    return JsonResponse({})
