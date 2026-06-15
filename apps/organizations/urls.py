from django.urls import path
from . import views


urlpatterns = [

    path("", views.list_organizations, name="list_organizations"),

    path("create/", views.create_organization, name="create_organization"),

    path("<uuid:org_id>/", views.get_organization, name="get_organization"),

    path("<uuid:org_id>/update/", views.update_organization, name="update_organization"),

    path("<uuid:org_id>/delete/", views.delete_organization, name="delete_organization"),

    # Organization workspace (org users)
    path("members/", views.org_members, name="org_members"),
    path("members/remove/", views.org_remove_member, name="org_remove_member"),

    # Invitation flow
    path("invites/", views.create_invite, name="create_invite"),
    path("invites/preview/", views.invite_preview, name="invite_preview"),
    path("invites/preview", views.invite_preview),
    path("invites/accept/", views.accept_invite, name="accept_invite"),

]