import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    DEFAULT_AUTO_CHECK_INTERVAL, MAX_HISTORY_SIZE
)

# --- Configuration et Initialisation ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Vérifications minimales de la configuration
if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNEL={PREDICTION_CHANNEL_ID}")

# Initialisation du client Telegram
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# --- Variables Globales d'État ---
# Historique des jeux: {game_number: {'total': int, 'is_even': bool, 'timestamp': str}}
games_history = {}

# Prédictions actives: {target_game: {'prediction': 'PAIR'/'IMPAIR', 'message_id': int, 'status': str, 'created_at': str}}
pending_predictions = {}

# Compteurs pour les écarts
current_even_streak = 0  # Série actuelle de pairs consécutifs
current_odd_streak = 0   # Série actuelle d'impairs consécutifs

# Configuration des écarts
max_even_gap = 3  # Écart max entre numéros pairs (défaut)
max_odd_gap = 3   # Écart max entre numéros impairs (défaut)
auto_mode = True  # Mode automatique par défaut

# Statistiques des écarts calculés automatiquement
auto_even_gap = 3
auto_odd_gap = 3

# Dernier numéro de jeu traité
last_game_number = 0
last_total = 0

# Compteurs globaux
total_even_count = 0
total_odd_count = 0
total_predictions_made = 0
total_predictions_won = 0
total_predictions_lost = 0

# Flags de canal
source_channel_ok = False
prediction_channel_ok = False

# --- Fonctions d'Analyse ---

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message (format #N uniquement)."""
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_total_value(message: str):
    """Extrait la valeur totale (#T) du message."""
    # Cherche #T suivi d'un nombre
    match = re.search(r"#T\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Alternative: chercher un nombre après "Total" ou dans un format spécifique
    match = re.search(r"[Tt]otal[\s:]*(\d+)", message)
    if match:
        return int(match.group(1))
    return None

def is_even(number: int) -> bool:
    """Vérifie si un nombre est pair."""
    return number % 2 == 0

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est un résultat final (non en cours)."""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message or '#T' in message

# --- Logique de Calcul des Écarts ---

def calculate_gap_stats():
    """Calcule les écarts max entre numéros pairs et impairs depuis l'historique."""
    global auto_even_gap, auto_odd_gap
    
    if len(games_history) < 10:
        return
    
    # Trier les jeux par numéro
    sorted_games = sorted(games_history.items(), key=lambda x: x[0])
    
    # Calculer les écarts entre pairs consécutifs
    even_gaps = []
    odd_gaps = []
    
    last_even_game = None
    last_odd_game = None
    
    for game_num, game_data in sorted_games:
        if game_data['is_even']:
            if last_even_game is not None:
                even_gaps.append(game_num - last_even_game)
            last_even_game = game_num
        else:
            if last_odd_game is not None:
                odd_gaps.append(game_num - last_odd_game)
            last_odd_game = game_num
    
    # Calculer les écarts max (utiliser le 90e percentile pour éviter les outliers)
    if even_gaps:
        even_gaps_sorted = sorted(even_gaps)
        auto_even_gap = even_gaps_sorted[int(len(even_gaps_sorted) * 0.9)] if even_gaps_sorted else 3
        auto_even_gap = max(2, min(auto_even_gap, 6))  # Limiter entre 2 et 6
    
    if odd_gaps:
        odd_gaps_sorted = sorted(odd_gaps)
        auto_odd_gap = odd_gaps_sorted[int(len(odd_gaps_sorted) * 0.9)] if odd_gaps_sorted else 3
        auto_odd_gap = max(2, min(auto_odd_gap, 6))  # Limiter entre 2 et 6
    
    logger.info(f"📊 Stats auto calculées - Écart Pair max: {auto_even_gap}, Écart Impair max: {auto_odd_gap}")

# --- Logique de Prédiction ---

def should_predict() -> tuple:
    """
    Détermine si une prédiction doit être faite et laquelle.
    Retourne: (should_predict: bool, prediction: str/None)
    """
    global max_even_gap, max_odd_gap
    
    # Utiliser les valeurs auto ou manuelles selon le mode
    even_threshold = auto_even_gap if auto_mode else max_even_gap
    odd_threshold = auto_odd_gap if auto_mode else max_odd_gap
    
    # Si on a une série de pairs consécutifs atteignant le max
    if current_even_streak >= even_threshold:
        return (True, "IMPAIR")  # Prédire impair après une longue série de pairs
    
    # Si on a une série d'impairs consécutifs atteignant le max
    if current_odd_streak >= odd_threshold:
        return (True, "PAIR")  # Prédire pair après une longue série d'impairs
    
    return (False, None)

async def send_prediction_to_channel(target_game: int, prediction: str):
    """Envoie la prédiction au canal de prédiction."""
    global total_predictions_made
    
    try:
        emoji = "🔵" if prediction == "PAIR" else "🔴"
        prediction_msg = f"🎯 Prédiction Jeu #{target_game}: {emoji} {prediction}\n📊 Statut: 🔮 En attente"
        
        msg_id = 0
        
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée: Jeu #{target_game} = {prediction}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction au canal: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible")
        
        pending_predictions[target_game] = {
            'prediction': prediction,
            'message_id': msg_id,
            'status': '🔮',
            'created_at': datetime.now().isoformat()
        }
        
        total_predictions_made += 1
        return msg_id
        
    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le statut d'une prédiction."""
    global total_predictions_won, total_predictions_lost
    
    try:
        if game_number not in pending_predictions:
            return False
        
        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        prediction = pred['prediction']
        
        emoji = "🔵" if prediction == "PAIR" else "🔴"
        updated_msg = f"🎯 Prédiction Jeu #{game_number}: {emoji} {prediction}\n📊 Statut: {new_status}"
        
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")
        
        pred['status'] = new_status
        
        # Mettre à jour les compteurs
        if new_status == '✅ GAGNÉ':
            total_predictions_won += 1
            del pending_predictions[game_number]
        elif new_status == '❌ PERDU':
            total_predictions_lost += 1
            del pending_predictions[game_number]
        
        return True
        
    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def check_prediction_result(game_number: int, total: int, is_even: bool):
    """Vérifie si une prédiction active correspond au résultat."""
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        predicted_type = pred['prediction']
        
        # Vérifier si la prédiction est correcte
        if (predicted_type == "PAIR" and is_even) or (predicted_type == "IMPAIR" and not is_even):
            await update_prediction_status(game_number, '✅ GAGNÉ')
            logger.info(f"🎉 Prédiction #{game_number} GAGNÉE! Attendu: {predicted_type}, Reçu: {total} ({'PAIR' if is_even else 'IMPAIR'})")
        else:
            await update_prediction_status(game_number, '❌ PERDU')
            logger.info(f"😞 Prédiction #{game_number} PERDUE! Attendu: {predicted_type}, Reçu: {total} ({'PAIR' if is_even else 'IMPAIR'})")

# --- Traitement des Messages ---

async def process_finalized_message(message_text: str, chat_id: int):
    """Traite un message finalisé du canal source."""
    global last_game_number, last_total, current_even_streak, current_odd_streak
    global total_even_count, total_odd_count
    
    try:
        if not is_message_finalized(message_text):
            return
        
        # Extraire le numéro de jeu
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        
        # Extraire la valeur totale (#T)
        total = extract_total_value(message_text)
        if total is None:
            logger.warning(f"⚠️ Impossible d'extraire le total du message: {message_text[:100]}")
            return
        
        # Déterminer si pair ou impair
        is_even_result = is_even(total)
        
        logger.info(f"🎮 Jeu #{game_number} - Total: {total} ({'PAIR' if is_even_result else 'IMPAIR'})")
        
        # Mettre à jour les séries
        if is_even_result:
            current_even_streak += 1
            current_odd_streak = 0
            total_even_count += 1
        else:
            current_odd_streak += 1
            current_even_streak = 0
            total_odd_count += 1
        
        # Stocker dans l'historique
        games_history[game_number] = {
            'total': total,
            'is_even': is_even_result,
            'timestamp': datetime.now().isoformat()
        }
        
        # Limiter la taille de l'historique
        if len(games_history) > MAX_HISTORY_SIZE:
            oldest = min(games_history.keys())
            del games_history[oldest]
        
        # Vérifier si une prédiction active correspond
        await check_prediction_result(game_number, total, is_even_result)
        
        # Recalculer les stats auto tous les 20 jeux
        if game_number % DEFAULT_AUTO_CHECK_INTERVAL == 0 and auto_mode:
            calculate_gap_stats()
            logger.info(f"🔄 Recalcul auto des écarts au jeu #{game_number}")
        
        # Vérifier si on doit faire une prédiction
        should_pred, prediction_type = should_predict()
        
        if should_pred and prediction_type:
            target_game = game_number + 1
            # Éviter les doublons
            if target_game not in pending_predictions:
                await send_prediction_to_channel(target_game, prediction_type)
                logger.info(f"🔮 Prédiction créée: Jeu #{target_game} = {prediction_type} (Série Pairs: {current_even_streak}, Impairs: {current_odd_streak})")
        
        # Mettre à jour les variables globales
        last_game_number = game_number
        last_total = total
        
    except Exception as e:
        logger.error(f"Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# --- Gestion des Messages (Hooks Telethon) ---

@client.on(events.NewMessage())
async def handle_message(event):
    """Gère les nouveaux messages dans le canal source."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            await process_finalized_message(message_text, chat_id)
    
    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    """Gère les messages édités dans le canal source."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            await process_finalized_message(message_text, chat_id)
    
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

# --- Commandes Administrateur ---

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond(
        "🤖 **Bot de Prédiction Pair/Impair**\n\n"
        "Commandes disponibles:\n"
        "`/status` - Voir l'état du bot\n"
        "`/setmode auto` - Mode automatique\n"
        "`/setmode manual` - Mode manuel\n"
        "`/setgap pair <n>` - Définir écart max pair\n"
        "`/setgap impair <n>` - Définir écart max impair\n"
        "`/stats` - Voir les statistiques\n"
        "`/help` - Aide détaillée"
    )

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Commande réservée à l'administrateur")
        return
    
    mode_str = "🤖 Automatique" if auto_mode else "👤 Manuel"
    even_gap = auto_even_gap if auto_mode else max_even_gap
    odd_gap = auto_odd_gap if auto_mode else max_odd_gap
    
    status_msg = (
        f"📊 **État du Bot**\n\n"
        f"🎮 Dernier jeu: #{last_game_number}\n"
        f"🔢 Dernier total: {last_total} ({'PAIR' if is_even(last_total) else 'IMPAIR' if last_total > 0 else 'N/A'})\n\n"
        f"📈 **Compteurs:**\n"
        f"• Pairs: {total_even_count}\n"
        f"• Impairs: {total_odd_count}\n"
        f"• Séries Pairs actuelle: {current_even_streak}\n"
        f"• Séries Impairs actuelle: {current_odd_streak}\n\n"
        f"⚙️ **Configuration:**\n"
        f"• Mode: {mode_str}\n"
        f"• Écart Pair max: {even_gap}\n"
        f"• Écart Impair max: {odd_gap}\n\n"
        f"🔮 **Prédictions:**\n"
        f"• Actives: {len(pending_predictions)}\n"
        f"• Total faites: {total_predictions_made}\n"
        f"• Gagnées: {total_predictions_won}\n"
        f"• Perdues: {total_predictions_lost}"
    )
    
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/setmode'))
async def cmd_setmode(event):
    global auto_mode
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Commande réservée à l'administrateur")
        return
    
    message_parts = event.message.message.split()
    if len(message_parts) < 2:
        await event.respond("❌ Usage: `/setmode auto` ou `/setmode manual`")
        return
    
    mode = message_parts[1].lower()
    
    if mode == 'auto':
        auto_mode = True
        calculate_gap_stats()
        await event.respond(f"✅ Mode **AUTOMATIQUE** activé\n\n📊 Écarts calculés - Pair: {auto_even_gap}, Impair: {auto_odd_gap}")
    elif mode == 'manual':
        auto_mode = False
        await event.respond(
            f"✅ Mode **MANUEL** activé\n\n"
            f"Écarts actuels - Pair: {max_even_gap}, Impair: {max_odd_gap}\n"
            f"Utilisez `/setgap pair <n>` et `/setgap impair <n>` pour modifier"
        )
    else:
        await event.respond("❌ Mode invalide. Utilisez `auto` ou `manual`")

@client.on(events.NewMessage(pattern='/setgap'))
async def cmd_setgap(event):
    global max_even_gap, max_odd_gap, auto_mode
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Commande réservée à l'administrateur")
        return
    
    message_parts = event.message.message.split()
    if len(message_parts) < 3:
        await event.respond("❌ Usage: `/setgap pair <n>` ou `/setgap impair <n>`")
        return
    
    gap_type = message_parts[1].lower()
    try:
        gap_value = int(message_parts[2])
        if gap_value < 2 or gap_value > 10:
            await event.respond("❌ L'écart doit être entre 2 et 10")
            return
    except ValueError:
        await event.respond("❌ Valeur invalide")
        return
    
    if gap_type == 'pair':
        max_even_gap = gap_value
        await event.respond(f"✅ Écart max pour les **PAIRS** défini à: **{gap_value}**")
    elif gap_type == 'impair':
        max_odd_gap = gap_value
        await event.respond(f"✅ Écart max pour les **IMPAIRS** défini à: **{gap_value}**")
    else:
        await event.respond("❌ Type invalide. Utilisez `pair` ou `impair`")

@client.on(events.NewMessage(pattern='/stats'))
async def cmd_stats(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Commande réservée à l'administrateur")
        return
    
    # Calculer les écarts depuis l'historique
    even_gaps = []
    odd_gaps = []
    last_even = None
    last_odd = None
    
    for game_num, game_data in sorted(games_history.items()):
        if game_data['is_even']:
            if last_even is not None:
                even_gaps.append(game_num - last_even)
            last_even = game_num
        else:
            if last_odd is not None:
                odd_gaps.append(game_num - last_odd)
            last_odd = game_num
    
    even_max = max(even_gaps) if even_gaps else 0
    even_avg = sum(even_gaps) / len(even_gaps) if even_gaps else 0
    odd_max = max(odd_gaps) if odd_gaps else 0
    odd_avg = sum(odd_gaps) / len(odd_gaps) if odd_gaps else 0
    
    win_rate = (total_predictions_won / total_predictions_made * 100) if total_predictions_made > 0 else 0
    
    stats_msg = (
        f"📈 **Statistiques Détaillées**\n\n"
        f"🎮 Jeux analysés: {len(games_history)}\n"
        f"🔢 Pairs: {total_even_count} | Impairs: {total_odd_count}\n\n"
        f"📊 **Écarts Pairs:**\n"
        f"• Max observé: {even_max}\n"
        f"• Moyenne: {even_avg:.2f}\n"
        f"• Seuil actuel: {auto_even_gap if auto_mode else max_even_gap}\n\n"
        f"📊 **Écarts Impairs:**\n"
        f"• Max observé: {odd_max}\n"
        f"• Moyenne: {odd_avg:.2f}\n"
        f"• Seuil actuel: {auto_odd_gap if auto_mode else max_odd_gap}\n\n"
        f"🔮 **Prédictions:**\n"
        f"• Total: {total_predictions_made}\n"
        f"• Gagnées: {total_predictions_won} ✅\n"
        f"• Perdues: {total_predictions_lost} ❌\n"
        f"• Taux de réussite: {win_rate:.1f}%"
    )
    
    await event.respond(stats_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    help_msg = (
        "📖 **Aide - Bot de Prédiction Pair/Impair**\n\n"
        "**Fonctionnement:**\n"
        "Le bot analyse les totaux (#T) des jeux et compte les écarts entre numéros pairs/impairs.\n\n"
        "**Logique de prédiction:**\n"
        "• Si une série de pairs atteint l'écart max → prédit IMPAIR\n"
        "• Si une série d'impairs atteint l'écart max → prédit PAIR\n\n"
        "**Modes:**\n"
        "• **Automatique**: Le bot calcule les écarts max tous les 20 jeux\n"
        "• **Manuel**: Vous définissez les écarts avec `/setgap`\n\n"
        "**Commandes:**\n"
        "• `/status` - État actuel du bot\n"
        "• `/setmode auto/manual` - Changer de mode\n"
        "• `/setgap pair <n>` - Définir écart max pair (2-10)\n"
        "• `/setgap impair <n>` - Définir écart max impair (2-10)\n"
        "• `/stats` - Statistiques détaillées\n"
        "• `/help` - Cette aide"
    )
    
    await event.respond(help_msg)

# --- Serveur Web et Démarrage ---

async def index(request):
    html = f"""<!DOCTYPE html>
    <html>
    <head>
        <title>Bot Prédiction Pair/Impair</title>
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }}
            h1 {{ color: #333; }}
            .status {{ background: #f0f0f0; padding: 15px; border-radius: 8px; margin: 20px 0; }}
        </style>
    </head>
    <body>
        <h1>🎯 Bot de Prédiction Pair/Impair</h1>
        <div class="status">
            <p><strong>Statut:</strong> ✅ En ligne</p>
            <p><strong>Dernier jeu:</strong> #{last_game_number}</p>
            <p><strong>Dernier total:</strong> {last_total}</p>
            <p><strong>Mode:</strong> {'Automatique' if auto_mode else 'Manuel'}</p>
            <p><strong>Prédictions actives:</strong> {len(pending_predictions)}</p>
        </div>
    </body>
    </html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    """Démarre le serveur web."""
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Serveur web démarré sur le port {PORT}")

async def schedule_daily_reset():
    """Tâche planifiée pour la réinitialisation quotidienne."""
    wat_tz = timezone(timedelta(hours=1))
    reset_time = time(0, 59, tzinfo=wat_tz)
    
    logger.info(f"🕐 Reset quotidien planifié pour {reset_time} WAT")
    
    while True:
        now = datetime.now(wat_tz)
        target_datetime = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target_datetime:
            target_datetime += timedelta(days=1)
        
        time_to_wait = (target_datetime - now).total_seconds()
        logger.info(f"⏳ Prochain reset dans {timedelta(seconds=time_to_wait)}")
        await asyncio.sleep(time_to_wait)
        
        logger.warning("🚨 RESET QUOTIDIEN DÉCLENCHÉ!")
        
        global games_history, pending_predictions, current_even_streak, current_odd_streak
        global total_even_count, total_odd_count, total_predictions_made
        global total_predictions_won, total_predictions_lost, last_game_number, last_total
        
        games_history.clear()
        pending_predictions.clear()
        current_even_streak = 0
        current_odd_streak = 0
        total_even_count = 0
        total_odd_count = 0
        total_predictions_made = 0
        total_predictions_won = 0
        total_predictions_lost = 0
        last_game_number = 0
        last_total = 0
        
        logger.warning("✅ Toutes les données ont été réinitialisées")

async def start_bot():
    """Démarre le client Telegram."""
    global source_channel_ok, prediction_channel_ok
    try:
        await client.start(bot_token=BOT_TOKEN)
        
        source_channel_ok = True
        prediction_channel_ok = True
        logger.info("✅ Bot connecté avec succès")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur démarrage du bot: {e}")
        return False

async def main():
    """Fonction principale."""
    try:
        await start_web_server()
        
        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage du bot")
            return
        
        # Lancer le reset quotidien en arrière-plan
        asyncio.create_task(schedule_daily_reset())
        
        logger.info("🚀 Bot complètement opérationnel!")
        await client.run_until_disconnected()
    
    except Exception as e:
        logger.error(f"Erreur dans main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
