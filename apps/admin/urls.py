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
]
