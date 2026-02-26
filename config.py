"""
Configuration du bot Telegram de prédiction Pair/Impair
"""

# ============================================
# IDENTIFIANTS TELEGRAM (ESSENTIELS)
# ============================================

API_ID = 29177661
API_HASH = 'a8639172fa8d35dbfd8ea46286d349ab'
BOT_TOKEN = '7749786995:AAGr9rk_uuykLLp5g7Hi3XwIlsdMfW9pWFw'
ADMIN_ID = 1190237801

# ============================================
# CONFIGURATION DES CANAUX
# ============================================

SOURCE_CHANNEL_ID = -1002682552255

# PLUSIEURS CANAUX DE PRÉDICTION (liste)
PREDICTION_CHANNEL_IDS = [
    -1003725380926,  # Canal principal
    # -1001234567890,  # Canal secondaire (décommentez pour ajouter)
    # -1000987654321,  # Canal tertiaire (décommentez pour ajouter)
]

# ============================================
# CONFIGURATION SERVEUR
# ============================================

PORT = 10000

# ============================================
# PARAMÈTRES DE PRÉDICTION PAIR/IMPAIR
# ============================================

DEFAULT_AUTO_CHECK_INTERVAL = 20
MAX_HISTORY_SIZE = 1000
