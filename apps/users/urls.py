from django.urls import path
from . import views

# Keep auth bootstrap routes here too as fallbacks, so they resolve
# even if root URL include ordering changes.
urlpatterns = [
    path("api/auth/csrf/", views.csrf_cookie),
    path("api/auth/csrf", views.csrf_cookie),
    path("api/auth/signup/", views.signup),
    path("api/auth/signup", views.signup),
    path("api/auth/login/", views.login_view),
    path("api/auth/login", views.login_view),
    path("api/auth/profile/", views.profile),
    path("api/auth/profile", views.profile),
    path("api/auth/logout/", views.logout_view),
    path("api/auth/logout", views.logout_view),

    path("api/csrf/", views.csrf_cookie),
    path("api/signup/", views.signup),
    path("api/login/", views.login_view),
    path("api/profile/", views.profile),
    path("api/profile/patch/", views.profile_patch),
    path("api/profile/password/", views.change_password),
    path("api/profile/org-members/", views.org_members),
    path("api/logout/", views.logout_view),
]
