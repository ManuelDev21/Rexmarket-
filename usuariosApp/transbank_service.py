import uuid
from django.conf import settings
from django.urls import reverse
from transbank.common.integration_api_keys import IntegrationApiKeys
from transbank.common.integration_commerce_codes import IntegrationCommerceCodes
from transbank.common.integration_type import IntegrationType
from transbank.common.options import WebpayOptions
from transbank.webpay.webpay_plus.transaction import Transaction


def es_modo_simulacion():
    return getattr(settings, 'TRANSBANK_MODO_SIMULACION', True)


def _obtener_transaccion_tbk():
    commerce_code = getattr(settings, 'TRANSBANK_COMMERCE_CODE', IntegrationCommerceCodes.WEBPAY_PLUS)
    api_key = getattr(settings, 'TRANSBANK_API_KEY', IntegrationApiKeys.WEBPAY)
    env_type = IntegrationType.LIVE if getattr(settings, 'TRANSBANK_PRODUCTION', False) else IntegrationType.TEST
    return Transaction(WebpayOptions(commerce_code, api_key, env_type))


def iniciar_transaccion_webpay(request, pago_obj, return_view_name='retorno_pago_comision'):
    """
    Inicia una transacción en Webpay Plus (Integración Real o Simulada)
    tanto para comisiones de publicación como para planes de destacado VIP.
    Retorna (url_redireccion, token_ws).
    """
    return_url = request.build_absolute_uri(reverse(return_view_name))

    if es_modo_simulacion():
        token_simulado = f"SIM-{uuid.uuid4().hex[:24]}"
        url_simulador = reverse('simulador_webpay', kwargs={'token_ws': token_simulado})
        return url_simulador, token_simulado

    monto = getattr(pago_obj, 'monto_comision', None)
    if monto is None:
        monto = getattr(pago_obj, 'monto', 0)

    tx = _obtener_transaccion_tbk()
    response = tx.create(
        buy_order=pago_obj.buy_order,
        session_id=f"USR-{pago_obj.vendedor.id}",
        amount=int(monto),
        return_url=return_url
    )
    return response['url'], response['token']


def confirmar_transaccion_webpay(token_ws, resultado_simulado='aprobado'):
    """
    Confirma el pago cuando Webpay retorna al comercio.
    Retorna dict con {'aprobado': bool, 'detalle': str}.
    """
    if es_modo_simulacion():
        if resultado_simulado == 'aprobado':
            return {'aprobado': True, 'detalle': 'Pago aprobado en simulador Webpay Plus'}
        return {'aprobado': False, 'detalle': 'Transacción rechazada o anulada por el usuario'}

    try:
        tx = _obtener_transaccion_tbk()
        response = tx.commit(token=token_ws)
        aprobado = response.get('status') == 'AUTHORIZED' and response.get('response_code') == 0
        return {
            'aprobado': aprobado,
            'detalle': f"Estado Webpay: {response.get('status')} (Código {response.get('response_code')})"
        }
    except Exception as e:
        return {
            'aprobado': False,
            'detalle': f"Error al validar transacción en Transbank: {str(e)}"
        }