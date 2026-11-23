import logging
import re
import threading
import unicodedata
from pysentimiento import create_analyzer

logger = logging.getLogger(__name__)

# ==============================================================================
# LISTAS LÉXICAS DE MODERACIÓN (INCLUYE MODISMOS CHILENOS)
# ==============================================================================
LISTA_AMARILLA = [
    'tonto', 'idiota', 'estupido', 'imbecil', 'pendejo', 'weon', 'huevon',
    'pelotudo', 'tarado', 'inutil', 'baboso', 'bobo', 'burro', 'cobarde',
    'cretino', 'desgraciado', 'desubicado', 'gil', 'ignorante', 'menso',
    'necio', 'pajero', 'payaso', 'pesado', 'ridiculo', 'torpe', 'leso',
    'sapo', 'barsa', 'flaite', 'ordinario', 'patetico', 'molesto', 'pesao',
    'tontito', 'weona', 'huevona', 'pelotuda', 'tarada', 'babosa', 'boba',
    'burra', 'cretina', 'mensa', 'payasa', 'ridicula', 'ordinaria',
    'patetica', 'desagradable', 'odioso', 'perkin', 'weta', 'longi',
    'waton', 'watona', 'guaton', 'guatona', 'aweonao', 'aweona', 'pao', 'pava'
]

LISTA_ROJA = [
    'hijueputa', 'hijo de puta', 'hija de puta', 'conchetumadre',
    'conchesumadre', 'concha tu madre', 'ctm', 'ctmre', 'puta', 'puto',
    'culiao', 'culiado', 'culiada', 'culia', 'qliao', 'qlia',
    'maraco', 'maraca', 'maricon', 'maricona', 'bastardo', 'bastarda',
    'maldito', 'maldita', 'acosar', 'acoso', 'acosador', 'acosadora',
    'amenaza', 'amenazar', 'amenazarte', 'matar', 'matarte', 'matarlo',
    'matarla', 'asesinar', 'golpear', 'golpearte', 'agredir', 'agredirte',
    'extorsion', 'extorsionar', 'chantaje', 'chantajear', 'hostigar',
    'hostigamiento', 'estafar', 'estafador', 'estafadora', 'robar',
    'ladron', 'ladrona', 'hacerte daño', 'te voy a matar', 'te voy a pegar',
    'te voy a golpear', 'te voy a hacer daño'
]

EVENTOS_SISTEMA = ['actualizar_chat', 'ping', 'reply_status', 'chat_update', 'refresh']

LEET_CHARS = {
    '@': 'a',
    '4': 'a',
    '3': 'e',
    '1': 'i',
    '!': 'i',
    '0': 'o',
    '$': 's',
    '5': 's',
    '7': 't',
}


def _normalizar_texto(texto: str) -> str:
    """Convierte a minúsculas y elimina tildes manteniendo la letra 'ñ'."""
    texto = texto.lower().strip()
    resultado = []
    for char in unicodedata.normalize('NFD', texto):
        if char == '\u0303':
            resultado.append(char)
        elif unicodedata.category(char) != 'Mn':
            resultado.append(char)
    return unicodedata.normalize('NFC', ''.join(resultado))


def _desofuscar_jerga(texto_normalizado: str) -> str:
    """
    Reconstruye evasiones comunes en chat chileno sin alterar precios ni números puros:
    - 'waton4 klia' -> 'watona culia'
    - 'guet4 qliao' -> 'weta culiao'
    - 'qlo' / 'klo' -> 'culiao'
    - 'm@rako' -> 'maraco'
    - 'c.t.m' / 'p u t a' -> 'ctm' / 'puta'
    """
    t = texto_normalizado

    # 1. Une letras sueltas separadas por puntos, guiones o espacios (ej: 'c.t.m' -> 'ctm')
    t = re.sub(r'\b(?:[a-zñ][\s._-]){2,}[a-zñ]\b', lambda m: re.sub(r'[\s._-]', '', m.group(0)), t)

    # 2. Reemplaza números/símbolos leetspeak SOLO si están pegados a letras (no toca '$4000')
    t = re.sub(
        r'(?<=[a-zñ])[013457@$!]|[013457@$!](?=[a-zñ])',
        lambda m: LEET_CHARS[m.group(0)],
        t
    )

    # 3. Colapsa letras repetidas 3 o más veces (ej: 'kliaaa' -> 'klia', 'ctmmm' -> 'ctm')
    t = re.sub(r'(.)\1{2,}', r'\1', t)

    # 4. Unifica 'gu' inicial con 'w' (ej: 'guatona' -> 'watona', 'gueon' -> 'weon', 'gueta' -> 'weta')
    t = re.sub(r'\bgu([aeio])', r'w\1', t)

    # 5. Normaliza 'k' como 'c' (ej: 'marako' -> 'maraco', 'klia' -> 'clia')
    t = t.replace('k', 'c')

    # 6. Reconstruye contracciones chilenas de 'culiado/culiada/culiao/culia' y 'qlo/klo'
    t = re.sub(r'\b[cq]l(ia(?:[oa]s?|d[oa]s?)?)\b', r'cul\1', t)
    t = re.sub(r'\b[cq]l([oa]s?)\b', r'culia\1', t)

    return t


def _compilar_patrones(lista_palabras: list[str]) -> list[tuple[str, re.Pattern]]:
    patrones = []
    for palabra in lista_palabras:
        normalizada = _normalizar_texto(palabra)
        patron = re.compile(rf'\b{re.escape(normalizada)}\b')
        patrones.append((palabra, patron))
    return patrones


PATRONES_ROJOS = _compilar_patrones(LISTA_ROJA)
PATRONES_AMARILLOS = _compilar_patrones(LISTA_AMARILLA)


def _buscar_coincidencia(texto: str, patrones: list[tuple[str, re.Pattern]]) -> str | None:
    for palabra_original, patron in patrones:
        if patron.search(texto):
            return palabra_original
    return None


# ==============================================================================
# CARGA SINGLETON DE ROBERTUITO EN MEMORIA
# ==============================================================================
_sentiment_analyzer = None
_hate_analyzer = None
_model_lock = threading.Lock()


def get_analyzers():
    global _sentiment_analyzer, _hate_analyzer
    if _sentiment_analyzer is None or _hate_analyzer is None:
        with _model_lock:
            if _sentiment_analyzer is None:
                logger.info("Cargando modelo local RoBERTuito (sentiment) en memoria...")
                _sentiment_analyzer = create_analyzer(task="sentiment", lang="es")
            if _hate_analyzer is None:
                logger.info("Cargando modelo local RoBERTuito (hate_speech) en memoria...")
                _hate_analyzer = create_analyzer(task="hate_speech", lang="es")
    return _sentiment_analyzer, _hate_analyzer


try:
    get_analyzers()
except Exception as e:
    logger.error(f"No se pudo precargar RoBERTuito al iniciar: {e}")


def _predecir_puntajes(texto_eval: str) -> tuple[float, float, float, float]:
    sentiment_ai, hate_ai = get_analyzers()
    res_sent = sentiment_ai.predict(texto_eval)
    res_hate = hate_ai.predict(texto_eval)
    return (
        float(res_sent.probas.get('NEG', 0.0)),
        float(res_hate.probas.get('hateful', 0.0)),
        float(res_hate.probas.get('targeted', 0.0)),
        float(res_hate.probas.get('aggressive', 0.0)),
    )


# ==============================================================================
# FUNCIÓN PRINCIPAL DE MODERACIÓN (DOBLE FILTRO: LÉXICO FONÉTICO + ROBERTUITO)
# ==============================================================================
def analyze_message(text: str) -> dict:
    if not text or not text.strip():
        return {'is_red': False, 'is_yellow': False, 'reason': 'empty'}

    texto_limpio = text.strip().lower()

    if any(evento in texto_limpio for evento in EVENTOS_SISTEMA):
        return {'is_red': False, 'is_yellow': False, 'reason': 'Evento técnico ignorado'}

    texto_normalizado = _normalizar_texto(text)
    texto_desofuscado = _desofuscar_jerga(texto_normalizado)

    # 1. CAPA 1 (LÉXICA ROJA SOBRE TEXTO NORMALIZADO Y DES-OFUSCADO)
    match_rojo = (
        _buscar_coincidencia(texto_normalizado, PATRONES_ROJOS)
        or _buscar_coincidencia(texto_desofuscado, PATRONES_ROJOS)
    )
    if match_rojo:
        return {
            'is_red': True,
            'is_yellow': False,
            'reason': f"Filtro Rojo detectó término prohibido: '{match_rojo}'",
            'origen_deteccion': 'FILTRO_LEXICO',
            'termino_detectado': match_rojo,
            'confianza_ia': None,
        }

    # 2. CAPA 2 (IA LOCAL ROBERTUITO SOBRE ORIGINAL Y VERSIÓN DES-OFUSCADA)
    neg_prob = hateful_prob = targeted_prob = aggressive_prob = 0.0
    try:
        neg_prob, hateful_prob, targeted_prob, aggressive_prob = _predecir_puntajes(text)

        if texto_desofuscado != texto_normalizado:
            neg_2, hate_2, targ_2, aggr_2 = _predecir_puntajes(texto_desofuscado)
            neg_prob = max(neg_prob, neg_2)
            hateful_prob = max(hateful_prob, hate_2)
            targeted_prob = max(targeted_prob, targ_2)
            aggressive_prob = max(aggressive_prob, aggr_2)
    except Exception as e:
        logger.error(f"Error ejecutando inferencia en RoBERTuito: {e}")

    if (
        hateful_prob >= 0.60
        or aggressive_prob >= 0.65
        or (hateful_prob >= 0.45 and neg_prob >= 0.85)
        or (neg_prob >= 0.90 and targeted_prob >= 0.55)
    ):
        return {
            'is_red': True,
            'is_yellow': False,
            'reason': (
                f"IA RoBERTuito detectó agresión/odio grave "
                f"(Odio: {hateful_prob:.0%}, Agresivo: {aggressive_prob:.0%}, Negativo: {neg_prob:.0%})"
            ),
            'origen_deteccion': 'IA_ROBERTUITO',
            'termino_detectado': None,
            'confianza_ia': round(max(hateful_prob, aggressive_prob, neg_prob), 4),
        }

    # 3. CAPA 3 (DOBLE FILTRO AMARILLO)
    match_amarillo = (
        _buscar_coincidencia(texto_normalizado, PATRONES_AMARILLOS)
        or _buscar_coincidencia(texto_desofuscado, PATRONES_AMARILLOS)
    )
    if match_amarillo:
        return {
            'is_red': False,
            'is_yellow': True,
            'reason': f"Filtro Amarillo detectó término ofensivo: '{match_amarillo}' (Negatividad IA: {neg_prob:.0%})",
            'origen_deteccion': 'FILTRO_LEXICO',
            'termino_detectado': match_amarillo,
            'confianza_ia': round(neg_prob, 4),
        }

    if neg_prob >= 0.78 or hateful_prob >= 0.40 or aggressive_prob >= 0.40:
        return {
            'is_red': False,
            'is_yellow': True,
            'reason': (
                f"IA RoBERTuito detectó tono hostil/negativo "
                f"(Negativo: {neg_prob:.0%}, Agresivo: {aggressive_prob:.0%})"
            ),
            'origen_deteccion': 'IA_ROBERTUITO',
            'termino_detectado': None,
            'confianza_ia': round(max(neg_prob, hateful_prob, aggressive_prob), 4),
        }

    return {
        'is_red': False,
        'is_yellow': False,
        'reason': 'Mensaje aprobado',
        'origen_deteccion': 'IA_ROBERTUITO',
        'termino_detectado': None,
        'confianza_ia': round(neg_prob, 4),
    }