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
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_IDS, PORT,
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

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNELS={PREDICTION_CHANNEL_IDS}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# --- Variables Globales d'État ---
games_history = {}
pending_finalization = {}
pending_predictions = {}

current_even_streak = 0
current_odd_streak = 0

max_even_gap = 3
max_odd_gap = 3
auto_mode = True

auto_even_gap = 3
auto_odd_gap = 3

last_game_number = 0
last_total = 0

total_even_count = 0
total_odd_count = 0
total_predictions_made = 0
total_predictions_won = 0
total_predictions_lost = 0

source_channel_ok = False
prediction_channels_ok = []

PREDICTION_WINDOW = 3
PREDICTION_TIMEOUT_MINUTES = 20

initial_analysis_done = False
GAMES_FOR_ANALYSIS = 20

# --- Fonctions Utilitaires ---

def extract_game_number(message: str):
    """Extrait le numéro de jeu (#N1074 ou #N 1074)."""
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_total_value(message: str):
    """Extrait #T avant #R."""
    match = re.search(r"#T\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def is_even(number: int) -> bool:
    return number % 2 == 0

def is_message_finalized(message: str) -> bool:
    return '✅' in message or '🔰' in message

def is_message_pending(message: str) -> bool:
    return '⏰' in message or '▶️' in message

def get_message_status(message: str) -> str:
    if is_message_finalized(message):
        return 'finalized'
    elif is_message_pending(message):
        return 'pending'
    return 'unknown'

# --- Analyse des Écarts (Fenêtre Glissante 20 Jeux) ---

def calculate_gap_stats_from_window():
    """
    Calcule les écarts max sur les 20 derniers jeux uniquement.
    Écart = nombre de positions entre deux résultats identiques.
    """
    global auto_even_gap, auto_odd_gap, initial_analysis_done
    
    if len(games_history) < GAMES_FOR_ANALYSIS:
        return False
    
    sorted_games = sorted(games_history.items(), key=lambda x: x[0])[-GAMES_FOR_ANALYSIS:]
    
    even_gaps = []
    odd_gaps = []
    
    last_even_idx = None
    last_odd_idx = None
    
    for idx, (game_num, game_data) in enumerate(sorted_games):
        if game_data['is_even']:
            if last_even_idx is not None:
                gap = idx - last_even_idx
                even_gaps.append(gap)
            last_even_idx = idx
        else:
            if last_odd_idx is not None:
                gap = idx - last_odd_idx
                odd_gaps.append(gap)
            last_odd_idx = idx
    
    old_even_gap = auto_even_gap
    old_odd_gap = auto_odd_gap
    
    if even_gaps:
        auto_even_gap = max(even_gaps)
        auto_even_gap = max(2, min(auto_even_gap, 8))
    
    if odd_gaps:
        auto_odd_gap = max(odd_gaps)
        auto_odd_gap = max(2, min(auto_odd_gap, 8))
    
    initial_analysis_done = True
    
    if old_even_gap != auto_even_gap or old_odd_gap != auto_odd_gap:
        logger.info(f"📊 Écarts mis à jour - PAIR: {old_even_gap}→{auto_even_gap}, IMPAIR: {old_odd_gap}→{auto_odd_gap}")
    
    return True

def calculate_current_streaks():
    """Calcule les séries actuelles sur les jeux historisés."""
    global current_even_streak, current_odd_streak
    
    if not games_history:
        return
    
    sorted_games = sorted(games_history.items(), key=lambda x: x[0])
    
    current_even_streak = 0
    current_odd_streak = 0
    
    for game_num, game_data in reversed(sorted_games):
        is_even_result = game_data['is_even']
        
        if current_even_streak == 0 and current_odd_streak == 0:
            if is_even_result:
                current_even_streak = 1
            else:
                current_odd_streak = 1
        else:
            if is_even_result and current_odd_streak == 0:
                current_even_streak += 1
            elif not is_even_result and current_even_streak == 0:
                current_odd_streak += 1
            else:
                break

# --- Logique de Prédiction ---

def should_predict() -> tuple:
    """
    Détermine si une prédiction doit être faite.
    Seuil = écart_max - 1
    """
    global initial_analysis_done
    
    if len(games_history) < GAMES_FOR_ANALYSIS:
        return (False, None)
    
    if not initial_analysis_done:
        if not calculate_gap_stats_from_window():
            return (False, None)
    
    active_predictions = [p for p in pending_predictions.values() if p['status'] == '🔮']
    if active_predictions:
        return (False, None)
    
    calculate_current_streaks()
    
    even_threshold = auto_even_gap if auto_mode else max_even_gap
    odd_threshold = auto_odd_gap if auto_mode else max_odd_gap
    
    if current_even_streak >= (even_threshold - 1) and current_even_streak > 0:
        return (True, "IMPAIR")
    
    if current_odd_streak >= (odd_threshold - 1) and current_odd_streak > 0:
        return (True, "PAIR")
    
    return (False, None)

async def send_prediction_to_channels(target_game: int, prediction: str):
    """Envoie la prédiction vers TOUS les canaux configurés."""
    global total_predictions_made
    
    try:
        emoji = "🔵" if prediction == "PAIR" else "🔴"
        prediction_msg = f"🎯 Jeu #{target_game} : {emoji} {prediction}"
        
        message_ids = {}
        
        for channel_id in PREDICTION_CHANNEL_IDS:
            if channel_id and channel_id != 0:
                try:
                    pred_msg = await client.send_message(channel_id, prediction_msg)
                    message_ids[channel_id] = pred_msg.id
                    logger.info(f"✅ Prédiction envoyée au canal {channel_id}: Jeu #{target_game}")
                except Exception as e:
                    logger.error(f"❌ Erreur envoi au canal {channel_id}: {e}")
                    message_ids[channel_id] = 0
        
        if not message_ids or all(v == 0 for v in message_ids.values()):
            logger.warning("⚠️ Aucun canal de prédiction accessible")
        
        pending_predictions[target_game] = {
            'prediction': prediction,
            'message_ids': message_ids,
            'status': '🔮',
            'created_at': datetime.now().isoformat(),
            'check_count': 0,
            'checked_games': []
        }
        
        total_predictions_made += 1
        
        channels_str = ', '.join([str(c) for c in message_ids.keys() if message_ids[c] != 0])
        await notify_admin(f"🔮 Nouvelle prédiction: Jeu #{target_game} = {prediction}\n"
                          f"📡 Canaux: {channels_str}\n"
                          f"📊 Séries: P={current_even_streak}/I={current_odd_streak}\n"
                          f"📈 Seuils: P={auto_even_gap}/I={auto_odd_gap}")
        
        return message_ids
        
    except Exception as e:
        logger.error(f"Erreur prédiction: {e}")
        return {}

async def update_prediction_status(game_number: int, new_status: str, won_at_offset: int = None):
    """Met à jour le statut sur TOUS les canaux."""
    global total_predictions_won, total_predictions_lost
    
    try:
        if game_number not in pending_predictions:
            return False
        
        pred = pending_predictions[game_number]
        message_ids = pred.get('message_ids', {})
        prediction = pred['prediction']
        
        emoji = "🔵" if prediction == "PAIR" else "🔴"
        
        if new_status.startswith('✅') and won_at_offset is not None:
            offset_emoji = ['0️⃣', '1️⃣', '2️⃣'][won_at_offset]
            status_text = f"✅{offset_emoji}"
        elif new_status == '❌':
            status_text = "❌"
        else:
            status_text = new_status
        
        updated_msg = f"🎯 Jeu #{game_number} : {emoji} {prediction}\n{status_text}"
        
        updated_channels = []
        for channel_id, msg_id in message_ids.items():
            if channel_id and msg_id > 0:
                try:
                    await client.edit_message(channel_id, msg_id, updated_msg)
                    updated_channels.append(str(channel_id))
                except Exception as e:
                    logger.error(f"❌ Erreur édition canal {channel_id}: {e}")
        
        pred['status'] = status_text
        
        if new_status.startswith('✅'):
            total_predictions_won += 1
            del pending_predictions[game_number]
            await notify_admin(f"✅ Prédiction #{game_number} GAGNÉE {status_text}\n"
                              f"📡 Canaux: {', '.join(updated_channels)}")
        elif new_status == '❌':
            total_predictions_lost += 1
            del pending_predictions[game_number]
            await notify_admin(f"❌ Prédiction #{game_number} PERDUE\n"
                              f"📡 Canaux: {', '.join(updated_channels)}")
        
        return True
        
    except Exception as e:
        logger.error(f"Erreur update: {e}")
        return False

async def check_prediction_result(game_number: int, total: int, is_even: bool):
    """Vérifie si prédiction gagnée."""
    for pred_game_num, pred_data in list(pending_predictions.items()):
        if pred_data['status'] != '🔮':
            continue
            
        offset = game_number - pred_game_num
        
        if 0 <= offset < PREDICTION_WINDOW:
            if game_number in pred_data.get('checked_games', []):
                return
                
            predicted_type = pred_data['prediction']
            is_correct = (predicted_type == "PAIR" and is_even) or (predicted_type == "IMPAIR" and not is_even)
            
            pred_data['checked_games'].append(game_number)
            pred_data['check_count'] = len(pred_data['checked_games'])
            pred_data['last_check'] = datetime.now()
            
            if is_correct:
                await update_prediction_status(pred_game_num, '✅ GAGNÉ', offset)
                return
            
            else:
                if pred_data['check_count'] >= PREDICTION_WINDOW:
                    await update_prediction_status(pred_game_num, '❌', None)

async def notify_admin(message: str):
    """Envoie une notification à l'admin."""
    try:
        if ADMIN_ID and ADMIN_ID != 0:
            await client.send_message(ADMIN_ID, f"🤖 *Bot Notification*\n\n{message}", parse_mode='markdown')
    except Exception as e:
        logger.error(f"Erreur notif admin: {e}")

# --- Traitement des Messages ---

async def process_message(message_text: str, chat_id: int, is_edit: bool = False):
    """Traite un message."""
    # DÉCLARATION GLOBALE CORRIGÉE - Toutes les variables modifiées
    global last_game_number, last_total, total_even_count, total_odd_count
    global current_even_streak, current_odd_streak, initial_analysis_done
    global games_history, pending_finalization
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        
        total = extract_total_value(message_text)
        status = get_message_status(message_text)
        
        logger.info(f"📨 Jeu #{game_number} | Status: {status} | Total: {total}")
        
        # Message en attente
        if status == 'pending':
            pending_finalization[game_number] = {
                'message_text': message_text,
                'received_at': datetime.now()
            }
            return
        
        # Message finalisé
        if status == 'finalized':
            if game_number in pending_finalization:
                del pending_finalization[game_number]
            
            if total is None:
                return
            
            if game_number in games_history and not is_edit:
                return
            
            is_even_result = is_even(total)
            
            # Incrémentation des compteurs globaux
            if is_even_result:
                total_even_count += 1
            else:
                total_odd_count += 1
            
            # Ajouter à l'historique (fenêtre glissante)
            games_history[game_number] = {
                'total': total,
                'is_even': is_even_result,
                'timestamp': datetime.now().isoformat()
            }
            
            # Garder uniquement les 20 derniers
            if len(games_history) > GAMES_FOR_ANALYSIS:
                oldest = min(games_history.keys())
                del games_history[oldest]
            
            # Recalculer écarts en temps réel
            if len(games_history) >= GAMES_FOR_ANALYSIS:
                calculate_gap_stats_from_window()
            
            # Vérifier prédictions
            await check_prediction_result(game_number, total, is_even_result)
            
            # Nouvelle prédiction ?
            should_pred, prediction_type = should_predict()
            
            if should_pred and prediction_type:
                target_game = game_number + 2
                if target_game not in pending_predictions:
                    await send_prediction_to_channels(target_game, prediction_type)
            
            last_game_number = game_number
            last_total = total
            
    except Exception as e:
        logger.error(f"Erreur traitement: {e}")
        import traceback
        logger.error(traceback.format_exc())

# --- Gestionnaires d'Événements ---

@client.on(events.NewMessage())
async def handle_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            await process_message(event.message.message, chat_id, False)
    except Exception as e:
        logger.error(f"Erreur handle: {e}")

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            logger.info(f"✏️ Édition détectée")
            await process_message(event.message.message, chat_id, True)
    except Exception as e:
        logger.error(f"Erreur édition: {e}")

# --- Commandes ---

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond(
        "🤖 **Bot Prédiction Pair/Impair**\n\n"
        "Commandes:\n"
        "`/status` - État\n"
        "`/info` - Canaux\n"
        "`/histo` - Historique 20 jeux\n"
        "`/setmode auto/manual`\n"
        "`/setgap pair/impair <n>`\n"
        "`/stats` - Statistiques\n"
        "`/reset` - Reset manuel"
    )

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    calculate_current_streaks()
    
    channels_str = ', '.join([str(c) for c in PREDICTION_CHANNEL_IDS if c])
    
    msg = (
        f"📊 **État**\n"
        f"🎮 Dernier: #{last_game_number}\n"
        f"📈 P:{total_even_count} I:{total_odd_count}\n"
        f"🔥 Séries: P={current_even_streak} I={current_odd_streak}\n"
        f"⚙️ Mode: {'Auto' if auto_mode else 'Manuel'}\n"
        f"📊 Écarts: P={auto_even_gap} I={auto_odd_gap}\n"
        f"📡 Canaux: {channels_str}\n"
        f"🔮 En cours: {len([p for p in pending_predictions.values() if p['status'] == '🔮'])}\n"
        f"✅ {total_predictions_won} | ❌ {total_predictions_lost}"
    )
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/info'))
async def cmd_info(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    channels_str = '\n'.join([f"• `{c}`" for c in PREDICTION_CHANNEL_IDS if c])
    
    msg = (
        f"ℹ️ **Info**\n"
        f"📡 Source: `{SOURCE_CHANNEL_ID}`\n"
        f"📡 Prédictions:\n{channels_str}\n"
        f"🎮 Dernier: `{last_game_number}`\n"
        f"⏳ En attente: {len(pending_finalization)}\n"
        f"🔮 Actives: {len([p for p in pending_predictions.values() if p['status'] == '🔮'])}"
    )
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/histo'))
async def cmd_histo(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    if not games_history:
        await event.respond("📭 Vide")
        return
    
    sorted_games = sorted(games_history.items(), key=lambda x: x[0])[-20:]
    
    lines = ["📜 **20 derniers jeux**\n"]
    for num, data in sorted_games:
        emoji = "🔵" if data['is_even'] else "🔴"
        lines.append(f"#{num}:{data['total']}{emoji}")
    
    even_pos = [i for i, (_, d) in enumerate(sorted_games) if d['is_even']]
    odd_pos = [i for i, (_, d) in enumerate(sorted_games) if not d['is_even']]
    
    even_gaps = [even_pos[i]-even_pos[i-1] for i in range(1, len(even_pos))] if len(even_pos) > 1 else []
    odd_gaps = [odd_pos[i]-odd_pos[i-1] for i in range(1, len(odd_pos))] if len(odd_pos) > 1 else []
    
    lines.append(f"\n📊 Écarts max: 🔵{max(even_gaps) if even_gaps else 0} 🔴{max(odd_gaps) if odd_gaps else 0}")
    lines.append(f"🤖 Seuils: 🔵{auto_even_gap} 🔴{auto_odd_gap}")
    
    await event.respond("\n".join(lines))

@client.on(events.NewMessage(pattern='/setmode'))
async def cmd_setmode(event):
    global auto_mode
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    parts = event.message.message.split()
    if len(parts) < 2:
        await event.respond("Usage: `/setmode auto` ou `manual`")
        return
    
    if parts[1].lower() == 'auto':
        auto_mode = True
        calculate_gap_stats_from_window()
        await event.respond(f"✅ Auto | Écarts: P={auto_even_gap} I={auto_odd_gap}")
    elif parts[1].lower() == 'manual':
        auto_mode = False
        await event.respond(f"✅ Manuel | Écarts: P={max_even_gap} I={max_odd_gap}")

@client.on(events.NewMessage(pattern='/setgap'))
async def cmd_setgap(event):
    global max_even_gap, max_odd_gap
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    parts = event.message.message.split()
    if len(parts) < 3:
        await event.respond("Usage: `/setgap pair 4`")
        return
    
    try:
        val = int(parts[2])
        if parts[1].lower() == 'pair':
            max_even_gap = val
            await event.respond(f"✅ Écart PAIR: {val}")
        elif parts[1].lower() == 'impair':
            max_odd_gap = val
            await event.respond(f"✅ Écart IMPAIR: {val}")
    except:
        await event.respond("❌ Valeur invalide")

@client.on(events.NewMessage(pattern='/stats'))
async def cmd_stats(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    win_rate = (total_predictions_won / total_predictions_made * 100) if total_predictions_made > 0 else 0
    
    msg = (
        f"📈 **Stats**\n"
        f"Jeux: {len(games_history)}\n"
        f"🔵 P:{total_even_count} 🔴 I:{total_odd_count}\n"
        f"🔮 Total: {total_predictions_made}\n"
        f"✅ {total_predictions_won} | ❌ {total_predictions_lost}\n"
        f"📊 Taux: {win_rate:.1f}%"
    )
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    await perform_reset("Manuel par admin")
    await event.respond("✅ Reset effectué")

# --- Tâches Automatiques ---

async def perform_reset(reason: str = "Automatique"):
    """Effectue le reset complet."""
    global games_history, pending_predictions, pending_finalization
    global current_even_streak, current_odd_streak, initial_analysis_done
    global total_even_count, total_odd_count, total_predictions_made
    global total_predictions_won, total_predictions_lost, last_game_number, last_total
    
    logger.warning(f"🚨 RESET: {reason}")
    
    games_history.clear()
    pending_predictions.clear()
    pending_finalization.clear()
    current_even_streak = current_odd_streak = 0
    total_even_count = total_odd_count = 0
    total_predictions_made = total_predictions_won = total_predictions_lost = 0
    last_game_number = last_total = 0
    initial_analysis_done = False
    
    await notify_admin(f"🚨 **RESET EFFECTUÉ**\nRaison: {reason}\nLe bot repart à zéro.")
    logger.warning("✅ Reset terminé")

async def check_prediction_timeouts():
    """Vérifie les timeouts de prédiction (20min)."""
    while True:
        try:
            await asyncio.sleep(60)
            
            now = datetime.now()
            expired = []
            
            for game_num, pred in pending_predictions.items():
                if pred['status'] != '🔮':
                    continue
                    
                created = datetime.fromisoformat(pred['created_at'])
                if now - created > timedelta(minutes=PREDICTION_TIMEOUT_MINUTES):
                    expired.append(game_num)
            
            if expired:
                logger.warning(f"🚨 {len(expired)} prédiction(s) en timeout!")
                await perform_reset(f"Timeout prédiction après {PREDICTION_TIMEOUT_MINUTES}min")
                
        except Exception as e:
            logger.error(f"Erreur timeout check: {e}")

async def schedule_daily_reset():
    """Reset quotidien à 1h00 heure du Bénin (WAT)."""
    wat_tz = timezone(timedelta(hours=1))
    
    while True:
        now = datetime.now(wat_tz)
        target = datetime.combine(now.date(), time(1, 0), tzinfo=wat_tz)
        
        if now >= target:
            target += timedelta(days=1)
        
        wait_seconds = (target - now).total_seconds()
        logger.info(f"⏳ Prochain reset: {target.strftime('%d/%m %H:%M')} WAT")
        
        await asyncio.sleep(wait_seconds)
        await perform_reset("Reset quotidien 1h00 WAT")

# --- Serveur Web ---

async def index(request):
    html = f"""<!DOCTYPE html>
    <html>
    <head><title>Bot</title>
    <style>
        body {{ font-family: Arial; max-width: 600px; margin: 50px auto; padding: 20px; }}
        .box {{ background: #f0f0f0; padding: 20px; border-radius: 10px; }}
    </style>
    </head>
    <body>
        <h1>🎯 Bot Prédiction</h1>
        <div class="box">
            <p>✅ En ligne</p>
            <p>Dernier: #{last_game_number}</p>
            <p>Mode: {'Auto' if auto_mode else 'Manuel'}</p>
            <p>Écarts: P={auto_even_gap} I={auto_odd_gap}</p>
            <p>Actives: {len([p for p in pending_predictions.values() if p['status'] == '🔮'])}</p>
        </div>
    </body>
    </html>"""
    return web.Response(text=html, content_type='text/html')

async def health_check(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Web port {PORT}")

# --- Démarrage ---

async def start_bot():
    global source_channel_ok, prediction_channels_ok
    try:
        await client.start(bot_token=BOT_TOKEN)
        source_channel_ok = True
        prediction_channels_ok = [c for c in PREDICTION_CHANNEL_IDS if c]
        logger.info("✅ Bot connecté")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur: {e}")
        return False

async def main():
    try:
        await start_web_server()
        
        if not await start_bot():
            return
        
        asyncio.create_task(schedule_daily_reset())
        asyncio.create_task(check_prediction_timeouts())
        
        logger.info("🚀 Bot opérationnel!")
        await client.run_until_disconnected()
    
    except Exception as e:
        logger.error(f"Erreur: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Arrêt")
    except Exception as e:
        logger.error(f"Fatal: {e}")
