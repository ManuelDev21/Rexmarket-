from django.urls import path, include
from django.views.generic import TemplateView
from usuariosApp import views
from django.contrib.auth import views as auth_views
from usuariosApp.forms import InstitutionalPasswordResetForm

urlpatterns = [
   # Autenticación, Recuperación de Clave y Apelaciones
    path('accounts/login/', views.login_register_view, name='login'),
    path(
        'accounts/password_reset/',
        auth_views.PasswordResetView.as_view(
            form_class=InstitutionalPasswordResetForm,
            template_name='registration/password_reset_form.html',
            email_template_name='registration/password_reset_email.txt',
            subject_template_name='registration/password_reset_subject.txt',
            success_url='/accounts/password_reset/done/'
        ),
        name='password_reset'
    ),
    path(
        'accounts/password_reset/done/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='registration/password_reset_done.html'
        ),
        name='password_reset_done'
    ),
    path(
        'accounts/reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='registration/password_reset_confirm.html',
            success_url='/accounts/reset/done/'
        ),
        name='password_reset_confirm'
    ),
    path(
        'accounts/reset/done/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='registration/password_reset_complete.html'
        ),
        name='password_reset_complete'
    ),
    path('accounts/', include('django.contrib.auth.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
    path('apelar-suspension/', views.solicitar_apelacion_view, name='solicitar_apelacion'),
    path('administracion/apelaciones/<int:apelacion_id>/resolver/', views.resolver_apelacion_admin, name='resolver_apelacion_admin'),
    path('signup/', views.SignUpView.as_view(), name='signup'),
    path('verify-email/', views.VerifyEmailView.as_view(), name='verify_email'),
    path('verify-email/resend/', views.resend_verification_code, name='resend_verification_code'),
    path('profile/', views.ProfileView.as_view(), name='profile'),
    path('delete-account/', views.DeleteAccountView.as_view(), name='delete_account'),

    # Productos y Comisiones Transbank
    path('', views.lista_productos, name='home'),
    path('publicar/', views.publicar_producto, name='publicar_producto'),
    path('producto/<int:producto_id>/pagar-comision/', views.reintentar_pago_comision, name='reintentar_pago_comision'),
    path('pago-comision/simulador/<str:token_ws>/', views.simulador_webpay, name='simulador_webpay'),
    path('pago-comision/retorno/', views.retorno_pago_comision, name='retorno_pago_comision'),
    path('editar/<int:pk>/', views.editar_producto, name='editar_producto'),
    path('eliminar/<int:pk>/', views.eliminar_producto, name='eliminar_producto'),
    path('producto/<int:producto_id>/', views.detalle_producto, name='detalle_producto'),
    path('producto/<int:producto_id>/marcar-vendido/', views.marcar_producto_vendido, name='marcar_producto_vendido'),
    path('destacar/<int:producto_id>/', views.destacar_producto, name='destacar_producto'),
    path('destacar/retorno/', views.retorno_pago_destacado, name='retorno_pago_destacado'),
    path('toggle-favorito/', views.toggle_favorito, name='toggle_favorito'),

    # Comentarios
    path('comentarios/eliminar/<int:comentario_id>/', views.eliminar_comentario, name='eliminar_comentario'),
    path('comentario/reportar/<int:comentario_id>/', views.reportar_comentario, name='reportar_comentario'),

    # Mensajería y Chat en Tiempo Real
    path('conversations/', views.conversations_list, name='conversations_list'),
    path('inbox/', views.inbox_redirect, name='inbox'),
    path('conversations/<int:conversation_id>/', views.conversation_detail, name='conversation_detail'),
    path('start-conversation/<int:recipient_id>/', views.start_conversation, name='start_conversation'),
    path('conversations/<int:conversation_id>/messages/send/', views.send_message, name='send_message'),
    path('conversations/<int:conversation_id>/messages/', views.get_conversation_messages, name='get_conversation_messages'),
    path('conversaciones/<int:conversation_id>/ocultar/', views.ocultar_conversacion, name='ocultar_conversacion'),
    path('mensajes/reportar/<int:message_id>/', views.report_message, name='report_message'),
    path('api/mensajes-no-leidos/', views.contar_mensajes_no_leidos, name='contar_mensajes_no_leidos'),

    # Administración, Analítica y Moderación (Staff/Superusuario)
    path('administracion/estadisticas/', views.estadisticas_admin, name='estadisticas_admin'),
    path('reportar/<int:producto_id>/', views.reportar_producto, name='reportar_producto'),
    path('reportes/', views.lista_reportes, name='lista_reportes'),
    path('reporte/eliminar/<int:reporte_id>/', views.eliminar_reporte, name='eliminar_reporte'),
    path('reportes/comentarios/', views.listar_reportes_comentarios, name='reportes_comentarios'),
    path('reporte/comentario/eliminar/<int:reporte_id>/', views.eliminar_reporte_comentario, name='eliminar_reporte_comentario'),
    path('reportes/mensajes/', views.lista_reportes_mensajes, name='lista_reportes_mensajes'),
    path('reportes/mensajes/eliminar/<int:reporte_id>/', views.eliminar_reporte_mensaje, name='eliminar_reporte_mensaje'),
    path('reportes/mensajes/resolver/<int:reporte_id>/', views.resolver_reporte_mensaje, name='resolver_reporte_mensaje'),
    path('administracion/usuarios/', views.gestion_usuarios, name='gestion_usuarios'),
    path('administracion/usuarios/perfil/<int:user_id>/', views.ver_perfil_admin, name='ver_perfil_admin'),
    path('administracion/usuarios/advertir/<int:user_id>/', views.sumar_advertencia_admin, name='sumar_advertencia_admin'),
    path('administracion/usuarios/estado/<int:user_id>/', views.cambiar_estado_usuario, name='cambiar_estado_usuario'),
    path('administracion/usuarios/eliminar/<int:user_id>/', views.eliminar_usuario_definitivo, name='eliminar_usuario_definitivo'),

    # Flujo de Tracking de Entregas Presenciales (QR) y Envíos
    path('conversations/<int:conversation_id>/tracking/proponer/', views.crear_punto_entrega, name='tracking_proponer'),
    path('conversations/<int:conversation_id>/tracking/responder/', views.responder_solicitud_punto, name='tracking_responder'),
    path('conversations/<int:conversation_id>/tracking/actualizar-estado/', views.actualizar_estado_tracking, name='tracking_actualizar_estado'),
    path('conversations/<int:conversation_id>/tracking/validar-qr/', views.validar_qr_entrega, name='tracking_validar_qr'),
    path('conversations/<int:conversation_id>/tracking/verificar-expiracion/', views.verificar_expiracion_chat, name='tracking_verificar_expiracion'),
    path('conversations/<int:conversation_id>/tracking/despachar/', views.despachar_pedido_envio, name='tracking_despachar'),
    path('conversations/<int:conversation_id>/tracking/confirmar-recepcion/', views.confirmar_recepcion_envio, name='tracking_confirmar_recepcion'),
    path('conversations/<int:conversation_id>/tracking/reportar-inasistencia/', views.reportar_inasistencia, name='tracking_reportar_inasistencia'),
    path('conversations/<int:conversation_id>/valorar/', views.crear_comentario_valoracion, name='crear_comentario_valoracion'),

    # Monitor Central de Pedidos, Disputas y Bitácora de Auditoría
    path('administracion/pedidos/', views.monitor_pedidos_admin, name='monitor_pedidos_admin'),
    path('administracion/pedidos/<int:pedido_id>/intervenir/', views.intervenir_pedido_admin, name='intervenir_pedido_admin'),
    path('administracion/auditoria/', views.bitacora_auditoria_admin, name='bitacora_auditoria_admin'),

    path(
        'serviceworker.js',
        TemplateView.as_view(
            template_name='base/serviceworker.js',
            content_type='application/javascript'
        ),
        name='serviceworker'
    ),
]