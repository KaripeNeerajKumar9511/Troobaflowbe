from django.http import JsonResponse


class ApiAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):

        if request.path.startswith("/api/"):
            # Browser session bootstrap (same-origin /api proxy); not for JWT.
            if request.path.startswith("/api/auth/"):
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
