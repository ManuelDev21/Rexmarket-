from django.contrib.auth import logout
from django.shortcuts import redirect
from django.contrib import messages
from django.http import JsonResponse

class BannedUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not request.user.is_active:
            logout(request)

            # Si la petición es API/AJAX, responder con JSON
            is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.path.startswith('/api/')
            if is_ajax or request.content_type == 'application/json':
                return JsonResponse({
                    'banned': True,
                    'error': 'Tu cuenta se encuentra suspendida por infringir las normas de RexMarket.'
                }, status=403)

            messages.error(
                request, 
                "Tu cuenta ha sido suspendida por infringir las normas de la comunidad. Para apelar, escribe a oficialrexmarket@gmail.com"
            )
            return redirect('login')

        return self.get_response(request)
