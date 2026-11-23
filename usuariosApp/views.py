import hmac
import json
import logging
import math
import secrets
import uuid
from collections import defaultdict
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import (
    Avg, BigIntegerField, Count, DurationField, ExpressionWrapper, F, Max, Q, Sum, Value
)
from django.db.models.functions import Cast, NullIf, Replace, Trim
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timezone import now
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView
from webpush import send_user_notification

from usuariosApp.forms import ComentarioForm, CustomUserCreationForm, ProductoForm
from usuariosApp.models import (
    ApelacionSuspension, AuditoriaAdmin, Comentario, Conversation, Favorito,
    Message, MessageReport, PagoComision, PagoDestacado, Pedido, Perfil,
    Producto, Reporte, ReporteComentario, Sede, VerificationCode
)
from usuariosApp.moderation import analyze_message
from usuariosApp.transbank_service import (
    confirmar_transaccion_webpay, es_modo_simulacion, iniciar_transaccion_webpay
)

logger = logging.getLogger(__name__)

TIEMPO_EXPIRACION_SOLICITUD_SEGUNDOS = 3600  # 1 hora
TIEMPO_ESPERA_INASISTENCIA_SEGUNDOS = 600   # 10 minutos de espera mínima antes de declarar inasistencia
MAX_LONGITUD_MENSAJE_CHAT = 2000

ESTADOS_PEDIDO_EN_CURSO = ['solicitado', 'acordado', 'en_camino', 'en_sitio', 'enviado']
ESTADOS_PEDIDO_COMPROMETIDO = ['acordado', 'en_camino', 'en_sitio', 'enviado']
ESTADOS_PEDIDO_CERRADOS = ['completado', 'cancelado_inasistencia', 'rechazado', 'expirado', 'cancelado_admin']


# ==============================================================================
# HELPERS DE EXPIRACIÓN, RESERVA Y CASCADA DE CONCURRENCIA
# ==============================================================================

def _verificar_expiracion_pedidos(producto=None, conversacion=None):
    """
    Caduca automáticamente solicitudes en estado 'solicitado' que superen 1 hora
    sin respuesta y notifica a los chats afectados de forma atómica.
    """
    limite = timezone.now() - timedelta(seconds=TIEMPO_EXPIRACION_SOLICITUD_SEGUNDOS)
    notificaciones_pendientes = []

    with transaction.atomic():
        qs = Pedido.objects.select_for_update().filter(
            estado='solicitado',
            fecha_creacion__lte=limite
        ).select_related('conversacion__producto', 'vendedor', 'comprador')

        if conversacion is not None:
            qs = qs.filter(conversacion=conversacion)
        elif producto is not None:
            qs = qs.filter(conversacion__producto=producto)

        expirados = list(qs)
        if not expirados:
            return 0

        for ped in expirados:
            ped.estado = 'expirado'
            ped.save(update_fields=['estado', 'fecha_actualizacion'])

            conv = ped.conversacion
            prod = getattr(conv, 'producto', None)
            if prod and not prod.vendido:
                prod_lock = Producto.objects.select_for_update().get(id=prod.id)
                tiene_acuerdo_activo = Pedido.objects.filter(
                    conversacion__producto=prod_lock,
                    estado__in=ESTADOS_PEDIDO_COMPROMETIDO
                ).exists()
                if not tiene_acuerdo_activo and prod_lock.reservado:
                    prod_lock.liberar_reserva()

            if conv:
                msg_texto = "⌛ La propuesta de entrega expiró automáticamente tras 1 hora sin respuesta. El artículo vuelve a estar libre para coordinar."
                Message.objects.create(
                    conversation=conv,
                    sender=ped.vendedor,
                    content=msg_texto
                )
                notificaciones_pendientes.append(conv.id)

    channel_layer = get_channel_layer()
    for conv_id in notificaciones_pendientes:
        try:
            async_to_sync(channel_layer.group_send)(
                f'chat_{conv_id}',
                {
                    'type': 'chat_update',
                    'sender_id': 0
                }
            )
        except Exception as e:
            logger.warning(f"Error notificando expiración WS en chat {conv_id}: {e}")

    return len(notificaciones_pendientes)


def _cancelar_otras_propuestas_al_reservar(producto, conversacion_acordada, vendedor):
    """
    Al aprobar una entrega y reservar el stock, rechaza solicitudes pendientes en otros
    chats del mismo producto y avisa a esos compradores.
    """
    if not producto:
        return

    otras_convs = Conversation.objects.filter(producto=producto).exclude(id=conversacion_acordada.id)
    channel_layer = get_channel_layer()

    for otra_conv in otras_convs:
        pendientes = Pedido.objects.filter(conversacion=otra_conv, estado='solicitado')
        if pendientes.exists():
            pendientes.update(estado='rechazado', fecha_actualizacion=timezone.now())

        msg_texto = (
            "⏳ Aviso automático: El vendedor acaba de reservar este producto con otro estudiante. "
            "Si esa entrega no se concreta, volverá a quedar disponible."
        )
        Message.objects.create(
            conversation=otra_conv,
            sender=vendedor,
            content=msg_texto
        )

        receptor = otra_conv.participants.exclude(id=vendedor.id).first()
        if receptor:
            try:
                async_to_sync(channel_layer.group_send)(
                    f'notificaciones_{receptor.id}',
                    {
                        'type': 'send_notification',
                        'data': {
                            'type': 'nueva_notificacion',
                            'latest_message': {
                                'id': 0,
                                'sender': vendedor.first_name or vendedor.username,
                                'content': msg_texto,
                                'conversation_id': otra_conv.id
                            }
                        }
                    }
                )
            except Exception as e:
                logger.warning(f"Error enviando notificación de reserva a {receptor.id}: {e}")

        try:
            async_to_sync(channel_layer.group_send)(
                f'chat_{otra_conv.id}',
                {
                    'type': 'chat_update',
                    'sender_id': vendedor.id
                }
            )
        except Exception as e:
            logger.warning(f"Error emitiendo chat_update de reserva en {otra_conv.id}: {e}")


def _notificar_liberacion_reserva_otros_chats(producto, conversacion_origen, vendedor):
    """
    Si una reserva se cae (por inasistencia o anulación administrativa), avisa a los
    demás chats abiertos por ese artículo que volvió a estar disponible.
    """
    if not producto or producto.vendido or producto.reservado:
        return

    otras_convs = Conversation.objects.filter(producto=producto)
    if conversacion_origen:
        otras_convs = otras_convs.exclude(id=conversacion_origen.id)

    channel_layer = get_channel_layer()
    for otra_conv in otras_convs:
        msg_texto = "🔔 Aviso automático: Este producto volvió a estar disponible para coordinar compra."
        Message.objects.create(
            conversation=otra_conv,
            sender=vendedor,
            content=msg_texto
        )
        try:
            async_to_sync(channel_layer.group_send)(
                f'chat_{otra_conv.id}',
                {
                    'type': 'chat_update',
                    'sender_id': vendedor.id
                }
            )
        except Exception as e:
            logger.warning(f"Error emitiendo chat_update de liberación en {otra_conv.id}: {e}")


def _notificar_cierre_cascada_otros_chats(producto, conversacion_ganadora, vendedor):
    """
    Cuando un producto se marca como vendido, cierra propuestas pendientes y notifica
    en cascada a todos los demás chats abiertos por ese producto.
    """
    if not producto:
        return

    otras_convs = Conversation.objects.filter(producto=producto)
    if conversacion_ganadora:
        otras_convs = otras_convs.exclude(id=conversacion_ganadora.id)

    channel_layer = get_channel_layer()
    for otra_conv in otras_convs:
        Pedido.objects.filter(
            conversacion=otra_conv,
            estado='solicitado'
        ).update(estado='rechazado', fecha_actualizacion=timezone.now())

        msg_texto = "🔒 Aviso automático: Este artículo ya fue vendido y dejó de estar disponible."
        Message.objects.create(
            conversation=otra_conv,
            sender=vendedor,
            content=msg_texto
        )

        receptor = otra_conv.participants.exclude(id=vendedor.id).first()
        if receptor:
            try:
                async_to_sync(channel_layer.group_send)(
                    f'notificaciones_{receptor.id}',
                    {
                        'type': 'send_notification',
                        'data': {
                            'type': 'nueva_notificacion',
                            'latest_message': {
                                'id': 0,
                                'sender': vendedor.first_name or vendedor.username,
                                'content': msg_texto,
                                'conversation_id': otra_conv.id
                            }
                        }
                    }
                )
            except Exception as e:
                logger.warning(f"Error enviando notificación de venta en cascada a {receptor.id}: {e}")

        try:
            async_to_sync(channel_layer.group_send)(
                f'chat_{otra_conv.id}',
                {
                    'type': 'chat_update',
                    'sender_id': vendedor.id
                }
            )
        except Exception as e:
            logger.warning(f"Error emitiendo chat_update de venta en cascada en {otra_conv.id}: {e}")


# ==============================================================================
# AUTENTICACIÓN Y REGISTRO SEGURO
# ==============================================================================

VERIFICATION_CODE_TTL_MINUTES = 15
VERIFICATION_RESEND_COOLDOWN_SECONDS = 30
VERIFICATION_MAX_RESENDS = 3
VERIFICATION_MAX_ATTEMPTS = 5


def _generar_codigo_verificacion():
    """Genera un código alfanumérico de 6 caracteres criptográficamente seguro (sin caracteres confusos)."""
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(6))


def _enviar_correo_verificacion(user, codigo):
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'oficialrexmarket@gmail.com')
    try:
        send_mail(
            'Verifica tu correo institucional - RexMarket',
            (
                f'Hola {user.first_name},\n\n'
                f'Tu código de verificación es: {codigo}\n\n'
                f'Este código es válido por {VERIFICATION_CODE_TTL_MINUTES} minutos. '
                f'Ingrésalo en la pantalla de verificación para activar tu cuenta en RexMarket.'
            ),
            from_email,
            [user.email],
            fail_silently=False,
        )
        return True
    except Exception as e:
        logger.error(f"Error enviando correo de verificación a {user.email}: {e}")
        return False


class SignUpView(CreateView):
    form_class = CustomUserCreationForm
    template_name = "registration/signup.html"
    success_url = reverse_lazy("verify_email")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sedes = list(Sede.objects.filter(activa=True).values('id', 'nombre', 'region', 'comuna'))
        context['sedes_json'] = json.dumps(sedes)
        return context

    def form_valid(self, form):
        user = form.save(commit=False)
        user.is_active = False
        user.save()

        sede_seleccionada = form.cleaned_data.get('sede')
        perfil, _ = Perfil.objects.get_or_create(usuario=user)
        perfil.sede = sede_seleccionada
        if sede_seleccionada:
            perfil.comuna = sede_seleccionada.comuna
        perfil.save()

        ahora = timezone.now()
        verification_code = _generar_codigo_verificacion()
        VerificationCode.objects.update_or_create(
            user=user,
            defaults={
                'code': verification_code,
                'is_verified': False,
                'created_at': ahora,
                'expires_at': ahora + timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES),
                'failed_attempts': 0,
                'resend_count': 0,
                'last_sent_at': ahora,
            }
        )

        self.request.session['pending_user_id'] = user.id
        _enviar_correo_verificacion(user, verification_code)

        messages.success(
            self.request,
            f'Te enviamos un código de verificación a {user.email}. Tienes {VERIFICATION_CODE_TTL_MINUTES} minutos para ingresarlo.'
        )
        return redirect(self.success_url)


class VerifyEmailView(View):
    def _get_context(self, user, record):
        cooldown = record.seconds_until_resend(VERIFICATION_RESEND_COOLDOWN_SECONDS) if record else 0
        segundos_expiracion = 0
        if record and record.expires_at:
            segundos_expiracion = max(0, int((record.expires_at - timezone.now()).total_seconds()))

        return {
            'user_email': user.email,
            'cooldown_seconds': cooldown,
            'segundos_expiracion': segundos_expiracion,
            'resends_remaining': max(0, VERIFICATION_MAX_RESENDS - (record.resend_count if record else 0)),
        }

    def get(self, request):
        user_id = request.session.get('pending_user_id')
        if not user_id:
            messages.info(request, 'Ingresa con tu cuenta o regístrate para continuar.')
            return redirect('signup')

        user = User.objects.filter(id=user_id, is_active=False).first()
        if not user:
            request.session.pop('pending_user_id', None)
            return redirect('login')

        record = VerificationCode.objects.filter(user=user).first()
        return render(request, 'registration/verify_email.html', self._get_context(user, record))

    def post(self, request):
        user_id = request.session.get('pending_user_id')
        if not user_id:
            messages.error(request, 'La sesión de registro ha expirado. Por favor, regístrate nuevamente.')
            return redirect('signup')

        user = get_object_or_404(User, id=user_id)
        if user.is_active:
            request.session.pop('pending_user_id', None)
            return redirect('login')

        entered_code = request.POST.get('verification_code', '').strip().upper()
        record = VerificationCode.objects.filter(user=user).first()

        if not record:
            messages.error(request, 'No existe un código activo. Solicita un reenvío.')
            return render(request, 'registration/verify_email.html', self._get_context(user, record))

        if record.failed_attempts >= VERIFICATION_MAX_ATTEMPTS:
            messages.error(
                request,
                'Superaste el máximo de intentos fallidos. Presiona "Reenviar" para generar un código nuevo.'
            )
            return render(request, 'registration/verify_email.html', self._get_context(user, record))

        if record.is_expired:
            messages.error(
                request,
                'El código ha expirado (superó los 15 minutos). Solicita uno nuevo con el botón "Reenviar".'
            )
            return render(request, 'registration/verify_email.html', self._get_context(user, record))

        if entered_code and hmac.compare_digest(record.code.upper(), entered_code):
            record.is_verified = True
            record.save(update_fields=['is_verified'])

            user.is_active = True
            user.save(update_fields=['is_active'])

            request.session.pop('pending_user_id', None)
            messages.success(request, '¡Tu cuenta ha sido activada con éxito! Ya puedes iniciar sesión.')
            return redirect('login')

        record.failed_attempts += 1
        record.save(update_fields=['failed_attempts'])
        restantes = VERIFICATION_MAX_ATTEMPTS - record.failed_attempts

        if restantes > 0:
            messages.error(request, f'Código incorrecto. Te quedan {restantes} intento(s) antes de que se bloquee.')
        else:
            messages.error(request, 'Código bloqueado por demasiados intentos fallidos. Solicita un nuevo código.')

        return render(request, 'registration/verify_email.html', self._get_context(user, record))


@require_POST
def resend_verification_code(request):
    """Endpoint AJAX para invalidar el código previo y enviar uno nuevo con cooldown de 30s."""
    user_id = request.session.get('pending_user_id')
    if not user_id:
        return JsonResponse({
            'success': False,
            'message': 'Tu sesión de registro expiró. Vuelve a registrarte.'
        }, status=401)

    user = User.objects.filter(id=user_id, is_active=False).first()
    if not user:
        return JsonResponse({
            'success': False,
            'message': 'Esta cuenta ya fue verificada o no existe.'
        }, status=400)

    record, _ = VerificationCode.objects.get_or_create(
        user=user,
        defaults={'code': _generar_codigo_verificacion()}
    )

    wait_seconds = record.seconds_until_resend(VERIFICATION_RESEND_COOLDOWN_SECONDS)
    if wait_seconds > 0:
        return JsonResponse({
            'success': False,
            'cooldown': wait_seconds,
            'message': f'Debes esperar {wait_seconds} segundos antes de solicitar otro código.'
        }, status=429)

    if record.resend_count >= VERIFICATION_MAX_RESENDS:
        return JsonResponse({
            'success': False,
            'resends_remaining': 0,
            'message': 'Alcanzaste el límite máximo de 3 reenvíos. Si tienes problemas con tu correo institucional, contacta a soporte.'
        }, status=429)

    ahora = timezone.now()
    nuevo_codigo = _generar_codigo_verificacion()
    record.code = nuevo_codigo
    record.created_at = ahora
    record.expires_at = ahora + timedelta(minutes=VERIFICATION_CODE_TTL_MINUTES)
    record.failed_attempts = 0
    record.resend_count += 1
    record.last_sent_at = ahora
    record.save()

    enviado = _enviar_correo_verificacion(user, nuevo_codigo)
    if not enviado:
        return JsonResponse({
            'success': False,
            'message': 'Hubo un problema al enviar el correo. Intenta nuevamente en unos segundos.'
        }, status=500)

    restantes = max(0, VERIFICATION_MAX_RESENDS - record.resend_count)
    return JsonResponse({
        'success': True,
        'cooldown': VERIFICATION_RESEND_COOLDOWN_SECONDS,
        'segundos_expiracion': VERIFICATION_CODE_TTL_MINUTES * 60,
        'resends_remaining': restantes,
        'message': f'¡Nuevo código enviado a {user.email}! Válido por 15 minutos.'
    })


def _formatear_rut_login(rut_raw: str) -> str:
    limpio = rut_raw.upper().replace(".", "").replace("-", "").strip()
    if len(limpio) >= 2:
        return f"{limpio[:-1]}-{limpio[-1]}"
    return rut_raw.strip()


def login_register_view(request):
    if request.user.is_authenticated:
        return redirect('home')

    login_form = AuthenticationForm()
    usuario_suspendido = None
    apelacion_pendiente = None

    if request.method == "POST":
        rut_ingresado = request.POST.get('username', '').strip()
        rut_normalizado = _formatear_rut_login(rut_ingresado)

        if rut_ingresado:
            usuario = (
                User.objects.select_related('perfil').filter(username__iexact=rut_normalizado).first()
                or User.objects.select_related('perfil').filter(username__iexact=rut_ingresado).first()
            )
            if usuario and not usuario.is_active:
                verificacion = VerificationCode.objects.filter(user=usuario, is_verified=False).first()
                perfil_usuario = getattr(usuario, 'perfil', None)
                if verificacion and not (perfil_usuario and perfil_usuario.esta_baneado):
                    request.session['pending_user_id'] = usuario.id
                    messages.warning(
                        request,
                        "Tu cuenta aún no ha sido activada. Ingresa el código enviado a tu correo institucional."
                    )
                    return redirect('verify_email')

                usuario_suspendido = usuario
                apelacion_pendiente = ApelacionSuspension.objects.filter(
                    usuario=usuario,
                    estado='pendiente'
                ).first()

                form_limpio = AuthenticationForm(initial={'username': usuario.username})
                return render(request, 'registration/login.html', {
                    'form': form_limpio,
                    'login_form': form_limpio,
                    'usuario_suspendido': usuario_suspendido,
                    'apelacion_pendiente': apelacion_pendiente,
                })

        post_data = request.POST.copy()
        post_data['username'] = rut_normalizado
        login_form = AuthenticationForm(request, data=post_data)
        if login_form.is_valid():
            user = login_form.get_user()
            login(request, user)
            messages.success(request, f'¡Bienvenido de nuevo, {user.first_name or user.username}!')
            next_url = request.GET.get('next', '')
            if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                next_url = 'home'
            return redirect(next_url)

    return render(request, 'registration/login.html', {
        'form': login_form,
        'login_form': login_form,
        'usuario_suspendido': usuario_suspendido,
        'apelacion_pendiente': apelacion_pendiente,
    })


def solicitar_apelacion_view(request):
    """Permite a un estudiante suspendido enviar una solicitud formal de revisión/desbaneo."""
    if request.user.is_authenticated:
        return redirect('home')

    rut_inicial = request.GET.get('rut', '').strip()

    if request.method == 'POST':
        rut_ingresado = _formatear_rut_login(request.POST.get('rut', ''))
        correo_ingresado = request.POST.get('email', '').strip().lower()
        motivo = request.POST.get('motivo', '').strip()[:1500]

        if len(motivo) < 20:
            messages.error(request, 'Por favor detalla tu descargo en al menos 20 caracteres.')
            return render(request, 'registration/solicitar_apelacion.html', {
                'rut_inicial': rut_ingresado,
                'email_inicial': correo_ingresado,
                'motivo_inicial': motivo,
            })

        usuario = User.objects.select_related('perfil').filter(
            username__iexact=rut_ingresado,
            email__iexact=correo_ingresado
        ).first()

        if not usuario:
            messages.error(request, 'No encontramos una cuenta que coincida con ese RUT y correo institucional.')
            return render(request, 'registration/solicitar_apelacion.html', {
                'rut_inicial': rut_ingresado,
                'email_inicial': correo_ingresado,
                'motivo_inicial': motivo,
            })

        if usuario.is_active:
            messages.info(request, 'Tu cuenta se encuentra activa. Ya puedes iniciar sesión normalmente.')
            return redirect('login')

        perfil_usuario = getattr(usuario, 'perfil', None)
        pendiente_verificacion = VerificationCode.objects.filter(user=usuario, is_verified=False).exists()
        if pendiente_verificacion and not (perfil_usuario and perfil_usuario.esta_baneado):
            request.session['pending_user_id'] = usuario.id
            messages.warning(
                request,
                'Tu cuenta no está suspendida, aún falta verificar el código enviado a tu correo institucional.'
            )
            return redirect('verify_email')

        if ApelacionSuspension.objects.filter(usuario=usuario, estado='pendiente').exists():
            messages.warning(request, 'Ya tienes una solicitud de apelación pendiente de revisión por el administrador.')
            return redirect('login')

        apelacion = ApelacionSuspension.objects.create(
            usuario=usuario,
            motivo_estudiante=motivo,
            estado='pendiente'
        )

        fecha_local = timezone.localtime(apelacion.fecha_creacion)
        _notificar_admin_reporte('APELACION', {
            'id': apelacion.id,
            'usuario_rut': usuario.username,
            'usuario_nombre': f"{usuario.first_name} {usuario.last_name}".strip() or usuario.username,
            'motivo': apelacion.motivo_estudiante,
            'fecha_display': fecha_local.strftime('%d/%m/%Y %H:%M'),
        })

        messages.success(
            request,
            'Tu solicitud de apelación fue enviada al equipo de administración. Recibirás una respuesta en tu correo institucional.'
        )
        return redirect('login')

    return render(request, 'registration/solicitar_apelacion.html', {
        'rut_inicial': rut_inicial
    })


class ProfileView(LoginRequiredMixin, DetailView):
    model = User
    template_name = 'registration/profile.html'
    context_object_name = 'user'

    def get_object(self):
        return self.request.user

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        usuario = self.request.user
        _verificar_expiracion_pedidos()

        context['mis_productos'] = Producto.objects.filter(
            vendedor=usuario
        ).select_related('sede').order_by('-id')

        mis_compras = Pedido.objects.filter(
            comprador=usuario
        ).select_related('conversacion__producto', 'vendedor').order_by('-id')

        mis_ventas = Pedido.objects.filter(
            vendedor=usuario
        ).select_related('conversacion__producto', 'comprador').order_by('-id')

        pedidos_activos_count = (
            mis_compras.filter(estado__in=ESTADOS_PEDIDO_EN_CURSO).count()
            + mis_ventas.filter(estado__in=ESTADOS_PEDIDO_EN_CURSO).count()
        )

        context['mis_compras'] = mis_compras
        context['mis_ventas'] = mis_ventas
        context['pedidos_activos_count'] = pedidos_activos_count
        return context


class DeleteAccountView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        user = request.user

        tiene_compromisos = Pedido.objects.filter(
            Q(comprador=user) | Q(vendedor=user),
            estado__in=ESTADOS_PEDIDO_COMPROMETIDO
        ).exists()
        if tiene_compromisos:
            messages.error(
                request,
                'No puedes eliminar tu cuenta mientras tengas entregas presenciales o envíos activos en curso.'
            )
            return redirect('profile')

        if 'password' in request.POST:
            password = request.POST.get('password', '')
            if not password or not user.check_password(password):
                messages.error(request, 'La contraseña ingresada es incorrecta.')
                return redirect('profile')

        logout(request)
        user.delete()
        messages.success(request, 'Tu cuenta y datos asociados han sido eliminados correctamente.')
        return redirect('home')


# ==============================================================================
# PRODUCTOS, CATÁLOGO, PAGOS TRANSBANK (2%) Y DESTACADOS VIP
# ==============================================================================

def lista_productos(request):
    _verificar_expiracion_pedidos()

    query = request.GET.get('q', '').strip()
    categoria_seleccionada = request.GET.get('categoria', '').strip()
    ordenar_por = request.GET.get('ordenar_por', '').strip()
    sede_seleccionada = request.GET.get('sede', '').strip()

    hace_una_semana = now() - timedelta(days=7)

    productos = Producto.objects.filter(
        activo=True,
        comision_pagada=True
    ).filter(
        Q(vendido=False) | Q(vendido=True, fecha_vendido__gte=hace_una_semana)
    ).annotate(
        valoracion_promedio=Avg('comentarios__valoracion')
    ).select_related('vendedor', 'sede').prefetch_related('favoritos')

    if sede_seleccionada.isdigit():
        productos = productos.filter(sede_id=int(sede_seleccionada))

    if query:
        productos = productos.filter(
            Q(nombre__icontains=query) | Q(descripcion__icontains=query)
        )

    if categoria_seleccionada:
        if categoria_seleccionada == 'favoritos' and request.user.is_authenticated:
            productos = productos.filter(favoritos__usuario=request.user)
        elif categoria_seleccionada == 'mis_productos' and request.user.is_authenticated:
            productos = productos.filter(vendedor=request.user)
        elif categoria_seleccionada not in ('favoritos', 'mis_productos'):
            productos = productos.filter(categoria=categoria_seleccionada)

    if ordenar_por == 'valoracion':
        productos = productos.order_by('vendido', 'reservado', F('valoracion_promedio').desc(nulls_last=True))
    elif ordenar_por in ('precio_asc', 'precio_desc'):
        char_field = Producto._meta.get_field('nombre').__class__()
        precio_limpio = NullIf(
            Trim(
                Replace(
                    Replace(
                        Replace(Cast('precio', output_field=char_field), Value('.'), Value('')),
                        Value(','), Value('')
                    ),
                    Value('$'), Value('')
                )
            ),
            Value('')
        )
        productos = productos.annotate(
            precio_sql=Cast(precio_limpio, output_field=BigIntegerField())
        )
        if ordenar_por == 'precio_asc':
            productos = productos.order_by('vendido', 'reservado', F('precio_sql').asc(nulls_last=True), '-id')
        else:
            productos = productos.order_by('vendido', 'reservado', F('precio_sql').desc(nulls_last=True), '-id')
    else:
        productos = productos.order_by(
            'vendido',
            'reservado',
            F('destacado_hasta').desc(nulls_last=True),
            '-id'
        )

    paginator = Paginator(productos, 21)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    if request.user.is_authenticated:
        favoritos_ids = set(request.user.favoritos.values_list('producto_id', flat=True))
        for p in page_obj:
            p.es_favorito_usuario = p.id in favoritos_ids
    else:
        for p in page_obj:
            p.es_favorito_usuario = False

    categorias = list(Producto.CATEGORIAS) + [
        ('favoritos', 'Mis Favoritos'),
        ('mis_productos', 'Mis Productos')
    ]

    productos_vip = Producto.objects.filter(
        activo=True,
        comision_pagada=True,
        vendido=False,
        reservado=False,
        destacado_hasta__gte=now(),
        tipo_destacado=7
    ).select_related('sede').order_by('-destacado_hasta')[:6]

    sedes_activas = Sede.objects.filter(activa=True)

    return render(request, 'index.html', {
        'page_obj': page_obj,
        'query': query,
        'categorias': categorias,
        'categoria_seleccionada': categoria_seleccionada,
        'ordenar_por': ordenar_por,
        'sedes_activas': sedes_activas,
        'sede_seleccionada': sede_seleccionada,
        'user': request.user,
        'productos_vip': productos_vip
    })


@login_required
def publicar_producto(request):
    perfil_usuario = getattr(request.user, 'perfil', None)
    sede_usuario = perfil_usuario.sede if perfil_usuario else None
    initial_data = {'sede': sede_usuario} if sede_usuario else {}

    if request.method == "POST":
        form = ProductoForm(request.POST, request.FILES, initial=initial_data)
        if sede_usuario and 'sede' in form.fields:
            form.fields['sede'].disabled = True

        if form.is_valid():
            producto = form.save(commit=False)
            producto.vendedor = request.user
            if sede_usuario:
                producto.sede = sede_usuario
            producto.activo = False
            producto.comision_pagada = False
            producto.save()

            monto_producto = producto.precio_numerico
            monto_comision = max(50, round(monto_producto * 0.02))
            buy_order = f"RXM-{producto.id}-{uuid.uuid4().hex[:8].upper()}"

            pago = PagoComision.objects.create(
                producto=producto,
                vendedor=request.user,
                monto_producto=monto_producto,
                monto_comision=monto_comision,
                buy_order=buy_order,
                estado='pendiente'
            )

            url_webpay, token_ws = iniciar_transaccion_webpay(
                request, pago, return_view_name='retorno_pago_comision'
            )
            pago.token_ws = token_ws
            pago.save(update_fields=['token_ws'])

            if es_modo_simulacion():
                return redirect(url_webpay)

            return render(request, 'redirigir_webpay.html', {
                'url_webpay': url_webpay,
                'token_ws': token_ws,
                'pago': pago,
            })
    else:
        form = ProductoForm(initial=initial_data)
        if sede_usuario and 'sede' in form.fields:
            form.fields['sede'].disabled = True
            form.fields['sede'].help_text = 'Asignada automáticamente según tu sede registrada.'

    return render(request, 'publicar_producto.html', {'form': form})


@login_required
def reintentar_pago_comision(request, producto_id):
    """Permite al vendedor retomar el pago de comisión del 2% (mínimo $50) de un producto pendiente."""
    producto = get_object_or_404(Producto, id=producto_id, vendedor=request.user)

    if producto.comision_pagada or PagoComision.objects.filter(producto=producto, estado='pagado').exists():
        if not producto.comision_pagada:
            producto.comision_pagada = True
            producto.activo = True
            producto.save(update_fields=['comision_pagada', 'activo'])
        messages.info(request, 'La comisión de este producto ya fue pagada.')
        return redirect('detalle_producto', producto_id=producto.id)

    monto_producto = producto.precio_numerico
    monto_comision = max(50, round(monto_producto * 0.02))
    buy_order = f"RXM-{producto.id}-{uuid.uuid4().hex[:8].upper()}"

    pago, _ = PagoComision.objects.update_or_create(
        producto=producto,
        defaults={
            'vendedor': request.user,
            'monto_producto': monto_producto,
            'monto_comision': monto_comision,
            'buy_order': buy_order,
            'estado': 'pendiente',
        }
    )

    url_webpay, token_ws = iniciar_transaccion_webpay(
        request, pago, return_view_name='retorno_pago_comision'
    )
    pago.token_ws = token_ws
    pago.save(update_fields=['token_ws'])

    if es_modo_simulacion():
        return redirect(url_webpay)

    return render(request, 'redirigir_webpay.html', {
        'url_webpay': url_webpay,
        'token_ws': token_ws,
        'pago': pago,
    })


@login_required
def simulador_webpay(request, token_ws):
    """Pantalla que simula Webpay Plus tanto para comisiones de publicación como para planes destacados."""
    pago = PagoComision.objects.filter(token_ws=token_ws, vendedor=request.user).first()
    es_destacado = False
    if not pago:
        pago = get_object_or_404(PagoDestacado, token_ws=token_ws, vendedor=request.user)
        es_destacado = True

    return render(request, 'checkout_transbank.html', {
        'pago': pago,
        'es_destacado': es_destacado,
    })


@csrf_exempt
def retorno_pago_comision(request):
    """Callback único tanto para el simulador como para Webpay Plus real (comisión 2%)."""
    if es_modo_simulacion():
        if not request.user.is_authenticated or request.method != 'POST':
            messages.error(request, 'Solicitud inválida en modo simulación.')
            return redirect('publicar_producto')

    token_ws = request.POST.get('token_ws') or request.GET.get('token_ws')
    tbk_token = request.POST.get('TBK_TOKEN') or request.GET.get('TBK_TOKEN')
    tbk_orden_compra = request.POST.get('TBK_ORDEN_COMPRA') or request.GET.get('TBK_ORDEN_COMPRA')
    resultado_simulado = request.POST.get('resultado_simulado', 'rechazado')

    if not token_ws:
        if tbk_orden_compra:
            qs_cancel = PagoComision.objects.filter(buy_order=tbk_orden_compra, estado='pendiente')
            if es_modo_simulacion():
                qs_cancel = qs_cancel.filter(vendedor=request.user)
            qs_cancel.update(estado='rechazado')
        motivo = 'La transacción fue anulada en Webpay.' if tbk_token else 'La sesión de pago en Webpay expiró o fue cancelada.'
        messages.warning(request, f'{motivo} Puedes reintentar el pago desde tu perfil.')
        return redirect('profile')

    with transaction.atomic():
        pago = get_object_or_404(PagoComision.objects.select_for_update(), token_ws=token_ws)

        if es_modo_simulacion() and pago.vendedor != request.user:
            raise PermissionDenied

        if pago.estado == 'pagado':
            messages.info(request, 'Esta publicación ya se encuentra pagada y activa.')
            return redirect('detalle_producto', producto_id=pago.producto.id)

        if pago.estado != 'pendiente':
            messages.warning(request, 'Esta sesión de pago ya fue procesada anteriormente.')
            return redirect('profile')

        resultado = confirmar_transaccion_webpay(token_ws, resultado_simulado=resultado_simulado)

        if resultado['aprobado']:
            pago.estado = 'pagado'
            pago.fecha_pago = now()
            pago.save(update_fields=['estado', 'fecha_pago'])

            producto = Producto.objects.select_for_update().get(id=pago.producto.id)
            producto.comision_pagada = True
            producto.activo = True
            producto.save(update_fields=['comision_pagada', 'activo'])

            messages.success(
                request,
                f'¡Pago de comisión (${pago.monto_comision}) aprobado por Webpay! Tu producto "{producto.nombre}" ya está publicado.'
            )
            return redirect('detalle_producto', producto_id=producto.id)

        pago.estado = 'rechazado'
        pago.save(update_fields=['estado'])

    messages.error(request, f'El pago en Webpay fue rechazado ({resultado["detalle"]}). Puedes reintentarlo desde tu perfil.')
    return redirect('profile')


@login_required
def destacar_producto(request, producto_id):
    """Inicia el cobro por Webpay Plus para destacar un producto activo (1, 3 o 7 días)."""
    producto = get_object_or_404(Producto, id=producto_id, vendedor=request.user)

    if not producto.comision_pagada or not producto.activo or producto.cancelado:
        messages.error(request, 'Debes completar el pago de publicación antes de destacar este producto.')
        return redirect('detalle_producto', producto_id=producto.id)

    if producto.vendido:
        messages.error(request, 'No puedes destacar un producto que ya fue vendido.')
        return redirect('detalle_producto', producto_id=producto.id)

    if request.method == 'POST':
        duracion = str(request.POST.get('duracion', '')).strip()
        precios = {'1': 490, '3': 1490, '7': 2990}

        if duracion not in precios:
            messages.error(request, 'La duración seleccionada no es válida.')
            return redirect('destacar_producto', producto_id=producto.id)

        dias = int(duracion)
        monto = precios[duracion]
        buy_order = f"RXM-DST-{producto.id}-{dias}D-{uuid.uuid4().hex[:8].upper()}"

        pago_dest = PagoDestacado.objects.create(
            producto=producto,
            vendedor=request.user,
            dias=dias,
            monto=monto,
            buy_order=buy_order,
            estado='pendiente'
        )

        url_webpay, token_ws = iniciar_transaccion_webpay(
            request, pago_dest, return_view_name='retorno_pago_destacado'
        )
        pago_dest.token_ws = token_ws
        pago_dest.save(update_fields=['token_ws'])

        if es_modo_simulacion():
            return redirect(url_webpay)

        return render(request, 'redirigir_webpay.html', {
            'url_webpay': url_webpay,
            'token_ws': token_ws,
            'pago': pago_dest,
        })

    return render(request, 'destacar.html', {
        'producto': producto,
        'now': now(),
    })


@csrf_exempt
def retorno_pago_destacado(request):
    """Callback único de Webpay Plus / simulador para activar planes de destacado VIP."""
    if es_modo_simulacion():
        if not request.user.is_authenticated or request.method != 'POST':
            messages.error(request, 'Solicitud inválida en modo simulación.')
            return redirect('home')

    token_ws = request.POST.get('token_ws') or request.GET.get('token_ws')
    tbk_token = request.POST.get('TBK_TOKEN') or request.GET.get('TBK_TOKEN')
    tbk_orden_compra = request.POST.get('TBK_ORDEN_COMPRA') or request.GET.get('TBK_ORDEN_COMPRA')
    resultado_simulado = request.POST.get('resultado_simulado', 'rechazado')

    if not token_ws:
        producto_id_redir = None
        if tbk_orden_compra:
            qs_cancel = PagoDestacado.objects.filter(buy_order=tbk_orden_compra, estado='pendiente')
            if es_modo_simulacion():
                qs_cancel = qs_cancel.filter(vendedor=request.user)
            pago_cancelado = qs_cancel.first()
            if pago_cancelado:
                producto_id_redir = pago_cancelado.producto_id
                pago_cancelado.estado = 'rechazado'
                pago_cancelado.save(update_fields=['estado'])

        motivo = 'El pago del destacado fue anulado en Webpay.' if tbk_token else 'La sesión de pago en Webpay expiró o fue cancelada.'
        messages.warning(request, motivo)
        if producto_id_redir:
            return redirect('detalle_producto', producto_id=producto_id_redir)
        return redirect('home')

    with transaction.atomic():
        pago = get_object_or_404(PagoDestacado.objects.select_for_update(), token_ws=token_ws)

        if es_modo_simulacion() and pago.vendedor != request.user:
            raise PermissionDenied

        producto = Producto.objects.select_for_update().get(id=pago.producto_id)

        if pago.estado == 'pagado':
            messages.info(request, 'Este plan de destacado ya fue aplicado a tu publicación.')
            return redirect('detalle_producto', producto_id=producto.id)

        if pago.estado != 'pendiente':
            messages.warning(request, 'Esta transacción ya fue procesada anteriormente.')
            return redirect('detalle_producto', producto_id=producto.id)

        resultado = confirmar_transaccion_webpay(token_ws, resultado_simulado=resultado_simulado)

        if resultado['aprobado']:
            ahora = now()
            pago.estado = 'pagado'
            pago.fecha_pago = ahora
            pago.save(update_fields=['estado', 'fecha_pago'])

            base_tiempo = (
                producto.destacado_hasta
                if (producto.destacado_hasta and producto.destacado_hasta > ahora)
                else ahora
            )
            producto.destacado_hasta = base_tiempo + timedelta(days=pago.dias)
            producto.tipo_destacado = max(producto.tipo_destacado or 0, pago.dias)
            producto.save(update_fields=['destacado_hasta', 'tipo_destacado'])

            messages.success(
                request,
                f'¡Pago de ${pago.monto} aprobado! Tu producto "{producto.nombre}" estará destacado por {pago.dias} día(s).'
            )
            return redirect('detalle_producto', producto_id=producto.id)

        pago.estado = 'rechazado'
        pago.save(update_fields=['estado'])

    messages.error(request, f'El pago del destacado en Webpay fue rechazado ({resultado["detalle"]}).')
    return redirect('detalle_producto', producto_id=pago.producto_id)


@login_required
@require_POST
def marcar_producto_vendido(request, producto_id):
    """Permite al vendedor marcar su producto como vendido y notifica en cascada a los demás chats."""
    with transaction.atomic():
        producto = get_object_or_404(
            Producto.objects.select_for_update(),
            id=producto_id,
            vendedor=request.user
        )
        if producto.vendido:
            return redirect('detalle_producto', producto_id=producto.id)

        tiene_pedido_comprometido = Pedido.objects.filter(
            conversacion__producto=producto,
            estado__in=ESTADOS_PEDIDO_COMPROMETIDO
        ).exists()
        if tiene_pedido_comprometido:
            messages.error(
                request,
                'Tienes una entrega o envío acordado en curso para este producto. Complétalo o cancélalo en el chat antes de marcarlo como vendido manualmente.'
            )
            return redirect('detalle_producto', producto_id=producto.id)

        producto.marcar_como_vendido()

    _notificar_cierre_cascada_otros_chats(producto, None, request.user)
    messages.success(
        request,
        f'¡Felicidades! Marcaste "{producto.nombre}" como vendido. Seguirá visible como Vendido durante 7 días.'
    )
    return redirect('detalle_producto', producto_id=producto.id)


@login_required
def estadisticas_admin(request):
    """Dashboard de analítica de ventas, rotación, ranking por sede/categoría y comisiones Transbank."""
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    ahora = timezone.localtime(timezone.now())
    sede_id = request.GET.get('sede', '').strip()
    periodo = request.GET.get('periodo', 'todo').strip()
    mes_param = request.GET.get('mes', '').strip()
    anio_param = request.GET.get('anio', '').strip()

    fecha_inicio = None
    fecha_fin = None
    etiqueta_periodo = "Histórico Completo"

    meses_nombres = [
        "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
    ]

    if mes_param.isdigit():
        mes_int = int(mes_param)
        anio_int = int(anio_param) if anio_param.isdigit() else ahora.year
        if 1 <= mes_int <= 12 and 2020 <= anio_int <= 2100:
            periodo = 'personalizado'
            anio_param = str(anio_int)
            fecha_inicio = ahora.replace(year=anio_int, month=mes_int, day=1, hour=0, minute=0, second=0, microsecond=0)
            if mes_int == 12:
                fecha_fin = fecha_inicio.replace(year=anio_int + 1, month=1) - timedelta(microseconds=1)
            else:
                fecha_fin = fecha_inicio.replace(month=mes_int + 1) - timedelta(microseconds=1)
            etiqueta_periodo = f"{meses_nombres[mes_int]} {anio_int}"
    elif anio_param.isdigit():
        anio_int = int(anio_param)
        if 2020 <= anio_int <= 2100:
            periodo = 'personalizado'
            fecha_inicio = ahora.replace(year=anio_int, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            fecha_fin = ahora.replace(year=anio_int, month=12, day=31, hour=23, minute=59, second=59, microsecond=999999)
            etiqueta_periodo = f"Año {anio_int}"
    elif periodo == 'semana':
        inicio_semana = ahora - timedelta(days=ahora.weekday())
        fecha_inicio = inicio_semana.replace(hour=0, minute=0, second=0, microsecond=0)
        etiqueta_periodo = "Esta Semana"
    elif periodo == 'mes':
        fecha_inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        etiqueta_periodo = f"Este Mes ({meses_nombres[ahora.month]})"
    elif periodo == 'anio':
        fecha_inicio = ahora.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        etiqueta_periodo = f"Año {ahora.year}"
    else:
        periodo = 'todo'

    productos_qs = Producto.objects.filter(comision_pagada=True)
    pagos_qs = PagoComision.objects.filter(estado='pagado')

    if sede_id.isdigit():
        sede_int = int(sede_id)
        productos_qs = productos_qs.filter(sede_id=sede_int)
        pagos_qs = pagos_qs.filter(producto__sede_id=sede_int)

    if fecha_inicio:
        productos_qs = productos_qs.filter(fecha_creacion__gte=fecha_inicio)
        pagos_qs = pagos_qs.filter(fecha_pago__gte=fecha_inicio)
    if fecha_fin:
        productos_qs = productos_qs.filter(fecha_creacion__lte=fecha_fin)
        pagos_qs = pagos_qs.filter(fecha_pago__lte=fecha_fin)

    total_publicados = productos_qs.count()
    total_activos = productos_qs.filter(activo=True, vendido=False).count()
    total_vendidos = productos_qs.filter(vendido=True).count()
    tasa_conversion = round((total_vendidos / total_publicados) * 100, 1) if total_publicados > 0 else 0

    vendidos_con_fechas = productos_qs.filter(
        vendido=True,
        fecha_vendido__isnull=False,
        fecha_creacion__isnull=False
    ).annotate(
        duracion_venta=ExpressionWrapper(
            F('fecha_vendido') - F('fecha_creacion'),
            output_field=DurationField()
        )
    )

    promedio_td = vendidos_con_fechas.aggregate(promedio=Avg('duracion_venta'))['promedio']
    if promedio_td:
        total_horas = int(promedio_td.total_seconds() // 3600)
        dias = total_horas // 24
        horas = total_horas % 24
        if dias > 0:
            tiempo_promedio_texto = f"{dias} d {horas} h"
        else:
            minutos = int((promedio_td.total_seconds() % 3600) // 60)
            tiempo_promedio_texto = f"{horas} h {minutos} min"
    else:
        tiempo_promedio_texto = "Sin ventas aún"

    resumen_pagos = pagos_qs.aggregate(
        total_comisiones=Sum('monto_comision'),
        volumen_transado=Sum('monto_producto'),
        cantidad_transacciones=Count('id')
    )
    total_comisiones = resumen_pagos['total_comisiones'] or 0
    volumen_transado = resumen_pagos['volumen_transado'] or 0
    cantidad_tx = resumen_pagos['cantidad_transacciones'] or 0
    ticket_promedio_comision = round(total_comisiones / cantidad_tx) if cantidad_tx > 0 else 0

    filtro_prod_q = Q(productos__comision_pagada=True)
    filtro_pago_q = Q(productos__pago_comision__estado='pagado')
    if fecha_inicio:
        filtro_prod_q &= Q(productos__fecha_creacion__gte=fecha_inicio)
        filtro_pago_q &= Q(productos__pago_comision__fecha_pago__gte=fecha_inicio)
    if fecha_fin:
        filtro_prod_q &= Q(productos__fecha_creacion__lte=fecha_fin)
        filtro_pago_q &= Q(productos__pago_comision__fecha_pago__lte=fecha_fin)

    ranking_sedes_qs = Sede.objects.filter(activa=True).annotate(
        publicados=Count('productos', filter=filtro_prod_q, distinct=True),
        vendidos=Count('productos', filter=filtro_prod_q & Q(productos__vendido=True), distinct=True),
        activos=Count('productos', filter=filtro_prod_q & Q(productos__activo=True, productos__vendido=False), distinct=True),
        volumen=Sum('productos__pago_comision__monto_producto', filter=filtro_pago_q),
        recaudado=Sum('productos__pago_comision__monto_comision', filter=filtro_pago_q),
    ).filter(Q(publicados__gt=0) | Q(recaudado__gt=0)).order_by('-vendidos', '-recaudado')

    ranking_sedes = []
    for s in ranking_sedes_qs:
        s.tasa_conversion = round((s.vendidos / s.publicados) * 100, 1) if s.publicados > 0 else 0.0
        ranking_sedes.append(s)

    categorias_dict = dict(getattr(Producto._meta.get_field('categoria'), 'choices', None) or [])
    categorias_qs = productos_qs.values('categoria').annotate(
        publicados=Count('id', distinct=True),
        vendidos=Count('id', filter=Q(vendido=True), distinct=True),
        activos=Count('id', filter=Q(activo=True, vendido=False), distinct=True),
        recaudado=Sum('pago_comision__monto_comision', filter=Q(pago_comision__estado='pagado')),
        volumen=Sum('pago_comision__monto_producto', filter=Q(pago_comision__estado='pagado')),
    ).order_by('-vendidos', '-publicados')

    ranking_categorias = []
    for cat in categorias_qs:
        raw_cat = cat['categoria'] or 'Sin categoría'
        cat['nombre_legible'] = categorias_dict.get(raw_cat, raw_cat)
        cat['tasa_conversion'] = round((cat['vendidos'] / cat['publicados']) * 100, 1) if cat['publicados'] > 0 else 0.0
        ranking_categorias.append(cat)

    agrupar_por_dia = periodo in ('semana', 'mes') or bool(mes_param)
    serie_comisiones = defaultdict(int)
    serie_ventas = defaultdict(int)

    for pago in pagos_qs.values('fecha_pago', 'monto_comision'):
        if pago['fecha_pago']:
            dt_local = timezone.localtime(pago['fecha_pago'])
            clave = dt_local.strftime('%d/%m') if agrupar_por_dia else dt_local.strftime('%m/%Y')
            serie_comisiones[clave] += int(pago['monto_comision'] or 0)

    for prod in productos_qs.filter(vendido=True).values('fecha_vendido', 'fecha_creacion'):
        fecha_ref = prod['fecha_vendido'] or prod['fecha_creacion']
        if fecha_ref:
            dt_local = timezone.localtime(fecha_ref)
            clave = dt_local.strftime('%d/%m') if agrupar_por_dia else dt_local.strftime('%m/%Y')
            serie_ventas[clave] += 1

    claves_ordenadas = sorted(
        set(list(serie_comisiones.keys()) + list(serie_ventas.keys())),
        key=lambda x: tuple(reversed(x.split('/')))
    )

    chart_data = {
        'labels': claves_ordenadas,
        'comisiones': [serie_comisiones[k] for k in claves_ordenadas],
        'ventas': [serie_ventas[k] for k in claves_ordenadas],
        'cat_labels': [c['nombre_legible'] for c in ranking_categorias[:6]],
        'cat_vendidos': [c['vendidos'] for c in ranking_categorias[:6]],
    }

    ultimas_comisiones = pagos_qs.select_related('producto__sede', 'vendedor').order_by('-fecha_pago')[:100]
    sedes_activas = Sede.objects.filter(activa=True).order_by('nombre')
    anios_disponibles = list(range(2025, ahora.year + 1))

    return render(request, 'reportes/estadisticas_admin.html', {
        'sedes_activas': sedes_activas,
        'sede_seleccionada': sede_id,
        'periodo_seleccionado': periodo,
        'mes_seleccionado': mes_param,
        'anio_seleccionado': anio_param,
        'etiqueta_periodo': etiqueta_periodo,
        'anios_disponibles': anios_disponibles,
        'meses_opciones': [(str(i), meses_nombres[i]) for i in range(1, 13)],
        'total_publicados': total_publicados,
        'total_activos': total_activos,
        'total_vendidos': total_vendidos,
        'tasa_conversion': tasa_conversion,
        'tiempo_promedio_texto': tiempo_promedio_texto,
        'total_comisiones': total_comisiones,
        'volumen_transado': volumen_transado,
        'ticket_promedio_comision': ticket_promedio_comision,
        'ranking_sedes': ranking_sedes,
        'ranking_categorias': ranking_categorias,
        'ultimas_comisiones': ultimas_comisiones,
        'chart_data': chart_data,
    })


@login_required
def editar_producto(request, pk):
    producto = get_object_or_404(Producto, pk=pk)

    if producto.vendedor != request.user and not request.user.is_superuser:
        messages.error(request, 'No tienes permiso para editar este producto.')
        return redirect('home')

    if producto.vendido and not request.user.is_superuser:
        messages.error(request, 'No puedes editar un producto que ya fue marcado como vendido.')
        return redirect('detalle_producto', producto_id=producto.id)

    precio_original = producto.precio
    sede_original = producto.sede

    if request.method == "POST":
        form = ProductoForm(request.POST, request.FILES, instance=producto)
        if 'precio' in form.fields:
            form.fields['precio'].disabled = True
        if 'sede' in form.fields:
            form.fields['sede'].disabled = True
        if form.is_valid():
            producto_editado = form.save(commit=False)
            producto_editado.precio = precio_original
            producto_editado.sede = sede_original
            producto_editado.save()
            messages.success(request, f'¡El producto "{producto.nombre}" ha sido actualizado!')
            return redirect('detalle_producto', producto_id=producto.id)
    else:
        form = ProductoForm(instance=producto)
        if 'precio' in form.fields:
            form.fields['precio'].disabled = True
            form.fields['precio'].help_text = 'El precio no puede modificarse después de publicar el producto.'
        if 'sede' in form.fields:
            form.fields['sede'].disabled = True
            form.fields['sede'].help_text = 'La sede de entrega no puede modificarse después de publicar el producto.'

    return render(request, 'editar_producto.html', {'form': form, 'producto': producto})


@login_required
def eliminar_producto(request, pk):
    producto = get_object_or_404(Producto, pk=pk)

    if producto.vendedor != request.user and not request.user.is_superuser:
        messages.error(request, 'No tienes permiso para eliminar este producto.')
        return redirect('home')

    if request.method == "POST":
        with transaction.atomic():
            prod_lock = Producto.objects.select_for_update().get(id=producto.id)
            tiene_pedido_comprometido = Pedido.objects.filter(
                conversacion__producto=prod_lock,
                estado__in=ESTADOS_PEDIDO_COMPROMETIDO
            ).exists()
            if tiene_pedido_comprometido and not request.user.is_superuser:
                messages.error(
                    request,
                    'No puedes eliminar este producto mientras exista una reserva o envío activo en curso.'
                )
                return redirect('detalle_producto', producto_id=prod_lock.id)

            Pedido.objects.filter(
                conversacion__producto=prod_lock,
                estado='solicitado'
            ).update(estado='rechazado', fecha_actualizacion=timezone.now())

            prod_lock.activo = False
            prod_lock.reservado = False
            prod_lock.save(update_fields=['activo', 'reservado'])

        messages.success(request, f'El producto "{producto.nombre}" ha sido eliminado exitosamente.')

    return redirect('home')


def detalle_producto(request, producto_id):
    vendedor_attr = 'vendedor' if hasattr(Producto, 'vendedor') else 'usuario'
    base_producto = get_object_or_404(Producto, id=producto_id)
    _verificar_expiracion_pedidos(producto=base_producto)

    filter_kwargs = {f'producto__{vendedor_attr}': getattr(base_producto, vendedor_attr)}

    producto = get_object_or_404(
        Producto.objects.select_related(vendedor_attr, 'sede').annotate(
            valoracion_promedio=Avg('comentarios__valoracion')
        ),
        id=producto_id
    )

    vendedor_usuario = getattr(producto, vendedor_attr)
    es_staff = request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser)
    es_dueno = request.user.is_authenticated and request.user == vendedor_usuario
    tiene_pedido_previo = (
        request.user.is_authenticated
        and Pedido.objects.filter(conversacion__producto=producto, comprador=request.user).exists()
    )

    if (not producto.activo or not producto.comision_pagada) and not (es_dueno or es_staff or tiene_pedido_previo):
        messages.warning(request, 'Esta publicación no se encuentra disponible.')
        return redirect('home')

    reputacion = Comentario.objects.filter(
        **filter_kwargs
    ).aggregate(promedio=Avg('valoracion'))['promedio']

    reputacion_vendedor = float(reputacion) if reputacion is not None else 0.0
    promedio_producto = float(producto.valoracion_promedio) if producto.valoracion_promedio is not None else 0.0

    comentarios = producto.comentarios.select_related('usuario').all().order_by('-fecha_creacion')
    form_comentario = ComentarioForm()

    es_comprador_con_reserva = False
    conversacion_reserva = None
    if request.user.is_authenticated and producto.reservado:
        pedido_reserva = Pedido.objects.filter(
            conversacion__producto=producto,
            comprador=request.user,
            estado__in=ESTADOS_PEDIDO_COMPROMETIDO
        ).select_related('conversacion').first()
        if pedido_reserva:
            es_comprador_con_reserva = True
            conversacion_reserva = pedido_reserva.conversacion

    if request.method == 'POST' and 'comentario' in request.POST:
        if not request.user.is_authenticated:
            messages.error(request, "Debes iniciar sesión para publicar un comentario.")
            return redirect('login')

        if vendedor_usuario == request.user:
            messages.error(request, "No puedes valorar ni comentar como comprador tu propia publicación.")
            return redirect('detalle_producto', producto_id=producto.id)

        pedido_completado = Pedido.objects.filter(
            conversacion__producto=producto,
            comprador=request.user,
            estado='completado'
        ).last()

        if not pedido_completado:
            messages.error(
                request,
                "Solo los estudiantes que completaron la compra de este artículo pueden publicar una valoración."
            )
            return redirect('detalle_producto', producto_id=producto.id)

        if Comentario.objects.filter(pedido=pedido_completado, usuario=request.user).exists():
            messages.warning(request, "Ya enviaste tu valoración para esta compra.")
            return redirect('detalle_producto', producto_id=producto.id)

        form_comentario = ComentarioForm(request.POST)
        if form_comentario.is_valid():
            comentario = form_comentario.save(commit=False)
            comentario.usuario = request.user
            comentario.producto = producto
            comentario.pedido = pedido_completado
            comentario.save()
            messages.success(request, "¡Tu valoración y comentario fueron publicados exitosamente!")
            return redirect('detalle_producto', producto_id=producto.id)

    return render(request, 'detalle_producto.html', {
        'producto': producto,
        'vendedor_usuario': vendedor_usuario,
        'promedio_producto': promedio_producto,
        'reputacion_vendedor': reputacion_vendedor,
        'comentarios': comentarios,
        'form_comentario': form_comentario,
        'es_comprador_con_reserva': es_comprador_con_reserva,
        'conversacion_reserva': conversacion_reserva,
    })


@login_required
def toggle_favorito(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            producto_id = data.get('producto_id')
            producto = Producto.objects.get(id=producto_id, activo=True)
            favorito, created = Favorito.objects.get_or_create(usuario=request.user, producto=producto)

            if not created:
                favorito.delete()
                return JsonResponse({'success': True, 'action': 'removed'})

            return JsonResponse({'success': True, 'action': 'added'})
        except Producto.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Producto no encontrado.'}, status=404)
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    return JsonResponse({'success': False, 'error': 'Método no permitido.'}, status=405)


@login_required
def eliminar_comentario(request, comentario_id):
    comentario = get_object_or_404(Comentario, id=comentario_id)
    producto_id = comentario.producto.id

    if comentario.usuario != request.user and not request.user.is_superuser:
        messages.error(request, "No tienes permiso para eliminar este comentario.")
        return redirect('detalle_producto', producto_id=producto_id)

    if request.method == 'POST':
        comentario.delete()
        messages.success(request, "Comentario eliminado exitosamente.")

    return redirect('detalle_producto', producto_id=producto_id)


# ==============================================================================
# MENSAJERÍA Y CHAT EN TIEMPO REAL
# ==============================================================================

def calcular_distancia_metros(lat1, lon1, lat2, lon2):
    """Calcula la distancia en metros entre dos coordenadas GPS usando Haversine."""
    r = 6371000
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    dphi = math.radians(float(lat2) - float(lat1))
    dlambda = math.radians(float(lon2) - float(lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _obtener_conversacion_autorizada(request, conversation_id, permitir_admin=False):
    conversation = get_object_or_404(Conversation, id=conversation_id)
    es_participante = conversation.participants.filter(id=request.user.id).exists()
    if es_participante:
        return conversation, False
    if permitir_admin and (request.user.is_staff or request.user.is_superuser):
        return conversation, True
    return None, False


def _notificar_evento_tracking(conversation, emisor, texto_mensaje):
    """Emite notificación en tiempo real (WebSocket + WebPush) al receptor en hitos de entrega."""
    receptor = conversation.participants.exclude(id=emisor.id).first()
    channel_layer = get_channel_layer()

    if receptor:
        nombre_emisor = emisor.first_name or emisor.username
        async_to_sync(channel_layer.group_send)(
            f'notificaciones_{receptor.id}',
            {
                'type': 'send_notification',
                'data': {
                    'type': 'nueva_notificacion',
                    'latest_message': {
                        'id': 0,
                        'sender': nombre_emisor,
                        'content': texto_mensaje,
                        'conversation_id': conversation.id
                    }
                }
            }
        )
        try:
            payload = {
                'head': f'🚚 Seguimiento RexMarket - {nombre_emisor}',
                'body': texto_mensaje,
                'url': f'/conversations/{conversation.id}/'
            }
            send_user_notification(user=receptor, payload=payload, ttl=1000)
        except Exception as e:
            logger.warning(f"Error enviando Push de tracking: {e}")

    async_to_sync(channel_layer.group_send)(
        f'chat_{conversation.id}',
        {
            'type': 'chat_update',
            'sender_id': emisor.id
        }
    )


@login_required
def inbox_redirect(request):
    return redirect('conversations_list')


@login_required
def conversations_list(request):
    conversations = (
        Conversation.objects.filter(participants=request.user)
        .exclude(hidden_by=request.user)
        .select_related('producto', 'producto__vendedor')
        .prefetch_related('participants', 'messages__sender')
        .annotate(
            ultima_actividad=Max(
                'messages__timestamp',
                filter=Q(messages__bloqueado_moderacion=False)
            )
        )
        .order_by(F('ultima_actividad').desc(nulls_last=True), '-created_at')
        .distinct()
    )

    chat_items = []
    for conversation in conversations:
        other_participant = conversation.participants.exclude(id=request.user.id).first()
        if other_participant:
            mensajes_visibles = conversation.messages.filter(bloqueado_moderacion=False)
            unread_count = (
                mensajes_visibles.filter(is_read=False)
                .exclude(sender=request.user)
                .count()
            )
            chat_items.append({
                'conversation': conversation,
                'contact': other_participant,
                'producto': conversation.producto,
                'unread_count': unread_count,
                'last_message': mensajes_visibles.last(),
            })

    return render(request, 'mensajeria/conversations_list.html', {
        'chat_items': chat_items,
    })


@login_required
def start_conversation(request, recipient_id):
    if recipient_id == request.user.id:
        messages.info(request, "No puedes iniciar una conversación contigo mismo.")
        return redirect('conversations_list')

    recipient = get_object_or_404(User, id=recipient_id)
    producto_id = request.GET.get('producto_id', '').strip()
    producto = Producto.objects.filter(id=int(producto_id)).first() if producto_id.isdigit() else None

    if producto:
        if request.user != producto.vendedor and recipient != producto.vendedor:
            messages.error(request, "La conversación sobre este artículo debe incluir directamente al vendedor.")
            return redirect('detalle_producto', producto_id=producto.id)

        if not producto.activo or not producto.comision_pagada:
            messages.warning(request, "Esta publicación no se encuentra disponible.")
            return redirect('home')

        _verificar_expiracion_pedidos(producto=producto)

    qs = Conversation.objects.filter(participants=request.user).filter(participants=recipient)
    if producto:
        conversation = qs.filter(producto=producto).first()
    else:
        conversation = (
            qs.filter(Q(producto__isnull=True) | Q(producto__vendido=False))
            .order_by('-id')
            .first()
            or qs.order_by('-id').first()
        )

    if not conversation:
        if producto and (producto.vendido or producto.reservado):
            estado_txt = "vendido" if producto.vendido else "reservado por otro estudiante"
            messages.warning(request, f"Este producto ya se encuentra {estado_txt}.")
            return redirect('detalle_producto', producto_id=producto.id)
        conversation = Conversation.objects.create(producto=producto)
        conversation.participants.add(request.user, recipient)
    elif producto and not conversation.producto:
        conversation.producto = producto
        conversation.save(update_fields=['producto'])

    conversation.hidden_by.remove(request.user)
    return redirect('conversation_detail', conversation_id=conversation.id)


@login_required
def conversation_detail(request, conversation_id):
    conversation, es_auditoria_admin = _obtener_conversacion_autorizada(
        request, conversation_id, permitir_admin=True
    )
    if not conversation:
        messages.error(request, "No tienes acceso a esta conversación.")
        return redirect('conversations_list')

    _verificar_expiracion_pedidos(conversacion=conversation)

    participantes = list(conversation.participants.all())
    producto = conversation.producto

    if es_auditoria_admin:
        vendedor = producto.vendedor if producto else (participantes[0] if participantes else None)
        comprador = next((u for u in participantes if u != vendedor), participantes[-1] if participantes else None)
        other_participant = comprador or vendedor
    else:
        other_participant = conversation.participants.exclude(id=request.user.id).first()
        vendedor = producto.vendedor if producto else other_participant
        comprador = request.user if request.user != vendedor else other_participant

    sede_vendedor = (
        (producto.sede if producto and producto.sede else None)
        or (getattr(vendedor, 'perfil', None).sede if getattr(vendedor, 'perfil', None) else None)
    )
    sede_comprador = (
        getattr(comprador, 'perfil', None).sede if getattr(comprador, 'perfil', None) else None
    )

    sede_encuentro = sede_vendedor or sede_comprador or Sede.objects.filter(activa=True).first()
    es_misma_sede = True
    if sede_vendedor and sede_comprador and sede_vendedor.id != sede_comprador.id:
        es_misma_sede = False

    acepta_envio = producto.acepta_envio if producto else True

    return render(request, 'mensajeria/conversation_detail.html', {
        'conversation': conversation,
        'other_participant': other_participant,
        'participantes_chat': participantes,
        'es_auditoria_admin': es_auditoria_admin,
        'producto': producto,
        'sede_encuentro': sede_encuentro,
        'sede_vendedor': sede_vendedor,
        'sede_comprador': sede_comprador,
        'es_misma_sede': es_misma_sede,
        'acepta_envio': acepta_envio,
    })


@login_required
def get_conversation_messages(request, conversation_id):
    conversation, es_auditoria_admin = _obtener_conversacion_autorizada(
        request, conversation_id, permitir_admin=True
    )
    if not conversation:
        return JsonResponse({'error': 'No autorizado.'}, status=403)

    _verificar_expiracion_pedidos(conversacion=conversation)

    messages_qs = Message.objects.filter(
        conversation_id=conversation_id
    ).select_related('sender').order_by('timestamp')

    if not es_auditoria_admin:
        messages_qs = messages_qs.filter(bloqueado_moderacion=False)
        unread = messages_qs.exclude(sender=request.user).filter(is_read=False)
        if unread.exists():
            unread.update(is_read=True)

    message_data = [
        {
            'id': msg.id,
            'sender': {
                'id': msg.sender.id,
                'first_name': msg.sender.first_name or msg.sender.username,
                'last_name': msg.sender.last_name or '',
            },
            'content': msg.content,
            'timestamp': msg.timestamp.strftime('%d/%m/%Y %H:%M'),
            'is_read': msg.is_read
        }
        for msg in messages_qs
    ]

    pedido = Pedido.objects.filter(conversacion=conversation).last()
    user_id_serializacion = pedido.comprador.id if (es_auditoria_admin and pedido) else request.user.id
    producto = getattr(conversation, 'producto', None)

    return JsonResponse({
        'messages': message_data,
        'pedido': serializar_pedido(pedido, user_id_serializacion) if pedido else None,
        'producto_reservado': bool(producto.reservado) if producto else False,
        'producto_vendido': bool(producto.vendido) if producto else False,
        'es_auditoria_admin': es_auditoria_admin,
    })


@login_required
def send_message(request, conversation_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)

    if not request.user.is_active:
        return JsonResponse({'banned': True, 'error': 'Cuenta suspendida.'}, status=403)

    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No tienes permiso en esta conversación.'}, status=403)

    try:
        data = json.loads(request.body)
    except Exception:
        return JsonResponse({'error': 'JSON inválido.'}, status=400)

    content = str(data.get('content', '')).strip()
    if not content:
        return JsonResponse({'error': 'El mensaje no puede estar vacío.'}, status=400)

    if len(content) > MAX_LONGITUD_MENSAJE_CHAT:
        return JsonResponse({
            'error': f'El mensaje excede el límite permitido de {MAX_LONGITUD_MENSAJE_CHAT} caracteres.'
        }, status=400)

    analysis = analyze_message(content)
    is_red = analysis.get('is_red', False)
    is_yellow = analysis.get('is_yellow', False)
    reason = analysis.get('reason', 'Moderación automática')

    admin_user = User.objects.filter(is_superuser=True).first() or request.user

    if is_red:
        message_draft = Message.objects.create(
            sender=request.user,
            content=content,
            conversation=conversation,
            bloqueado_moderacion=True
        )
        MessageReport.objects.create(
            message=message_draft,
            reporter=admin_user,
            reason=f"🔴 [MODERACIÓN ROJA]: {reason}",
            tipo_alerta='ROJA',
            origen_deteccion=analysis.get('origen_deteccion', 'IA_ROBERTUITO'),
            termino_detectado=analysis.get('termino_detectado'),
            confianza_ia=analysis.get('confianza_ia'),
            resolved=False
        )

        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"notificaciones_{request.user.id}",
            {
                "type": "send_notification",
                "data": {
                    "type": "user_banned",
                    "message": "Tu cuenta ha sido suspendida por incumplir las normas de la comunidad."
                }
            }
        )

        perfil, _ = Perfil.objects.get_or_create(usuario=request.user)
        perfil.suspender_por_moderacion(motivo=reason)
        logout(request)

        return JsonResponse({
            'banned': True,
            'message': 'Tu cuenta ha sido suspendida por violar las normas de convivencia.'
        }, status=403)

    message = Message.objects.create(
        sender=request.user,
        content=content,
        conversation=conversation
    )

    conversation.hidden_by.clear()

    receptor = conversation.participants.exclude(id=request.user.id).first()
    channel_layer = get_channel_layer()

    if receptor:
        async_to_sync(channel_layer.group_send)(
            f'notificaciones_{receptor.id}',
            {
                'type': 'send_notification',
                'data': {
                    'type': 'nueva_notificacion',
                    'latest_message': {
                        'id': message.id,
                        'sender': request.user.first_name or request.user.username,
                        'content': message.content,
                        'conversation_id': conversation.id
                    }
                }
            }
        )

        try:
            nombre_emisor = request.user.first_name or request.user.username
            payload = {
                'head': f'💬 {nombre_emisor} en RexMarket',
                'body': message.content,
                'url': f'/conversations/{conversation.id}/'
            }
            send_user_notification(user=receptor, payload=payload, ttl=1000)
        except Exception as e:
            logger.warning(f"Error enviando notificación Push en vista: {e}")

    async_to_sync(channel_layer.group_send)(
        f'chat_{conversation.id}',
        {
            'type': 'chat_update',
            'sender_id': request.user.id
        }
    )

    if is_yellow:
        MessageReport.objects.create(
            message=message,
            reporter=admin_user,
            reason=f"🟡 [IA AMARILLA]: {reason}",
            tipo_alerta='AMARILLA',
            origen_deteccion=analysis.get('origen_deteccion', 'IA_ROBERTUITO'),
            termino_detectado=analysis.get('termino_detectado'),
            confianza_ia=analysis.get('confianza_ia'),
            resolved=False
        )

    return JsonResponse({'message': 'Mensaje enviado correctamente.'})


@login_required
def ocultar_conversacion(request, conversation_id):
    if request.method == 'POST':
        conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
        if conversation:
            conversation.hidden_by.add(request.user)
            messages.success(request, 'Conversación archivada/ocultada.')
    return redirect('conversations_list')


@login_required
def report_message(request, message_id):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            reason = str(data.get('reason', 'Sin motivo especificado')).strip()[:500]
            if len(reason) < 3:
                return JsonResponse({'error': 'Debes indicar un motivo válido.'}, status=400)

            message = get_object_or_404(Message, id=message_id, bloqueado_moderacion=False)

            if not message.conversation.participants.filter(id=request.user.id).exists():
                return JsonResponse({'error': 'No tienes acceso a este mensaje.'}, status=403)

            if message.sender == request.user:
                return JsonResponse({'error': 'No puedes reportar tus propios mensajes.'}, status=400)

            if MessageReport.objects.filter(message=message, reporter=request.user).exists():
                return JsonResponse({'error': 'Ya reportaste este mensaje anteriormente.'}, status=400)

            MessageReport.objects.create(
                message=message,
                reporter=request.user,
                reason=reason,
                tipo_alerta='AMARILLA'
            )
            return JsonResponse({'status': 'Denuncia enviada al equipo de administración exitosamente.'})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'Método no permitido.'}, status=405)


@login_required
def contar_mensajes_no_leidos(request):
    mensajes_no_leidos = Message.objects.filter(
        conversation__participants=request.user,
        is_read=False,
        bloqueado_moderacion=False
    ).exclude(sender=request.user)

    cantidad = mensajes_no_leidos.count()
    data = {'unread_count': cantidad}

    if cantidad > 0:
        ultimo = mensajes_no_leidos.order_by('-timestamp').first()
        if ultimo:
            data['latest_message'] = {
                'id': ultimo.id,
                'sender': ultimo.sender.first_name or ultimo.sender.username,
                'content': ultimo.content[:40] + ('...' if len(ultimo.content) > 40 else ''),
                'conversation_id': ultimo.conversation.id
            }

    return JsonResponse(data)


# ==============================================================================
# REPORTES Y ADMINISTRACIÓN
# ==============================================================================

def _notificar_admin_reporte(categoria, reporte_data):
    """Envía en tiempo real nuevos reportes al panel de administración vía Channels."""
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            "admin_reportes",
            {
                "type": "nuevo_reporte",
                "categoria": categoria,
                "reporte_data": reporte_data,
            }
        )
    except Exception as e:
        logger.warning(f"No se pudo emitir notificación WebSocket de reporte ({categoria}): {e}")


def _notificar_admin_reporte_cerrado(categoria, reporte_id):
    """Notifica a los administradores conectados que un reporte fue resuelto o eliminado."""
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            "admin_reportes",
            {
                "type": "reporte_resuelto",
                "categoria": categoria,
                "reporte_id": reporte_id,
            }
        )
    except Exception as e:
        logger.warning(f"No se pudo emitir cierre de reporte ({categoria}): {e}")


@login_required
def reportar_producto(request, producto_id):
    if request.method == 'POST':
        producto = get_object_or_404(Producto.objects.select_related('vendedor', 'sede'), id=producto_id)
        motivo = request.POST.get('motivo', '').strip()[:600]

        if producto.vendedor == request.user:
            return JsonResponse({
                'success': False,
                'message': 'No puedes reportar tu propia publicación.'
            }, status=400)

        if len(motivo) < 5:
            return JsonResponse({
                'success': False,
                'message': 'El motivo del reporte debe tener al menos 5 caracteres.'
            }, status=400)

        if Reporte.objects.filter(producto=producto, usuario=request.user).exists():
            return JsonResponse({
                'success': False,
                'message': 'Ya enviaste un reporte para este producto.'
            }, status=400)

        reporte = Reporte.objects.create(
            producto=producto,
            usuario=request.user,
            motivo=motivo
        )

        fecha_local = timezone.localtime(reporte.fecha_reporte)
        _notificar_admin_reporte('PRODUCTO', {
            'id': reporte.id,
            'producto_id': producto.id,
            'producto_nombre': producto.nombre,
            'producto_precio': str(producto.precio),
            'producto_imagen': producto.imagen.url if producto.imagen else '',
            'producto_sede': producto.sede.nombre if producto.sede else 'Sin sede',
            'producto_vendido': producto.vendido,
            'producto_activo': producto.activo,
            'vendedor_id': producto.vendedor.id,
            'vendedor_name': f"{producto.vendedor.first_name} {producto.vendedor.last_name}".strip() or producto.vendedor.username,
            'vendedor_username': producto.vendedor.username,
            'reporter_name': f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username,
            'reporter_username': request.user.username,
            'motivo': reporte.motivo,
            'fecha_display': fecha_local.strftime('%d/%m/%Y %H:%M'),
            'fecha_iso': fecha_local.strftime('%Y-%m-%d %H:%M:%S'),
        })

        return JsonResponse({
            'success': True,
            'message': 'El reporte ha sido enviado al equipo de administración.'
        })

    return JsonResponse({'success': False, 'message': 'Método no permitido.'}, status=405)


@login_required
def lista_reportes(request):
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    reportes = Reporte.objects.select_related('producto__vendedor', 'usuario').all().order_by('-fecha_reporte')
    return render(request, 'reportes/lista_reportes.html', {'reportes': reportes})


@login_required
def eliminar_reporte(request, reporte_id):
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    if request.method == 'POST':
        reporte = get_object_or_404(Reporte, id=reporte_id)
        rep_id = reporte.id
        prod_nombre = reporte.producto.nombre
        vendedor_obj = reporte.producto.vendedor
        reporte.delete()
        _notificar_admin_reporte_cerrado('PRODUCTO', rep_id)
        _registrar_auditoria(
            request,
            accion='REPORTE_PROD_DESCARTADO',
            detalle=f"Reporte #{rep_id} sobre producto '{prod_nombre}' descartado/eliminado.",
            usuario_afectado=vendedor_obj
        )
        messages.success(request, 'El reporte ha sido eliminado correctamente.')

    return redirect('lista_reportes')


@login_required
def reportar_comentario(request, comentario_id):
    if request.method == 'POST':
        motivo = request.POST.get('motivo', '').strip()[:600]
        if not motivo or len(motivo) < 5:
            return JsonResponse({
                'success': False,
                'message': 'El motivo debe tener al menos 5 caracteres.'
            }, status=400)

        comentario = get_object_or_404(Comentario.objects.select_related('producto', 'usuario'), id=comentario_id)
        if comentario.usuario == request.user:
            return JsonResponse({
                'success': False,
                'message': 'No puedes reportar tu propio comentario.'
            }, status=400)

        if ReporteComentario.objects.filter(comentario=comentario, usuario=request.user).exists():
            return JsonResponse({
                'success': False,
                'message': 'Ya reportaste este comentario anteriormente.'
            }, status=400)

        reporte = ReporteComentario.objects.create(
            comentario=comentario,
            usuario=request.user,
            motivo=motivo
        )

        fecha_local = timezone.localtime(reporte.fecha_creacion)
        _notificar_admin_reporte('COMENTARIO', {
            'id': reporte.id,
            'producto_id': comentario.producto.id,
            'producto_nombre': comentario.producto.nombre,
            'comentario_texto': comentario.comentario,
            'comentario_valoracion': comentario.valoracion,
            'autor_id': comentario.usuario.id,
            'autor_name': f"{comentario.usuario.first_name} {comentario.usuario.last_name}".strip() or comentario.usuario.username,
            'autor_username': comentario.usuario.username,
            'reporter_name': f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username,
            'reporter_username': request.user.username,
            'motivo': reporte.motivo,
            'fecha_display': fecha_local.strftime('%d/%m/%Y %H:%M'),
            'fecha_iso': fecha_local.strftime('%Y-%m-%d %H:%M:%S'),
        })

        return JsonResponse({
            'success': True,
            'message': 'El comentario ha sido reportado con éxito.'
        })

    return JsonResponse({'success': False, 'message': 'Método no permitido.'}, status=405)


@login_required
def listar_reportes_comentarios(request):
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    reportes_comentarios = ReporteComentario.objects.select_related(
        'comentario__producto', 'usuario'
    ).all().order_by('-fecha_creacion')

    return render(request, 'reportes/reportes_cometario.html', {
        'reportes_comentarios': reportes_comentarios
    })


@login_required
def eliminar_reporte_comentario(request, reporte_id):
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    if request.method == 'POST':
        reporte = get_object_or_404(ReporteComentario, id=reporte_id)
        rep_id = reporte.id
        autor_com = reporte.comentario.usuario
        prod_nombre = reporte.comentario.producto.nombre
        reporte.delete()
        _notificar_admin_reporte_cerrado('COMENTARIO', rep_id)
        _registrar_auditoria(
            request,
            accion='REPORTE_COM_DESCARTADO',
            detalle=f"Reporte #{rep_id} sobre comentario en '{prod_nombre}' descartado/eliminado.",
            usuario_afectado=autor_com
        )
        messages.success(request, 'El reporte sobre el comentario ha sido eliminado.')

    return redirect('reportes_comentarios')


@login_required
def lista_reportes_mensajes(request):
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    reportes_pendientes = MessageReport.objects.filter(resolved=False).select_related(
        'message__sender', 'message__conversation', 'reporter'
    ).order_by('-fecha_creacion')

    reportes_resueltos = MessageReport.objects.filter(resolved=True).select_related(
        'message__sender', 'message__conversation', 'reporter'
    ).order_by('-fecha_creacion')

    return render(request, 'reportes/reportes_mensajes.html', {
        'reportes_pendientes': reportes_pendientes,
        'reportes_resueltos': reportes_resueltos
    })


@login_required
def resolver_reporte_mensaje(request, reporte_id):
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    if request.method == 'POST':
        reporte = get_object_or_404(MessageReport, id=reporte_id)
        reporte.resolved = True
        reporte.save(update_fields=['resolved'])
        _notificar_admin_reporte_cerrado('MENSAJE', reporte.id)
        _registrar_auditoria(
            request,
            accion='REPORTE_MSG_RESUELTO',
            detalle=f"Reporte de mensaje #{reporte.id} marcado como resuelto.",
            usuario_afectado=reporte.message.sender
        )
        messages.success(request, 'El reporte ha sido marcado como resuelto.')

    return redirect('lista_reportes_mensajes')


@login_required
def eliminar_reporte_mensaje(request, reporte_id):
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    if request.method == 'POST':
        reporte = get_object_or_404(MessageReport, id=reporte_id)
        rep_id = reporte.id
        emisor = reporte.message.sender
        reporte.delete()
        _notificar_admin_reporte_cerrado('MENSAJE', rep_id)
        _registrar_auditoria(
            request,
            accion='REPORTE_MSG_ELIMINADO',
            detalle=f"Reporte de mensaje #{rep_id} eliminado definitivamente.",
            usuario_afectado=emisor
        )
        messages.success(request, 'El reporte ha sido eliminado definitivamente.')

    return redirect('lista_reportes_mensajes')


@login_required
def gestion_usuarios(request):
    if not request.user.is_superuser:
        raise PermissionDenied

    usuarios_activos = User.objects.filter(is_active=True).exclude(id=request.user.id).select_related('perfil__sede').order_by('-date_joined')
    usuarios_baneados = User.objects.filter(is_active=False).select_related('perfil__sede').order_by('-date_joined')
    apelaciones_pendientes = ApelacionSuspension.objects.filter(estado='pendiente').select_related('usuario__perfil__sede')
    apelaciones_historial = ApelacionSuspension.objects.exclude(estado='pendiente').select_related('usuario', 'revisado_por')[:25]

    return render(request, 'reportes/gestion_usuarios.html', {
        'usuarios_activos': usuarios_activos,
        'usuarios_baneados': usuarios_baneados,
        'apelaciones_pendientes': apelaciones_pendientes,
        'apelaciones_historial': apelaciones_historial,
    })


@login_required
@require_POST
def resolver_apelacion_admin(request, apelacion_id):
    """Permite al superusuario aprobar (reactivar cuenta) o rechazar una apelación con respaldo en bitácora."""
    if not request.user.is_superuser:
        raise PermissionDenied

    apelacion = get_object_or_404(ApelacionSuspension, id=apelacion_id)
    if apelacion.estado != 'pendiente':
        messages.info(request, 'Esta apelación ya fue resuelta previamente.')
        return redirect('gestion_usuarios')

    decision = request.POST.get('decision')
    respuesta = request.POST.get('respuesta_admin', '').strip()[:1000]

    if len(respuesta) < 8:
        messages.error(request, 'Debes escribir una resolución de al menos 8 caracteres.')
        return redirect('gestion_usuarios')

    usuario_target = apelacion.usuario
    apelacion.respuesta_admin = respuesta
    apelacion.revisado_por = request.user
    apelacion.fecha_resolucion = timezone.now()

    if decision == 'aprobar':
        apelacion.estado = 'aprobada'
        apelacion.save()

        usuario_target.is_active = True
        usuario_target.save(update_fields=['is_active'])

        perfil, _ = Perfil.objects.get_or_create(usuario=usuario_target)
        perfil.esta_baneado = False
        if perfil.advertencias >= 3:
            perfil.advertencias = 2
        perfil.save(update_fields=['esta_baneado', 'advertencias'])

        _registrar_auditoria(
            request,
            accion='REACTIVACION_USUARIO',
            detalle=f"Apelación #{apelacion.id} APROBADA para {usuario_target.username}. Resolución: {respuesta}",
            usuario_afectado=usuario_target
        )
        asunto = 'Apelación Aprobada - Cuenta Reactivada en RexMarket'
        cuerpo = (
            f"Hola {usuario_target.first_name or usuario_target.username},\n\n"
            f"Tu solicitud de apelación (#{apelacion.id}) ha sido APROBADA por el equipo de administración.\n"
            f"Observación: {respuesta}\n\n"
            f"Ya puedes volver a iniciar sesión en RexMarket."
        )
        messages.success(request, f'Apelación #{apelacion.id} aprobada. La cuenta {usuario_target.username} fue reactivada.')
    else:
        apelacion.estado = 'rechazada'
        apelacion.save()

        _registrar_auditoria(
            request,
            accion='SUSPENSION_USUARIO',
            detalle=f"Apelación #{apelacion.id} RECHAZADA para {usuario_target.username}. Motivo: {respuesta}",
            usuario_afectado=usuario_target
        )
        asunto = 'Resolución de Apelación - RexMarket'
        cuerpo = (
            f"Hola {usuario_target.first_name or usuario_target.username},\n\n"
            f"Tras revisar los antecedentes en nuestra bitácora, tu solicitud de apelación (#{apelacion.id}) ha sido RECHAZADA.\n"
            f"Motivo: {respuesta}\n\n"
            f"La suspensión de tu cuenta se mantiene vigente."
        )
        messages.warning(request, f'Apelación #{apelacion.id} rechazada. Se notificó al estudiante.')

    try:
        send_mail(
            asunto,
            cuerpo,
            getattr(settings, 'DEFAULT_FROM_EMAIL', 'oficialrexmarket@gmail.com'),
            [usuario_target.email],
            fail_silently=True,
        )
    except Exception as e:
        logger.warning(f"No se pudo enviar correo de resolución de apelación: {e}")

    return redirect('gestion_usuarios')


@login_required
def ver_perfil_admin(request, user_id):
    """Vista de alta seguridad: solo el superusuario puede consultar perfiles ajenos."""
    if not request.user.is_superuser:
        raise PermissionDenied

    usuario_target = get_object_or_404(User, id=user_id)
    perfil, _ = Perfil.objects.get_or_create(usuario=usuario_target)
    productos_usuario = Producto.objects.filter(vendedor=usuario_target).order_by('-id')

    return render(request, 'reportes/ver_perfil_admin.html', {
        'usuario_target': usuario_target,
        'perfil': perfil,
        'productos_usuario': productos_usuario
    })


@login_required
@require_POST
def sumar_advertencia_admin(request, user_id):
    if not request.user.is_superuser:
        raise PermissionDenied

    usuario_target = get_object_or_404(User, id=user_id)
    if usuario_target.id == request.user.id:
        messages.error(request, "No puedes advertirte a ti mismo.")
        return redirect('ver_perfil_admin', user_id=user_id)

    motivo = request.POST.get('motivo', '').strip()[:500] or "Incumplimiento de normas de convivencia / operación."
    perfil, _ = Perfil.objects.get_or_create(usuario=usuario_target)
    perfil.agregar_advertencia()

    _registrar_auditoria(
        request,
        accion='ADVERTENCIA_MANUAL',
        detalle=f"Se sumó advertencia a {usuario_target.username} (Total: {perfil.advertencias}/3). Motivo: {motivo}",
        usuario_afectado=usuario_target
    )

    messages.warning(request, f"Se ha registrado una advertencia a {usuario_target.username}. (Total: {perfil.advertencias}/3)")
    return redirect('ver_perfil_admin', user_id=user_id)


@login_required
def cambiar_estado_usuario(request, user_id):
    if not request.user.is_superuser:
        raise PermissionDenied

    if request.method == 'POST':
        usuario = get_object_or_404(User, id=user_id)
        if usuario.id != request.user.id:
            usuario.is_active = not usuario.is_active
            usuario.save(update_fields=['is_active'])

            perfil, _ = Perfil.objects.get_or_create(usuario=usuario)
            perfil.esta_baneado = not usuario.is_active
            if usuario.is_active and perfil.advertencias >= 3:
                perfil.advertencias = 2
            perfil.save(update_fields=['esta_baneado', 'advertencias'])

            estado = "activado" if usuario.is_active else "suspendido/baneado"
            accion_cod = 'REACTIVACION_USUARIO' if usuario.is_active else 'SUSPENSION_USUARIO'
            motivo = request.POST.get('motivo', '').strip()[:500] or f"Cambio manual de estado a {estado}."

            _registrar_auditoria(
                request,
                accion=accion_cod,
                detalle=f"Cuenta {usuario.username} ({usuario.email}) marcada como {estado}. Motivo: {motivo}",
                usuario_afectado=usuario
            )
            messages.success(request, f'El usuario {usuario.username} ha sido {estado}.')

    return redirect('gestion_usuarios')


@login_required
def eliminar_usuario_definitivo(request, user_id):
    if not request.user.is_superuser:
        raise PermissionDenied

    if request.method == 'POST':
        usuario = get_object_or_404(User, id=user_id)
        if usuario.id != request.user.id:
            rut_eliminado = usuario.username
            correo_eliminado = usuario.email
            nombre_completo = f"{usuario.first_name} {usuario.last_name}".strip()

            _registrar_auditoria(
                request,
                accion='ELIMINACION_USUARIO',
                detalle=f"Eliminación definitiva de cuenta RUT {rut_eliminado} ({nombre_completo} - {correo_eliminado}).",
                usuario_afectado=None
            )
            usuario.delete()
            messages.success(request, f'El usuario {rut_eliminado} fue eliminado definitivamente.')

    return redirect('gestion_usuarios')


def _obtener_ip_cliente(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _registrar_auditoria(request, accion, detalle, usuario_afectado=None, es_sistema=False):
    """Registra de forma inmutable cualquier intervención administrativa o sanción automática."""
    try:
        AuditoriaAdmin.objects.create(
            administrador=None if es_sistema else (request.user if request and request.user.is_authenticated else None),
            usuario_afectado=usuario_afectado,
            accion=accion,
            detalle=detalle,
            ip_origen=_obtener_ip_cliente(request) if request else None,
        )
    except Exception as e:
        logger.error(f"Error registrando auditoría ({accion}): {e}")


@login_required
def monitor_pedidos_admin(request):
    """Monitor central de entregas presenciales, envíos interregionales, inasistencias y disputas."""
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    _verificar_expiracion_pedidos()

    filtro_estado = request.GET.get('estado', '').strip()
    sede_id = request.GET.get('sede', '').strip()

    pedidos_qs = Pedido.objects.select_related(
        'conversacion__producto__sede',
        'comprador__perfil',
        'vendedor__perfil',
        'propuesto_por',
        'quien_llego_primero',
        'usuario_ausente'
    ).order_by('-id')

    if sede_id.isdigit():
        pedidos_qs = pedidos_qs.filter(conversacion__producto__sede_id=int(sede_id))

    if filtro_estado == 'activos':
        pedidos_qs = pedidos_qs.filter(estado__in=ESTADOS_PEDIDO_EN_CURSO)
    elif filtro_estado == 'completados':
        pedidos_qs = pedidos_qs.filter(estado='completado')
    elif filtro_estado == 'inasistencias':
        pedidos_qs = pedidos_qs.filter(estado='cancelado_inasistencia')
    elif filtro_estado == 'anulados':
        pedidos_qs = pedidos_qs.filter(estado='cancelado_admin')

    base_stats = Pedido.objects.all()
    if sede_id.isdigit():
        base_stats = base_stats.filter(conversacion__producto__sede_id=int(sede_id))

    stats = {
        'total': base_stats.count(),
        'activos': base_stats.filter(estado__in=ESTADOS_PEDIDO_EN_CURSO).count(),
        'completados': base_stats.filter(estado='completado').count(),
        'inasistencias': base_stats.filter(estado__in=['cancelado_inasistencia', 'cancelado_admin']).count(),
    }

    sedes_activas = Sede.objects.filter(activa=True)

    return render(request, 'reportes/monitor_pedidos.html', {
        'pedidos': pedidos_qs,
        'stats': stats,
        'sedes_activas': sedes_activas,
        'sede_seleccionada': sede_id,
        'filtro_estado': filtro_estado,
    })


@login_required
@require_POST
def intervenir_pedido_admin(request, pedido_id):
    """Permite al administrador anular un pedido en disputa/conflicto y liberar la reserva."""
    if not request.user.is_staff and not request.user.is_superuser:
        raise PermissionDenied

    motivo = request.POST.get('motivo_resolucion', '').strip()[:800]
    sancionar_a = request.POST.get('sancionar_usuario', 'ninguno')

    if len(motivo) < 8:
        messages.error(request, 'Debes ingresar una resolución administrativa de al menos 8 caracteres.')
        return redirect('monitor_pedidos_admin')

    if sancionar_a != 'ninguno' and not request.user.is_superuser:
        messages.error(request, 'Solo un superusuario puede aplicar advertencias disciplinarias a usuarios.')
        return redirect('monitor_pedidos_admin')

    with transaction.atomic():
        pedido = get_object_or_404(Pedido.objects.select_for_update(), id=pedido_id)

        if pedido.estado in ESTADOS_PEDIDO_CERRADOS:
            messages.warning(request, f'El pedido #{pedido.id} ya se encuentra cerrado ({pedido.get_estado_display()}).')
            return redirect('monitor_pedidos_admin')

        estado_anterior = pedido.get_estado_display()
        pedido.estado = 'cancelado_admin'
        pedido.resolucion_admin = f"[{timezone.localtime():%d/%m/%Y %H:%M}] {request.user.username}: {motivo}"
        pedido.save()

        producto = pedido.conversacion.producto if pedido.conversacion else None
        if producto and not producto.vendido:
            prod_lock = Producto.objects.select_for_update().get(id=producto.id)
            prod_lock.liberar_reserva()
            _notificar_liberacion_reserva_otros_chats(prod_lock, pedido.conversacion, pedido.vendedor)

    usuario_sancionado = None
    texto_sancion = "Sin advertencia adicional."
    if sancionar_a == 'comprador':
        usuario_sancionado = pedido.comprador
    elif sancionar_a == 'vendedor':
        usuario_sancionado = pedido.vendedor

    if usuario_sancionado:
        perfil_sancionado, _ = Perfil.objects.get_or_create(usuario=usuario_sancionado)
        perfil_sancionado.agregar_advertencia()
        texto_sancion = f"Se sumó +1 advertencia a {usuario_sancionado.username} ({perfil_sancionado.advertencias}/3)."

    producto_nombre = producto.nombre if producto else f"Pedido #{pedido.id}"
    msg_admin = f"🛡️ [Resolución Administrativa]: El pedido fue anulado por administración. Motivo: {motivo}"
    Message.objects.create(
        conversation=pedido.conversacion,
        sender=request.user,
        content=msg_admin
    )
    if pedido.conversacion:
        _notificar_evento_tracking(pedido.conversacion, request.user, msg_admin)

    _registrar_auditoria(
        request,
        accion='INTERVENCION_PEDIDO',
        detalle=f"Pedido #{pedido.id} ({producto_nombre}) anulado desde estado '{estado_anterior}'. Motivo: {motivo}. {texto_sancion}",
        usuario_afectado=usuario_sancionado
    )

    messages.success(request, f'Pedido #{pedido.id} anulado, reserva liberada y registrado en auditoría.')
    return redirect('monitor_pedidos_admin')


@login_required
def bitacora_auditoria_admin(request):
    """Bitácora inmutable de intervenciones administrativas para respaldo ante apelaciones."""
    if not request.user.is_superuser:
        raise PermissionDenied

    accion_filtro = request.GET.get('accion', '').strip()
    registros = AuditoriaAdmin.objects.select_related('administrador', 'usuario_afectado').all()

    if accion_filtro:
        registros = registros.filter(accion=accion_filtro)

    return render(request, 'reportes/bitacora_auditoria.html', {
        'registros': registros,
        'acciones_choices': AuditoriaAdmin.ACCIONES,
        'accion_seleccionada': accion_filtro,
    })


# ==============================================================================
# ENTREGA PRESENCIAL (MAPA + QR EXCLUSIVO) Y ENVÍOS INTERREGIONALES
# ==============================================================================

def serializar_pedido(pedido, user_id=None):
    if not pedido:
        return None

    segundos_esperados = 0
    if pedido.fecha_llegada_sitio:
        segundos_esperados = int((timezone.now() - pedido.fecha_llegada_sitio).total_seconds())

    segundos_restantes_expiracion = 0
    if pedido.estado == 'solicitado' and pedido.fecha_creacion:
        transcurridos = (timezone.now() - pedido.fecha_creacion).total_seconds()
        segundos_restantes_expiracion = max(0, int(TIEMPO_EXPIRACION_SOLICITUD_SEGUNDOS - transcurridos))

    ya_valorado = Comentario.objects.filter(
        pedido=pedido,
        usuario=pedido.comprador
    ).exists()

    es_comprador = (user_id == pedido.comprador.id)

    return {
        'id': pedido.id,
        'tipo_entrega': pedido.tipo_entrega,
        'lugar_nombre': pedido.lugar_nombre,
        'latitud': pedido.latitud,
        'longitud': pedido.longitud,
        'empresa_envio': pedido.empresa_envio,
        'codigo_seguimiento': pedido.codigo_seguimiento,
        'direccion_envio': pedido.direccion_envio,
        'estado': pedido.estado,
        'estado_display': pedido.get_estado_display(),
        'token_qr': pedido.token_qr if es_comprador else None,
        'comprador_id': pedido.comprador.id,
        'vendedor_id': pedido.vendedor.id,
        'propuesto_por_id': pedido.propuesto_por.id if pedido.propuesto_por else None,
        'quien_llego_primero_id': pedido.quien_llego_primero.id if pedido.quien_llego_primero else None,
        'segundos_esperados': segundos_esperados,
        'segundos_restantes_expiracion': segundos_restantes_expiracion,
        'ya_valorado': ya_valorado,
    }


@login_required
@require_POST
def verificar_expiracion_chat(request, conversation_id):
    """Endpoint ligero para refrescar cuando el contador de 1 hora llega a 0 en el cliente."""
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No autorizado.'}, status=403)

    _verificar_expiracion_pedidos(conversacion=conversation)
    pedido = Pedido.objects.filter(conversacion=conversation).last()
    return JsonResponse({
        'status': 'ok',
        'pedido': serializar_pedido(pedido, request.user.id) if pedido else None
    })


@login_required
@require_POST
def crear_punto_entrega(request, conversation_id):
    """Crea propuesta de entrega presencial (con token QR único) o de envío interregional."""
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No tienes acceso a esta conversación.'}, status=403)

    producto = getattr(conversation, 'producto', None)
    if not producto:
        return JsonResponse({'error': 'Esta conversación no tiene un producto asociado para coordinar entrega.'}, status=400)

    _verificar_expiracion_pedidos(producto=producto)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Formato JSON inválido.'}, status=400)

    vendedor = producto.vendedor
    comprador = conversation.participants.exclude(id=vendedor.id).first()
    if not comprador or request.user not in (vendedor, comprador):
        return JsonResponse({'error': 'No se pudo identificar a los participantes de la venta.'}, status=400)

    tipo_entrega = data.get('tipo_entrega', 'presencial')
    if tipo_entrega not in ('presencial', 'envio'):
        return JsonResponse({'error': 'Tipo de entrega no válido.'}, status=400)

    if tipo_entrega == 'envio':
        if not producto.acepta_envio:
            return JsonResponse({'error': 'Este producto no acepta envíos a otras regiones.'}, status=400)

        empresa_envio = str(data.get('empresa_envio', '')).strip()[:100]
        direccion_envio = str(data.get('direccion_envio', '')).strip()[:255]

        if not empresa_envio or not direccion_envio:
            return JsonResponse({'error': 'Debes indicar la empresa de transporte y la dirección o sucursal de destino.'}, status=400)

        with transaction.atomic():
            prod_lock = Producto.objects.select_for_update().get(id=producto.id)
            if prod_lock.vendido:
                return JsonResponse({'error': 'Este producto ya fue vendido.'}, status=400)
            if prod_lock.reservado:
                return JsonResponse({'error': 'Este producto ya se encuentra reservado por otro comprador.'}, status=400)

            pedido_activo = Pedido.objects.select_for_update().filter(
                conversacion=conversation,
                estado__in=ESTADOS_PEDIDO_EN_CURSO
            ).first()
            if pedido_activo:
                return JsonResponse({'error': 'Ya existe una coordinación de entrega activa.'}, status=400)

            pedido = Pedido.objects.create(
                conversacion=conversation,
                vendedor=vendedor,
                comprador=comprador,
                tipo_entrega='envio',
                lugar_nombre=f"Envío vía {empresa_envio}",
                empresa_envio=empresa_envio,
                direccion_envio=direccion_envio,
                propuesto_por=request.user,
                estado='solicitado'
            )

        msg_texto = f"📦 Propuso envío por {empresa_envio} hacia: {direccion_envio} (expira en 1 hora si no hay respuesta)"
        Message.objects.create(
            conversation=conversation,
            sender=request.user,
            content=msg_texto
        )
        _notificar_evento_tracking(conversation, request.user, msg_texto)
        return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})

    lugar_nombre = str(data.get('lugar_nombre', '')).strip()[:150]
    if not lugar_nombre:
        return JsonResponse({'error': 'Debes escribir una referencia del punto de encuentro dentro de la sede.'}, status=400)

    try:
        lat = float(data.get('latitud'))
        lng = float(data.get('longitud'))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Coordenadas inválidas en el mapa.'}, status=400)

    if not (math.isfinite(lat) and math.isfinite(lng)) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return JsonResponse({'error': 'Coordenadas geográficas fuera de rango.'}, status=400)

    sede_obj = (
        producto.sede
        or (getattr(vendedor, 'perfil', None).sede if getattr(vendedor, 'perfil', None) else None)
    )
    if sede_obj and sede_obj.latitud is not None and sede_obj.longitud is not None:
        distancia = calcular_distancia_metros(sede_obj.latitud, sede_obj.longitud, lat, lng)
        if not math.isfinite(distancia) or distancia > sede_obj.radio_permitido_metros:
            return JsonResponse({
                'error': f'El punto elegido está a {int(distancia) if math.isfinite(distancia) else "?"}m de {sede_obj.nombre}. Debe estar dentro del perímetro de {sede_obj.radio_permitido_metros}m.'
            }, status=400)

    with transaction.atomic():
        prod_lock = Producto.objects.select_for_update().get(id=producto.id)
        if prod_lock.vendido:
            return JsonResponse({'error': 'Este producto ya fue vendido.'}, status=400)
        if prod_lock.reservado:
            return JsonResponse({'error': 'Este producto ya se encuentra reservado por otro comprador.'}, status=400)

        pedido_activo = Pedido.objects.select_for_update().filter(
            conversacion=conversation,
            estado__in=ESTADOS_PEDIDO_EN_CURSO
        ).first()
        if pedido_activo:
            return JsonResponse({'error': 'Ya existe una coordinación de entrega activa.'}, status=400)

        token_qr_unico = f"RXM-QR-{uuid.uuid4().hex}"
        pedido = Pedido.objects.create(
            conversacion=conversation,
            vendedor=vendedor,
            comprador=comprador,
            tipo_entrega='presencial',
            lugar_nombre=lugar_nombre,
            latitud=str(lat),
            longitud=str(lng),
            token_qr=token_qr_unico,
            propuesto_por=request.user,
            estado='solicitado'
        )

    msg_texto = f"📍 Propuso punto de encuentro en {sede_obj.nombre if sede_obj else 'Sede'}: {pedido.lugar_nombre} (expira en 1 hora si no hay respuesta)"
    Message.objects.create(
        conversation=conversation,
        sender=request.user,
        content=msg_texto
    )
    _notificar_evento_tracking(conversation, request.user, msg_texto)

    return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})


@login_required
@require_POST
def responder_solicitud_punto(request, conversation_id):
    """
    Permite a la contraparte aceptar o rechazar la propuesta de entrega.
    Al aceptar, bloquea atómicamente el stock en estado 'Reservado' y rechaza otras solicitudes.
    """
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No tienes acceso a esta conversación.'}, status=403)

    _verificar_expiracion_pedidos(conversacion=conversation)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Formato JSON inválido.'}, status=400)

    aceptar = bool(data.get('aceptar', False))
    producto = getattr(conversation, 'producto', None)

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().filter(
            conversacion=conversation,
            estado='solicitado'
        ).last()

        if not pedido or pedido.propuesto_por == request.user:
            return JsonResponse({'error': 'La solicitud ya expiró o no tienes propuestas pendientes.'}, status=400)

        if aceptar:
            if producto:
                prod_lock = Producto.objects.select_for_update().get(id=producto.id)
                if prod_lock.vendido or prod_lock.reservado:
                    pedido.estado = 'rechazado'
                    pedido.save(update_fields=['estado', 'fecha_actualizacion'])
                    return JsonResponse({
                        'error': 'Este producto ya fue reservado o vendido a otro usuario.'
                    }, status=400)

                prod_lock.reservar()

            pedido.estado = 'acordado'
            pedido.save(update_fields=['estado', 'fecha_actualizacion'])

            if producto:
                _cancelar_otras_propuestas_al_reservar(producto, conversation, pedido.vendedor)

            msg_texto = f"✅ Aceptó el punto de encuentro en: {pedido.lugar_nombre} (Publicación marcada como Reservada)"
        else:
            pedido.estado = 'rechazado'
            pedido.save(update_fields=['estado', 'fecha_actualizacion'])
            msg_texto = "❌ Rechazó la propuesta de entrega."

    Message.objects.create(
        conversation=conversation,
        sender=request.user,
        content=msg_texto
    )
    _notificar_evento_tracking(conversation, request.user, msg_texto)

    return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})


@login_required
@require_POST
def actualizar_estado_tracking(request, conversation_id):
    """
    Actualiza estados dinámicos (en_camino, en_sitio) con bloqueo atómico y validación GPS.
    """
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No tienes acceso a esta conversación.'}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Formato JSON inválido.'}, status=400)

    nuevo_estado = data.get('estado')
    if nuevo_estado not in ('en_camino', 'en_sitio'):
        return JsonResponse({'error': 'Transición de estado no permitida.'}, status=400)

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().filter(
            conversacion=conversation,
            tipo_entrega='presencial'
        ).last()

        if not pedido or pedido.estado not in ['acordado', 'en_camino', 'en_sitio']:
            return JsonResponse({'error': 'No existe una entrega presencial activa.'}, status=400)

        transiciones_validas = {
            'acordado': ['en_camino'],
            'en_camino': ['en_camino', 'en_sitio'],
            'en_sitio': ['en_sitio'],
        }

        if nuevo_estado not in transiciones_validas.get(pedido.estado, []):
            return JsonResponse({'error': 'Transición de estado no permitida.'}, status=400)

        cache_camino_key = f"pedido_en_camino_{pedido.id}"
        cache_sitio_key = f"pedido_en_sitio_{pedido.id}"
        usuarios_en_camino = set(cache.get(cache_camino_key, []))
        usuarios_en_sitio = set(cache.get(cache_sitio_key, []))

        if nuevo_estado == 'en_sitio' and ('latitud' in data and 'longitud' in data):
            try:
                lat_usr = float(data.get('latitud'))
                lng_usr = float(data.get('longitud'))
                if math.isfinite(lat_usr) and math.isfinite(lng_usr) and pedido.latitud and pedido.longitud:
                    dist_punto = calcular_distancia_metros(pedido.latitud, pedido.longitud, lat_usr, lng_usr)
                    if dist_punto > 350:
                        return JsonResponse({
                            'error': f'Aún estás a {int(dist_punto)}m del punto acordado. Acércate al campus antes de confirmar llegada.'
                        }, status=400)
            except (TypeError, ValueError):
                pass

        if nuevo_estado == 'en_camino':
            if request.user.id in usuarios_en_camino and pedido.estado in ('en_camino', 'en_sitio'):
                return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})
            usuarios_en_camino.add(request.user.id)
            cache.set(cache_camino_key, list(usuarios_en_camino), timeout=86400)
            if pedido.estado == 'acordado':
                pedido.estado = 'en_camino'

        elif nuevo_estado == 'en_sitio':
            if request.user.id in usuarios_en_sitio:
                return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})

            usuarios_en_sitio.add(request.user.id)
            cache.set(cache_sitio_key, list(usuarios_en_sitio), timeout=86400)

            pedido.estado = 'en_sitio'
            if not pedido.fecha_llegada_sitio:
                pedido.fecha_llegada_sitio = timezone.now()
                pedido.quien_llego_primero = request.user

        pedido.save()

    estado_textos = {
        'en_camino': '🚶 Voy en camino al punto de encuentro.',
        'en_sitio': '📍 Llegué al punto de encuentro pactado.',
    }
    msg_texto = estado_textos[nuevo_estado]
    Message.objects.create(
        conversation=conversation,
        sender=request.user,
        content=msg_texto
    )
    _notificar_evento_tracking(conversation, request.user, msg_texto)

    return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})


@login_required
@require_POST
def reportar_inasistencia(request, conversation_id):
    """Maneja el vencimiento del tiempo de espera, libera la reserva del producto y sanciona reincidencia."""
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No tienes acceso a esta conversación.'}, status=403)

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().filter(
            conversacion=conversation,
            estado='en_sitio'
        ).last()

        if not pedido or not pedido.fecha_llegada_sitio:
            return JsonResponse({'error': 'No existe un registro de espera activo.'}, status=400)

        if pedido.quien_llego_primero != request.user:
            return JsonResponse({'error': 'Solo el usuario que llegó primero al punto de encuentro puede realizar esta acción.'}, status=403)

        otro_usuario = pedido.vendedor if request.user == pedido.comprador else pedido.comprador
        usuarios_en_sitio = set(cache.get(f"pedido_en_sitio_{pedido.id}", []))
        otro_confirmo_en_chat = Message.objects.filter(
            conversation=conversation,
            sender=otro_usuario,
            content='📍 Llegué al punto de encuentro pactado.',
            timestamp__gte=pedido.fecha_creacion
        ).exists()
        if len(usuarios_en_sitio) >= 2 or otro_confirmo_en_chat:
            return JsonResponse({'error': 'Ambos participantes ya confirmaron llegada en el punto de encuentro.'}, status=400)

        segundos_esperados = (timezone.now() - pedido.fecha_llegada_sitio).total_seconds()
        if segundos_esperados < TIEMPO_ESPERA_INASISTENCIA_SEGUNDOS:
            faltantes = int(TIEMPO_ESPERA_INASISTENCIA_SEGUNDOS - segundos_esperados)
            minutos = faltantes // 60
            segs = faltantes % 60
            return JsonResponse({
                'error': f'Debes esperar al menos {TIEMPO_ESPERA_INASISTENCIA_SEGUNDOS // 60} minutos en el punto. Faltan {minutos}m {segs}s.'
            }, status=400)

        usuario_ausente = otro_usuario
        pedido.estado = 'cancelado_inasistencia'
        pedido.usuario_ausente = usuario_ausente
        pedido.save()

        producto = getattr(conversation, 'producto', None)
        if producto and not producto.vendido:
            prod_lock = Producto.objects.select_for_update().get(id=producto.id)
            prod_lock.liberar_reserva()
            _notificar_liberacion_reserva_otros_chats(prod_lock, conversation, pedido.vendedor)

    perfil_ausente, _ = Perfil.objects.get_or_create(usuario=usuario_ausente)
    perfil_ausente.inasistencias += 1
    perfil_ausente.save(update_fields=['inasistencias'])

    penalizado_auto = False
    if perfil_ausente.inasistencias >= 2 and perfil_ausente.inasistencias % 2 == 0:
        perfil_ausente.agregar_advertencia()
        penalizado_auto = True
        _registrar_auditoria(
            request=None,
            accion='PENALIZACION_INASISTENCIA',
            detalle=(
                f"Penalización automática: {usuario_ausente.username} acumuló {perfil_ausente.inasistencias} "
                f"inasistencias confirmadas (Pedido #{pedido.id}). Advertencias actuales: {perfil_ausente.advertencias}/3."
            ),
            usuario_afectado=usuario_ausente,
            es_sistema=True
        )

    if request.user == pedido.comprador:
        if producto and not Comentario.objects.filter(pedido=pedido, usuario=request.user).exists():
            Comentario.objects.create(
                producto=producto,
                usuario=request.user,
                pedido=pedido,
                valoracion=1,
                comentario=f"Transacción fallida por inasistencia: El vendedor ({pedido.vendedor.first_name or pedido.vendedor.username}) no se presentó al punto."
            )
        msg_texto = "⚠️ Transacción cancelada por el comprador debido a inasistencia del vendedor (1★ asignada)."
    else:
        msg_texto = "⚠️ Transacción cancelada por el vendedor debido a inasistencia del comprador. El producto vuelve a estar disponible."

    if penalizado_auto:
        msg_texto += f" El sistema aplicó +1 advertencia automática por reincidencia ({perfil_ausente.advertencias}/3)."

    Message.objects.create(
        conversation=conversation,
        sender=request.user,
        content=msg_texto
    )
    _notificar_evento_tracking(conversation, request.user, msg_texto)

    return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})


@login_required
@require_POST
def validar_qr_entrega(request, conversation_id):
    """
    Valida exclusivamente mediante escaneo QR presencial en campus, marca el producto
    como vendido y cierra en cascada los demás chats abiertos por el artículo.
    """
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No tienes acceso a esta conversación.'}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'valido': False, 'error': 'Formato JSON inválido.'}, status=400)

    qr_escaneado = str(data.get('qr_token', '')).strip()

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().filter(
            conversacion=conversation,
            tipo_entrega='presencial',
            estado='en_sitio'
        ).last()

        if not pedido:
            return JsonResponse({
                'valido': False,
                'error': 'Ambos deben confirmar llegada ("Llegué al punto") antes de escanear el código QR.'
            }, status=400)

        if request.user != pedido.vendedor:
            return JsonResponse({'valido': False, 'error': 'Solo el vendedor puede escanear y validar el código QR.'}, status=403)

        cache_key = f"qr_intentos_{pedido.id}_{request.user.id}"
        intentos = cache.get(cache_key, 0)
        if intentos >= 8:
            return JsonResponse({'valido': False, 'error': 'Demasiados intentos inválidos. Espera 10 minutos.'}, status=429)

        token_esperado = str(pedido.token_qr or '').strip()

        if qr_escaneado and token_esperado and hmac.compare_digest(qr_escaneado, token_esperado):
            cache.delete(cache_key)
            pedido.estado = 'completado'
            pedido.save(update_fields=['estado', 'fecha_actualizacion'])

            producto_pedido = getattr(conversation, 'producto', None)
            if producto_pedido:
                prod_lock = Producto.objects.select_for_update().get(id=producto_pedido.id)
                if not prod_lock.vendido:
                    prod_lock.marcar_como_vendido()
                    _notificar_cierre_cascada_otros_chats(prod_lock, conversation, request.user)

            msg_texto = "✅ ¡Código QR verificado en campus! Entrega presencial confirmada y transacción completada."
            Message.objects.create(
                conversation=conversation,
                sender=request.user,
                content=msg_texto
            )
            _notificar_evento_tracking(conversation, request.user, msg_texto)

            return JsonResponse({'valido': True, 'pedido': serializar_pedido(pedido, request.user.id)})

        cache.set(cache_key, intentos + 1, timeout=600)

    return JsonResponse({
        'valido': False,
        'error': 'El código QR escaneado no corresponde a este pedido activo.'
    }, status=400)


@login_required
@require_POST
def despachar_pedido_envio(request, conversation_id):
    """Permite al vendedor registrar el código de seguimiento de la encomienda."""
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No tienes acceso a esta conversación.'}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Formato JSON inválido.'}, status=400)

    codigo = str(data.get('codigo_seguimiento', '')).strip()[:100]
    if len(codigo) < 4:
        return JsonResponse({'error': 'Ingresa un código de seguimiento válido.'}, status=400)

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().filter(
            conversacion=conversation,
            tipo_entrega='envio',
            estado='acordado'
        ).last()
        if not pedido:
            return JsonResponse({'error': 'No hay un envío acordado pendiente de despacho.'}, status=400)

        if request.user != pedido.vendedor:
            return JsonResponse({'error': 'Solo el vendedor puede registrar el código de despacho.'}, status=403)

        pedido.codigo_seguimiento = codigo
        pedido.estado = 'enviado'
        pedido.save()

    msg_texto = f"🚚 Paquete despachado por {pedido.empresa_envio}. Código de seguimiento: {codigo}"
    Message.objects.create(
        conversation=conversation,
        sender=request.user,
        content=msg_texto
    )
    _notificar_evento_tracking(conversation, request.user, msg_texto)

    return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})


@login_required
@require_POST
def confirmar_recepcion_envio(request, conversation_id):
    """Permite al comprador confirmar recepción del paquete, marca vendido y cierra en cascada."""
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'error': 'No tienes acceso a esta conversación.'}, status=403)

    with transaction.atomic():
        pedido = Pedido.objects.select_for_update().filter(
            conversacion=conversation,
            tipo_entrega='envio',
            estado='enviado'
        ).last()
        if not pedido:
            return JsonResponse({'error': 'No hay un envío en tránsito para confirmar.'}, status=400)

        if request.user != pedido.comprador:
            return JsonResponse({'error': 'Solo el comprador puede confirmar la recepción del paquete.'}, status=403)

        pedido.estado = 'completado'
        pedido.save()

        producto_pedido = getattr(conversation, 'producto', None)
        if producto_pedido:
            prod_lock = Producto.objects.select_for_update().get(id=producto_pedido.id)
            if not prod_lock.vendido:
                prod_lock.marcar_como_vendido()
                _notificar_cierre_cascada_otros_chats(prod_lock, conversation, pedido.vendedor)

    msg_texto = "✅ ¡El comprador confirmó la recepción del paquete! Transacción completada."
    Message.objects.create(
        conversation=conversation,
        sender=request.user,
        content=msg_texto
    )
    _notificar_evento_tracking(conversation, request.user, msg_texto)

    return JsonResponse({'status': 'ok', 'pedido': serializar_pedido(pedido, request.user.id)})


@login_required
@require_POST
def crear_comentario_valoracion(request, conversation_id):
    """Guarda la calificación (1-5 estrellas) y reseña del comprador tras la entrega exitosa."""
    conversation, _ = _obtener_conversacion_autorizada(request, conversation_id)
    if not conversation:
        return JsonResponse({'status': 'error', 'message': 'No tienes acceso a esta conversación.'}, status=403)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'status': 'error', 'message': 'Formato JSON inválido.'}, status=400)

    pedido = Pedido.objects.filter(conversacion=conversation, estado='completado').last()
    if not pedido:
        return JsonResponse({'status': 'error', 'message': 'No existe un pedido completado en esta conversación.'}, status=400)

    if request.user != pedido.comprador:
        return JsonResponse({'status': 'error', 'message': 'Solo el comprador tiene permiso para valorar la entrega.'}, status=403)

    if Comentario.objects.filter(pedido=pedido, usuario=request.user).exists():
        return JsonResponse({'status': 'error', 'message': 'Ya enviaste una valoración para esta entrega.'}, status=400)

    try:
        valoracion_num = int(data.get('valoracion', 5))
    except (TypeError, ValueError):
        valoracion_num = 5
    valoracion_num = max(1, min(5, valoracion_num))

    comentario_texto = str(data.get('comentario', '')).strip()[:1000]
    if not comentario_texto:
        comentario_texto = "¡Transacción completada exitosamente!"

    producto = getattr(conversation, 'producto', None)
    if not producto:
        return JsonResponse({'status': 'error', 'message': 'La conversación no posee un producto asociado.'}, status=400)

    nuevo_comentario = Comentario.objects.create(
        producto=producto,
        usuario=request.user,
        pedido=pedido,
        comentario=comentario_texto,
        valoracion=valoracion_num
    )

    return JsonResponse({
        'status': 'success',
        'message': '¡Gracias por tu valoración!',
        'comentario_id': nuevo_comentario.id
    })