from django.http import JsonResponse


class ApiAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        if request.path.startswith("/api/"):
            # Browser session bootstrap (same-origin /api proxy); not for JWT.
            if request.path.startswith("/api/auth/"):
                return self.get_response(request)

            # TF Admin portal uses its own session flag (tf_admin), not Django user auth.
            if request.path.startswith("/api/admin/"):
                from apps.admin.auth import is_admin_session
                admin_public = (
                    "/api/admin/login/",
                    "/api/admin/login",
                )
                if request.path in admin_public:
                    return self.get_response(request)
                if not is_admin_session(request):
                    return JsonResponse({"error": "Unauthorized"}, status=401)
                return self.get_response(request)

            public_paths = [
                "/api/login/",
                "/api/signup/",
                "/api/csrf/",
            ]

            if request.path not in public_paths:
                if not request.user.is_authenticated:
                    return JsonResponse(
                        {"error": "Unauthorized"},
                        status=401
                    )

        return self.get_response(request)
