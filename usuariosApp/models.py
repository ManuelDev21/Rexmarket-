import logging
import uuid
from datetime import timedelta
from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.timezone import now
from django.db.models.signals import post_save
from django.dispatch import receiver
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


class Sede(models.Model):
    REGIONES_CHILE = [
        ('XV', 'Arica y Parinacota'),
        ('I', 'Tarapacá'),
        ('II', 'Antofagasta'),
        ('III', 'Atacama'),
        ('IV', 'Coquimbo'),
        ('V', 'Valparaíso'),
        ('RM', 'Metropolitana de Santiago'),
        ('VI', "Libertador General Bernardo O'Higgins"),
        ('VII', 'Maule'),
        ('XVI', 'Ñuble'),
        ('VIII', 'Biobío'),
        ('IX', 'La Araucanía'),
        ('XIV', 'Los Ríos'),
        ('X', 'Los Lagos'),
        ('XI', 'Aysén del General Carlos Ibáñez del Campo'),
        ('XII', 'Magallanes y de la Antártica Chilena'),
    ]

    nombre = models.CharField(max_length=100)  # Ej: INACAP Talca, INACAP Santiago Centro
    region = models.CharField(max_length=5, choices=REGIONES_CHILE)
    comuna = models.CharField(max_length=100)
    direccion = models.CharField(max_length=255, blank=True, null=True)
    latitud = models.FloatField()
    longitud = models.FloatField()
    radio_permitido_metros = models.PositiveIntegerField(default=250)
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ['region', 'nombre']

    def __str__(self):
        return f"{self.nombre} ({self.get_region_display()})"


class Producto(models.Model):
    CATEGORIAS = [
        ('Comida y Bebidas', 'Comida y Bebidas'),
        ('POSTRES', 'Postres y Dulces'),
        ('TECNOLOGIA', 'Tecnología y accesorios'),
        ('ROPA', 'Ropa y Moda'),
        ('ACCESORIOS', 'Accesorios'),
        ('LIBROS_PAPELERIA', 'Libros y Papelería'),
        ('MANUALIDADES', 'Manualidades'),
        ('SERVICIOS', 'Servicios'),
        ('HOGAR', 'Hogar y Decoración'),
        ('ENTRETENIMIENTO', 'Juegos y Entretenimiento'),
        ('DEPORTES', 'Deportes y Fitness'),
        ('OTROS', 'Otros'),
    ]

    nombre = models.CharField(max_length=100)
    descripcion = models.TextField()
    precio = models.CharField(max_length=20)
    imagen = models.ImageField(upload_to='productos/')
    vendedor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='productos')
    sede = models.ForeignKey(Sede, on_delete=models.SET_NULL, null=True, blank=True, related_name='productos')
    categoria = models.CharField(max_length=30, choices=CATEGORIAS, default='OTROS')
    destacado_hasta = models.DateTimeField(null=True, blank=True)
    tipo_destacado = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)
    cancelado = models.BooleanField(default=False)

    # Estados de disponibilidad, reserva y ventas
    acepta_envio = models.BooleanField(default=False)
    reservado = models.BooleanField(default=False)
    vendido = models.BooleanField(default=False)
    fecha_vendido = models.DateTimeField(null=True, blank=True)
    fecha_creacion = models.DateTimeField(default=now)
    comision_pagada = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=['activo', 'comision_pagada', 'vendido', 'reservado']),
            models.Index(fields=['sede', 'activo']),
        ]

    def __str__(self):
        return self.nombre

    def reservar(self):
        """Bloquea el producto como reservado mientras existe una entrega acordada."""
        if not self.vendido and not self.reservado:
            self.reservado = True
            self.save(update_fields=['reservado'])

    def liberar_reserva(self):
        """Devuelve el producto a estado disponible si el acuerdo se cancela o anula."""
        if not self.vendido and self.reservado:
            self.reservado = False
            self.save(update_fields=['reservado'])

    def marcar_como_vendido(self):
        """Marca el producto como vendido, libera la reserva y registra el momento para mostrarlo por 7 días."""
        self.vendido = True
        self.reservado = False
        self.fecha_vendido = now()
        self.save(update_fields=['vendido', 'reservado', 'fecha_vendido'])

    @property
    def es_vendido_visible(self):
        """Retorna True si fue vendido hace menos de 7 días."""
        if not self.vendido or not self.fecha_vendido:
            return False
        return (now() - self.fecha_vendido) <= timedelta(days=7)

    def es_destacado(self):
        return bool(self.destacado_hasta and self.destacado_hasta > now())

    def es_favorito(self, usuario):
        if usuario.is_authenticated:
            return self.favoritos.filter(usuario=usuario).exists()
        return False

    @property
    def precio_numerico(self):
        """Convierte el string de precio en un entero para comparaciones y ordenamiento seguro."""
        try:
            limpio = ''.join(c for c in str(self.precio) if c.isdigit())
            return int(limpio) if limpio else 0
        except (ValueError, TypeError):
            return 0


class VerificationCode(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='verification_code')
    code = models.CharField(max_length=12)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=now)
    expires_at = models.DateTimeField(null=True, blank=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    resend_count = models.PositiveSmallIntegerField(default=0)
    last_sent_at = models.DateTimeField(default=now)

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = now() + timedelta(minutes=15)
        super().save(*args, **kwargs)

    @property
    def is_expired(self):
        return bool(self.expires_at and now() > self.expires_at)

    def seconds_until_resend(self, cooldown_seconds=30):
        if not self.last_sent_at:
            return 0
        elapsed = (now() - self.last_sent_at).total_seconds()
        return max(0, int(cooldown_seconds - elapsed))

    def __str__(self):
        return f"{self.user.username} - {'Verificado' if self.is_verified else 'No verificado'}"


class Favorito(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='favoritos')
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='favoritos')
    fecha_agregado = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('usuario', 'producto')

    def __str__(self):
        return f"{self.usuario.username} - {self.producto.nombre}"


class Conversation(models.Model):
    producto = models.ForeignKey('Producto', on_delete=models.SET_NULL, null=True, blank=True, related_name='conversaciones')
    participants = models.ManyToManyField(User, related_name='conversations')
    created_at = models.DateTimeField(auto_now_add=True)
    hidden_by = models.ManyToManyField(User, related_name='hidden_conversations', blank=True)

    def __str__(self):
        nombres = ", ".join(u.first_name or u.username for u in self.participants.all()[:2])
        prod_nombre = self.producto.nombre if self.producto else "Sin producto"
        return f"Conversación [{self.id}] ({prod_nombre}) entre {nombres}"


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    bloqueado_moderacion = models.BooleanField(
        default=False,
        help_text="True si el mensaje disparó Alerta Roja y fue bloqueado antes de entregarse."
    )

    class Meta:
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['conversation', 'timestamp']),
            models.Index(fields=['conversation', 'is_read']),
        ]

    def __str__(self):
        return f"De {self.sender.username} a las {self.timestamp:%H:%M}: {self.content[:30]}"


class Reporte(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='reportes')
    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reportes')
    motivo = models.TextField()
    fecha_reporte = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        nombre_usuario = self.usuario.first_name or self.usuario.username
        return f"Reporte de {nombre_usuario} sobre {self.producto.nombre}"


class MessageReport(models.Model):
    TIPO_CHOICES = [
        ('ROJA', 'Alerta Roja'),
        ('AMARILLA', 'Alerta Amarilla'),
    ]

    ORIGEN_CHOICES = [
        ('FILTRO_LEXICO', 'Filtro Léxico / Fonético'),
        ('IA_ROBERTUITO', 'IA Local RoBERTuito'),
        ('REPORTE_USUARIO', 'Reporte Manual de Usuario'),
    ]

    message = models.ForeignKey(Message, related_name='reports', on_delete=models.CASCADE)
    reporter = models.ForeignKey(User, related_name='submitted_reports', on_delete=models.CASCADE)
    reason = models.TextField()
    tipo_alerta = models.CharField(max_length=10, choices=TIPO_CHOICES, default='AMARILLA')
    origen_deteccion = models.CharField(max_length=20, choices=ORIGEN_CHOICES, default='REPORTE_USUARIO')
    termino_detectado = models.CharField(max_length=100, blank=True, null=True)
    confianza_ia = models.FloatField(blank=True, null=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['resolved', '-fecha_creacion']),
            models.Index(fields=['tipo_alerta', 'resolved']),
        ]

    def __str__(self):
        return f"Reporte [{self.tipo_alerta} - {self.origen_deteccion}] de {self.reporter.username}"


@receiver(post_save, sender=MessageReport)
def notificar_websocket_reporte(sender, instance, created, **kwargs):
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    try:
        if created:
            async_to_sync(channel_layer.group_send)(
                "admin_reportes",
                {
                    "type": "nuevo_reporte",
                    "categoria": "MENSAJE",
                    "reporte_data": {
                        "id": instance.id,
                        "tipo_alerta": getattr(instance, 'tipo_alerta', 'AMARILLA'),
                        "origen_deteccion": instance.get_origen_deteccion_display(),
                        "reporter_name": f"{instance.reporter.first_name} {instance.reporter.last_name}".strip() or instance.reporter.username,
                        "reporter_username": instance.reporter.username or "No registrado",
                        "reported_name": f"{instance.message.sender.first_name} {instance.message.sender.last_name}".strip() or instance.message.sender.username,
                        "reported_username": instance.message.sender.username or "No registrado",
                        "content": instance.message.content,
                        "reason": instance.reason,
                        "fecha_iso": instance.fecha_creacion.strftime('%Y-%m-%d %H:%M:%S'),
                        "fecha_display": instance.fecha_creacion.strftime('%d/%m/%Y %H:%M'),
                        "conversation_id": instance.message.conversation.id
                    }
                }
            )
        elif instance.resolved:
            async_to_sync(channel_layer.group_send)(
                "admin_reportes",
                {
                    "type": "reporte_resuelto",
                    "categoria": "MENSAJE",
                    "reporte_id": instance.id
                }
            )
    except Exception as e:
        logger.warning(f"No se pudo notificar reporte de mensaje por WebSocket: {e}")


class Pedido(models.Model):
    ESTADOS = (
        ('solicitado', 'Solicitado'),
        ('acordado', 'Acordado'),
        ('en_camino', 'En Camino'),
        ('en_sitio', 'En Sitio'),
        ('enviado', 'Enviado por Encomienda'),
        ('completado', 'Completado'),
        ('cancelado_inasistencia', 'Cancelado por Inasistencia'),
        ('rechazado', 'Rechazado'),
        ('expirado', 'Expirado (Sin respuesta)'),
        ('cancelado_admin', 'Anulado por Administración')
    )
    TIPO_ENTREGA = (
        ('presencial', 'Entrega Presencial en Sede'),
        ('envio', 'Envío por Encomienda'),
    )
    usuario_ausente = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pedidos_con_inasistencia'
    )
    resolucion_admin = models.TextField(blank=True, null=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    conversacion = models.ForeignKey('Conversation', on_delete=models.CASCADE, related_name='pedidos')
    comprador = models.ForeignKey(User, on_delete=models.CASCADE, related_name='compras_pedidos')
    vendedor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ventas_pedidos')
    tipo_entrega = models.CharField(max_length=20, choices=TIPO_ENTREGA, default='presencial')

    # Campos para entrega presencial en sede (Google Maps + Código QR dinámico)
    lugar_nombre = models.CharField(max_length=255, blank=True, null=True)
    latitud = models.CharField(max_length=50, blank=True, null=True)
    longitud = models.CharField(max_length=50, blank=True, null=True)
    pin_seguridad = models.CharField(max_length=4, blank=True, null=True)
    token_qr = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    # Campos para envío interregional
    empresa_envio = models.CharField(max_length=50, blank=True, null=True)
    codigo_seguimiento = models.CharField(max_length=100, blank=True, null=True)
    direccion_envio = models.TextField(blank=True, null=True)

    estado = models.CharField(max_length=30, choices=ESTADOS, default='solicitado')

    # Campos de seguridad y temporizador
    propuesto_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='pedidos_propuestos')
    fecha_llegada_sitio = models.DateTimeField(null=True, blank=True)
    quien_llego_primero = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='llegadas_primero')

    class Meta:
        indexes = [
            models.Index(fields=['estado', 'fecha_creacion']),
        ]


class Comentario(models.Model):
    producto = models.ForeignKey('Producto', on_delete=models.CASCADE, related_name='comentarios')
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    pedido = models.OneToOneField(Pedido, on_delete=models.SET_NULL, null=True, blank=True, related_name='comentario_transaccion')
    comentario = models.TextField()
    valoracion = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    fecha_creacion = models.DateTimeField(auto_now_add=True)


class ReporteComentario(models.Model):
    comentario = models.ForeignKey(Comentario, on_delete=models.CASCADE, related_name='reportes')
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    motivo = models.TextField()
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Reporte de {self.usuario.username} sobre comentario en {self.comentario.producto.nombre}'


class Perfil(models.Model):
    usuario = models.OneToOneField(User, on_delete=models.CASCADE, related_name='perfil')
    sede = models.ForeignKey(Sede, on_delete=models.SET_NULL, null=True, blank=True, related_name='estudiantes')
    comuna = models.CharField(max_length=100, blank=True, null=True)
    advertencias = models.PositiveIntegerField(default=0)
    esta_baneado = models.BooleanField(default=False)
    inasistencias = models.PositiveIntegerField(default=0)

    def __str__(self):
        sede_nombre = self.sede.nombre if self.sede else "Sin sede"
        return f"Perfil de {self.usuario.username} - {sede_nombre} ({self.advertencias}/3 advertencias)"

    def agregar_advertencia(self):
        """Aumenta las advertencias y banea al llegar a 3."""
        self.advertencias += 1
        if self.advertencias >= 3:
            self.esta_baneado = True
            self.usuario.is_active = False
            self.usuario.save(update_fields=['is_active'])
        self.save(update_fields=['advertencias', 'esta_baneado'])

    def suspender_por_moderacion(self, motivo="Incumplimiento grave de normas en el chat."):
        """Suspende la cuenta sincronizando User.is_active, Perfil.esta_baneado y la bitácora."""
        self.esta_baneado = True
        self.save(update_fields=['esta_baneado'])

        if self.usuario.is_active:
            self.usuario.is_active = False
            self.usuario.save(update_fields=['is_active'])

        AuditoriaAdmin.objects.create(
            administrador=None,
            usuario_afectado=self.usuario,
            accion='SUSPENSION_AUTOMATICA_IA',
            detalle=f"Suspensión automática de {self.usuario.username} por Alerta Roja en chat. {motivo}"
        )


@receiver(post_save, sender=User)
def crear_perfil_usuario(sender, instance, created, **kwargs):
    if created:
        Perfil.objects.get_or_create(usuario=instance)


class PagoComision(models.Model):
    ESTADOS_PAGO = (
        ('pendiente', 'Pendiente'),
        ('pagado', 'Pagado'),
        ('rechazado', 'Rechazado'),
    )
    producto = models.OneToOneField(Producto, on_delete=models.CASCADE, related_name='pago_comision')
    vendedor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pagos_comisiones')
    monto_producto = models.PositiveIntegerField()
    monto_comision = models.PositiveIntegerField()  # 2% del precio
    buy_order = models.CharField(max_length=50, unique=True)
    token_ws = models.CharField(max_length=120, blank=True, null=True, db_index=True)
    estado = models.CharField(max_length=20, choices=ESTADOS_PAGO, default='pendiente')
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_pago = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Comisión 2% (${self.monto_comision}) - {self.producto.nombre} [{self.estado}]"


class PagoDestacado(models.Model):
    ESTADOS_PAGO = (
        ('pendiente', 'Pendiente'),
        ('pagado', 'Pagado'),
        ('rechazado', 'Rechazado'),
    )
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name='pagos_destacado')
    vendedor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pagos_destacado')
    dias = models.PositiveIntegerField(default=1)
    monto = models.PositiveIntegerField()
    buy_order = models.CharField(max_length=64, unique=True)
    token_ws = models.CharField(max_length=120, blank=True, null=True, db_index=True)
    estado = models.CharField(max_length=20, choices=ESTADOS_PAGO, default='pendiente')
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_pago = models.DateTimeField(null=True, blank=True)

    @property
    def monto_comision(self):
        """Alias para reutilizar checkout_transbank.html y transbank_service.py sin cambios."""
        return self.monto

    def __str__(self):
        return f"Destacado {self.dias}d (${self.monto}) - {self.producto.nombre} [{self.estado}]"


class AuditoriaAdmin(models.Model):
    ACCIONES = [
        ('ADVERTENCIA_MANUAL', 'Advertencia Manual Sumada'),
        ('PENALIZACION_INASISTENCIA', 'Advertencia Automática por Inasistencia'),
        ('SUSPENSION_AUTOMATICA_IA', 'Suspensión Automática por Moderación (IA/Filtro)'),
        ('SUSPENSION_USUARIO', 'Suspensión / Baneo de Cuenta'),
        ('REACTIVACION_USUARIO', 'Reactivación / Desbaneo de Cuenta'),
        ('ELIMINACION_USUARIO', 'Eliminación Definitiva de Cuenta'),
        ('INTERVENCION_PEDIDO', 'Anulación / Mediación de Pedido'),
        ('REPORTE_MSG_RESUELTO', 'Reporte de Mensaje Resuelto'),
        ('REPORTE_MSG_ELIMINADO', 'Reporte de Mensaje Eliminado'),
        ('REPORTE_PROD_DESCARTADO', 'Reporte de Producto Descartado'),
        ('REPORTE_COM_DESCARTADO', 'Reporte de Comentario Descartado'),
    ]

    administrador = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acciones_auditoria_realizadas',
        help_text="Null indica acción ejecutada automáticamente por el sistema."
    )
    usuario_afectado = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='acciones_auditoria_recibidas'
    )
    accion = models.CharField(max_length=40, choices=ACCIONES)
    detalle = models.TextField()
    ip_origen = models.GenericIPAddressField(null=True, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-fecha']
        verbose_name = 'Registro de Auditoría'
        verbose_name_plural = 'Bitácora de Auditoría'
        indexes = [
            models.Index(fields=['accion', '-fecha']),
        ]

    def __str__(self):
        actor = self.administrador.username if self.administrador else "SISTEMA"
        return f"[{self.fecha:%d/%m/%Y %H:%M}] {actor} - {self.get_accion_display()}"


class ApelacionSuspension(models.Model):
    ESTADOS = [
        ('pendiente', 'Pendiente de Revisión'),
        ('aprobada', 'Aprobada (Cuenta Reactivada)'),
        ('rechazada', 'Rechazada'),
    ]

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, related_name='apelaciones')
    motivo_estudiante = models.TextField()
    estado = models.CharField(max_length=15, choices=ESTADOS, default='pendiente')
    respuesta_admin = models.TextField(blank=True, null=True)
    revisado_por = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='apelaciones_revisadas'
    )
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_resolucion = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-fecha_creacion']
        verbose_name = 'Apelación de Suspensión'
        verbose_name_plural = 'Apelaciones de Suspensión'

    def __str__(self):
        return f"Apelación #{self.id} - {self.usuario.username} [{self.get_estado_display()}]"