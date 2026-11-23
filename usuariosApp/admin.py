from django.contrib import admin
from .models import (
    Sede, Perfil, Producto, Comentario, Favorito,
    Conversation, Message, Pedido, PagoComision,
    Reporte, ReporteComentario, MessageReport,
    VerificationCode
)


@admin.register(Sede)
class SedeAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'region', 'comuna', 'latitud', 'longitud', 'radio_permitido_metros', 'activa')
    list_filter = ('region', 'activa')
    search_fields = ('nombre', 'comuna', 'direccion')
    list_editable = ('radio_permitido_metros', 'activa')


@admin.register(Perfil)
class PerfilAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'sede', 'comuna', 'advertencias', 'esta_baneado')
    list_filter = ('sede', 'esta_baneado')
    search_fields = ('usuario__username', 'usuario__email', 'comuna')


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'sede', 'categoria', 'precio', 'vendedor', 'activo', 'vendido', 'comision_pagada')
    search_fields = ('nombre', 'descripcion', 'vendedor__username')
    list_filter = ('activo', 'vendido', 'comision_pagada', 'sede', 'categoria')
    list_editable = ('categoria', 'activo')


@admin.register(PagoComision)
class PagoComisionAdmin(admin.ModelAdmin):
    list_display = ('buy_order', 'producto', 'vendedor', 'monto_producto', 'monto_comision', 'estado', 'fecha_creacion')
    list_filter = ('estado', 'fecha_creacion')
    search_fields = ('buy_order', 'producto__nombre', 'vendedor__username')


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = ('id', 'comprador', 'vendedor', 'tipo_entrega', 'lugar_nombre', 'estado', 'fecha_creacion')
    list_filter = ('tipo_entrega', 'estado', 'fecha_creacion')
    search_fields = ('comprador__username', 'vendedor__username', 'lugar_nombre', 'codigo_seguimiento')


@admin.register(Comentario)
class ComentarioAdmin(admin.ModelAdmin):
    list_display = ('producto', 'usuario', 'valoracion', 'fecha_creacion')
    search_fields = ('producto__nombre', 'usuario__username', 'comentario')
    list_filter = ('valoracion', 'fecha_creacion')


@admin.register(Favorito)
class FavoritoAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'producto', 'fecha_agregado')
    search_fields = ('usuario__username', 'producto__nombre')


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('id', 'producto', 'created_at')
    filter_horizontal = ('participants', 'hidden_by')


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'conversation', 'sender', 'timestamp', 'is_read')
    search_fields = ('content', 'sender__username')
    list_filter = ('is_read', 'timestamp')


@admin.register(Reporte)
class ReporteAdmin(admin.ModelAdmin):
    list_display = ('id', 'producto', 'usuario', 'fecha_reporte')
    search_fields = ('producto__nombre', 'usuario__username', 'motivo')
    list_filter = ('fecha_reporte',)


@admin.register(ReporteComentario)
class ReporteComentarioAdmin(admin.ModelAdmin):
    list_display = ('id', 'comentario', 'usuario', 'fecha_creacion')
    search_fields = ('usuario__username', 'motivo')
    list_filter = ('fecha_creacion',)


@admin.register(MessageReport)
class MessageReportAdmin(admin.ModelAdmin):
    list_display = ('id', 'tipo_alerta', 'reporter', 'resolved', 'fecha_creacion')
    list_filter = ('tipo_alerta', 'resolved', 'fecha_creacion')
    search_fields = ('reason', 'reporter__username')


@admin.register(VerificationCode)
class VerificationCodeAdmin(admin.ModelAdmin):
    list_display = ('user', 'code', 'is_verified')
    list_filter = ('is_verified',)
    search_fields = ('user__username', 'code')


class PerfilInline(admin.StackedInline):
    model = Perfil
    can_delete = False
    verbose_name_plural = 'Perfil y Sede INACAP'
    min_num = 1


from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

class CustomUserAdmin(BaseUserAdmin):
    inlines = (PerfilInline,)



