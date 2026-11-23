import json
import logging
from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth.models import User
from webpush import send_user_notification
from .models import Conversation, Message, MessageReport, Perfil
from .moderation import analyze_message

logger = logging.getLogger(__name__)
evaluate_ai = sync_to_async(analyze_message, thread_sensitive=False)


class ChatConsumer(AsyncWebsocketConsumer):

    # --------------------------------------------------------
    # CONEXIÓN Y DESCONEXIÓN CON CONTROL DE ACCESO
    # --------------------------------------------------------
    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close()
            return

        self.conversation_id = self.scope['url_route']['kwargs']['conversation_id']
        self.es_participante = await self.is_user_participant(self.conversation_id, user.id)
        self.es_auditor = not self.es_participante and (user.is_staff or user.is_superuser)

        if not self.es_participante and not self.es_auditor:
            await self.close()
            return

        self.room_group_name = f'chat_{self.conversation_id}'

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

        if self.es_participante:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'user_status',
                    'status': 'En línea',
                    'user_id': user.id,
                    'is_new': True
                }
            )

    async def disconnect(self, close_code):
        user = self.scope.get('user')
        if user and user.is_authenticated and getattr(self, 'es_participante', False) and hasattr(self, 'room_group_name'):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'user_status',
                    'status': 'Desconectado',
                    'user_id': user.id,
                    'is_new': False
                }
            )
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    # --------------------------------------------------------
    # MÉTODOS AUXILIARES EN BASE DE DATOS
    # --------------------------------------------------------
    @database_sync_to_async
    def is_user_participant(self, conversation_id, user_id):
        try:
            conv = Conversation.objects.get(id=conversation_id)
            return conv.participants.filter(id=user_id).exists()
        except Exception:
            return False

    @database_sync_to_async
    def guardar_mensaje(self, user_id, texto):
        try:
            usuario = User.objects.get(id=user_id)
            return Message.objects.create(
                conversation_id=self.conversation_id,
                sender=usuario,
                content=texto
            )
        except Exception as e:
            logger.error(f"Error guardando mensaje por WebSocket: {e}")
            return None

    @database_sync_to_async
    def reportar_silenciosamente(self, user_id, mensaje_texto, analysis, tipo='AMARILLA', bloqueado=False):
        try:
            usuario_infractor = User.objects.get(id=user_id)
            mensaje_obj = Message.objects.create(
                conversation_id=self.conversation_id,
                sender=usuario_infractor,
                content=mensaje_texto,
                bloqueado_moderacion=bloqueado
            )
            denunciante = User.objects.filter(is_superuser=True).first() or usuario_infractor

            MessageReport.objects.create(
                message=mensaje_obj,
                reporter=denunciante,
                reason=analysis.get('reason', f'Alerta {tipo}'),
                tipo_alerta=tipo,
                origen_deteccion=analysis.get('origen_deteccion', 'IA_ROBERTUITO'),
                termino_detectado=analysis.get('termino_detectado'),
                confianza_ia=analysis.get('confianza_ia'),
                resolved=False
            )
        except Exception as e:
            logger.error(f"Error en reporte silencioso: {e}")

    @database_sync_to_async
    def banear_usuario(self, user_id, razon="Alerta Roja en chat"):
        usuario = User.objects.filter(id=user_id).first()
        if usuario:
            perfil, _ = Perfil.objects.get_or_create(usuario=usuario)
            perfil.suspender_por_moderacion(motivo=razon)

    @database_sync_to_async
    def update_unread_messages(self):
        user = self.scope.get('user')
        if user and user.is_authenticated:
            Message.objects.filter(
                conversation_id=self.conversation_id
            ).exclude(
                sender_id=user.id
            ).filter(is_read=False, bloqueado_moderacion=False).update(is_read=True)

    @database_sync_to_async
    def enviar_notificacion_push(self, emisor, texto_mensaje):
        try:
            conv = Conversation.objects.get(id=self.conversation_id)
            receptor = conv.participants.exclude(id=emisor.id).first()

            if receptor:
                nombre_emisor = emisor.first_name if emisor.first_name else emisor.username
                payload = {
                    'head': f'💬 {nombre_emisor} en RexMarket',
                    'body': texto_mensaje,
                    'url': f'/conversations/{self.conversation_id}/'
                }
                send_user_notification(user=receptor, payload=payload, ttl=1000)
        except Exception as e:
            logger.error(f"Error enviando notificación Push por WebSocket: {e}")

    # --------------------------------------------------------
    # RECEPCIÓN Y TRANSMISIÓN DE MENSAJES
    # --------------------------------------------------------
    async def receive(self, text_data):
        if getattr(self, 'es_auditor', False):
            return
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            return

        try:
            text_data_json = json.loads(text_data)
        except Exception:
            return

        action = text_data_json.get('action')
        message = text_data_json.get('message')

        if action == 'actualizar_chat' or message == 'actualizar_chat':
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_update',
                    'sender_id': user.id
                }
            )
            return

        if action == 'mark_as_read':
            await self.update_unread_messages()
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'messages_read',
                    'user_id': user.id
                }
            )
            return
        elif action == 'reply_status':
            await self.channel_layer.group_send(
                self.room_group_name,
                {'type': 'user_status', 'status': 'En línea', 'user_id': user.id, 'is_new': False}
            )
            return
        elif action == 'ping':
            await self.channel_layer.group_send(
                self.room_group_name,
                {'type': 'user_status', 'status': 'ping', 'user_id': user.id, 'is_new': False}
            )
            return

        if message:
            analysis = await evaluate_ai(message)
            if analysis.get('reason') == 'Evento técnico ignorado':
                return

            is_red = analysis.get('is_red', False)
            is_yellow = analysis.get('is_yellow', False)

            if is_red:
                await self.reportar_silenciosamente(
                    user.id,
                    message,
                    analysis,
                    tipo='ROJA',
                    bloqueado=True
                )
                await self.banear_usuario(user.id, analysis.get('reason', 'Alerta Roja'))
                await self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': 'Tu cuenta ha sido suspendida por incumplir las normas de la comunidad.'
                }))
                await self.close()
                return

            if is_yellow:
                await self.reportar_silenciosamente(
                    user.id,
                    message,
                    analysis,
                    tipo='AMARILLA',
                    bloqueado=False
                )

            await self.guardar_mensaje(user.id, message)
            await self.enviar_notificacion_push(user, message)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message': message,
                    'is_yellow': is_yellow,
                    'sender_id': user.id
                }
            )

    # --------------------------------------------------------
    # ENVÍO DE EVENTOS AL NAVEGADOR
    # --------------------------------------------------------
    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'message': event['message'],
            'is_yellow': event.get('is_yellow', False),
            'sender_id': event.get('sender_id')
        }))

    async def chat_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_update',
            'sender_id': event.get('sender_id')
        }))

    async def user_status(self, event):
        await self.send(text_data=json.dumps({
            'type': 'status',
            'status': event['status'],
            'user_id': event['user_id'],
            'is_new': event.get('is_new', False)
        }))

    async def messages_read(self, event):
        await self.send(text_data=json.dumps({
            'type': 'messages_read',
            'user_id': event['user_id']
        }))


class NotificationConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if user and user.is_authenticated:
            self.room_group_name = f"notificaciones_{user.id}"
            await self.channel_layer.group_add(
                self.room_group_name,
                self.channel_name
            )
            await self.accept()
        else:
            await self.close()

    async def disconnect(self, close_code):
        user = self.scope.get('user')
        if user and user.is_authenticated and hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def send_notification(self, event):
        await self.send(text_data=json.dumps(event['data']))


class AdminReportesConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]

        if self.user.is_authenticated and self.user.is_staff:
            self.group_name = "admin_reportes"
            await self.channel_layer.group_add(self.group_name, self.channel_name)
            await self.accept()
        else:
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def nuevo_reporte(self, event):
        await self.send(text_data=json.dumps({
            'tipo': 'NUEVO_REPORTE',
            'categoria': event.get('categoria', 'MENSAJE'),
            'reporte': event['reporte_data']
        }))

    async def reporte_resuelto(self, event):
        await self.send(text_data=json.dumps({
            'tipo': 'REPORTE_RESUELTO',
            'categoria': event.get('categoria', 'MENSAJE'),
            'reporte_id': event['reporte_id']
        }))