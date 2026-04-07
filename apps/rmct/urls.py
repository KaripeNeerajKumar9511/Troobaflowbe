from django.urls import path

from . import views

urlpatterns = [
    # Models
    path('models/', views.model_list_or_create),  # GET list, POST create (body has id)
    path('models/seed-demo/', views.seed_demo),
    path('models/<uuid:model_id>/', views.model_detail),  # GET one
    path('models/<uuid:model_id>/general/', views.model_general),  # PATCH general settings
    path('models/<uuid:model_id>/save/', views.model_save),  # PUT full model
    path('models/<uuid:model_id>/patch/', views.model_patch),
    path('models/<uuid:model_id>/delete/', views.model_delete),
    path('models/<uuid:model_id>/param-names/', views.model_param_names),
    path('models/<uuid:model_id>/param-names/upsert/', views.model_param_names_upsert),
    path('models/<uuid:model_id>/dept-codes/', views.model_dept_codes),
    path('models/<uuid:model_id>/dept-codes/save/', views.model_dept_codes_put),
    # Versions
    path('models/<uuid:model_id>/versions/', views.version_list),
    path('models/<uuid:model_id>/versions/create/', views.version_create),
    path('models/<uuid:model_id>/versions/<uuid:version_id>/restore/', views.version_restore),
    path('versions/<uuid:version_id>/', views.version_snapshot),
    path('versions/<uuid:version_id>/patch/', views.version_patch),
    path('versions/<uuid:version_id>/delete/', views.version_delete),
    # Scenarios
    path('models/<uuid:model_id>/scenarios/', views.scenario_list_or_create),  # GET list, POST create
    path('models/<uuid:model_id>/scenarios/basecase/', views.scenario_ensure_basecase),
    path('models/<uuid:model_id>/scenarios/basecase/results/', views.scenario_basecase_results),  # GET + PUT
    path('scenarios/<uuid:scenario_id>/', views.scenario_patch),
    path('scenarios/<uuid:scenario_id>/delete/', views.scenario_delete),
    path('scenarios/<uuid:scenario_id>/changes/', views.scenario_upsert_change),
    path('scenarios/<uuid:scenario_id>/changes/<uuid:change_id>/delete/', views.scenario_remove_change),
    path('scenarios/<uuid:scenario_id>/results/', views.scenario_save_results),
]
