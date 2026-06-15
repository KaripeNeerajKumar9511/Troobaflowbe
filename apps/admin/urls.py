from django.urls import path
from . import views

urlpatterns = [
    path('api/admin/login/', views.admin_login),
    path('api/admin/login', views.admin_login),
    path('api/admin/logout/', views.admin_logout),
    path('api/admin/logout', views.admin_logout),
    path('api/admin/me/', views.admin_me),
    path('api/admin/me', views.admin_me),
    path('api/admin/stats/', views.admin_stats),
    path('api/admin/stats', views.admin_stats),
    path('api/admin/users/', views.admin_users),
    path('api/admin/users', views.admin_users),
    path('api/admin/users/<int:user_id>/credential/', views.admin_user_credential),
    path('api/admin/users/<int:user_id>/credential', views.admin_user_credential),
    path('api/admin/users/<int:user_id>/password/', views.admin_change_user_password),
    path('api/admin/users/<int:user_id>/password', views.admin_change_user_password),
    path('api/admin/users/<int:user_id>/delete/', views.admin_delete_user),
    path('api/admin/users/<int:user_id>/delete', views.admin_delete_user),
    path('api/admin/users/<int:user_id>/', views.admin_user_detail),
    path('api/admin/users/<int:user_id>', views.admin_user_detail),
    path(
        'api/admin/users/<int:user_id>/models/<uuid:model_id>/',
        views.admin_model_detail,
    ),
    path(
        'api/admin/users/<int:user_id>/models/<uuid:model_id>',
        views.admin_model_detail,
    ),
    path('api/admin/organizations/', views.admin_organizations),
    path('api/admin/organizations', views.admin_organizations),
    path('api/admin/organizations/create/', views.admin_create_organization),
    path('api/admin/organizations/create', views.admin_create_organization),
    path('api/admin/organizations/<uuid:org_id>/', views.admin_organization_detail),
    path('api/admin/organizations/<uuid:org_id>', views.admin_organization_detail),
    path('api/admin/organizations/<uuid:org_id>/members/', views.admin_organization_members),
    path('api/admin/organizations/<uuid:org_id>/members', views.admin_organization_members),
    path(
        'api/admin/organizations/<uuid:org_id>/members/create/',
        views.admin_create_organization_member,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/members/create',
        views.admin_create_organization_member,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/delete/',
        views.admin_delete_organization,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/delete',
        views.admin_delete_organization,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/deactivate/',
        views.admin_deactivate_organization,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/deactivate',
        views.admin_deactivate_organization,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/activate/',
        views.admin_activate_organization,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/activate',
        views.admin_activate_organization,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/members/<int:user_id>/delete/',
        views.admin_delete_organization_member,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/members/<int:user_id>/delete',
        views.admin_delete_organization_member,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/members/<int:user_id>/deactivate/',
        views.admin_deactivate_organization_member,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/members/<int:user_id>/deactivate',
        views.admin_deactivate_organization_member,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/members/<int:user_id>/activate/',
        views.admin_activate_organization_member,
    ),
    path(
        'api/admin/organizations/<uuid:org_id>/members/<int:user_id>/activate',
        views.admin_activate_organization_member,
    ),
    path('api/admin/passwords/organizations/', views.admin_passwords_organizations),
    path('api/admin/passwords/organizations', views.admin_passwords_organizations),
    path(
        'api/admin/passwords/organizations/<uuid:org_id>/',
        views.admin_organization_passwords,
    ),
    path(
        'api/admin/passwords/organizations/<uuid:org_id>',
        views.admin_organization_passwords,
    ),
]
