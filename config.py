"""
Configuration du bot Telegram de prédiction Pair/Impair
"""
import os

def parse_channel_id(env_var: str, default: str) -> int:
    value = os.getenv(env_var) or default
    channel_id = int(value)
    # Convertit l'ID positif en format ID de canal Telegram négatif si nécessaire
    if channel_id > 0 and len(str(channel_id)) >= 10:
        channel_id = -channel_id
    return channel_id

# ID du canal source (où les résultats arrivent)
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')

# ID DU CANAL DE PRÉDICTION
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003725380926')

ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')

API_ID = int(os.getenv('API_ID') or '0')
API_HASH = os.getenv('API_HASH') or ''
BOT_TOKEN = os.getenv('BOT_TOKEN') or '7749786995:AAGr9rk_uuykLLp5g7Hi3XwIlsdMfW9pWFw'

PORT = int(os.getenv('PORT') or '10000')

# Paramètres de prédiction Pair/Impair
DEFAULT_AUTO_CHECK_INTERVAL = 20  # Vérification auto tous les 20 jeux
MAX_HISTORY_SIZE = 100  # Taille maximale de l'historique des jeux
