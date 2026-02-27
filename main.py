import os
import asyncio
import re
import logging
import sys
import json
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

# Écarts en mode manuel (définis par admin)
manual_even_gap = 3
manual_odd_gap = 3

# Écarts en mode auto (calculés par le bot)
auto_even_gap = 3
auto_odd_gap = 3

auto_mode = True

last_game_number = 0
last_total = 0

total_even_count = 0
total_odd_count = 0
total_predictions_made = 0
total_predictions_won = 0
total_predictions_lost = 0

source_channel_ok = False

DYNAMIC_PREDICTION_CHANNELS = []

PREDICTION_WINDOW = 3
PREDICTION_TIMEOUT_MINUTES = 20

initial_analysis_done = False
GAMES_FOR_ANALYSIS = 20

CHANNELS_FILE = 'dynamic_channels.json'

# --- Fonctions Utilitaires ---

def load_dynamic_channels():
    global DYNAMIC_PREDICTION_CHANNELS
    try:
        if os.path.exists(CHANNELS_FILE):
            with open(CHANNELS_FILE, 'r') as f:
                loaded = json.load(f)
                if loaded:
                    DYNAMIC_PREDICTION_CHANNELS = loaded
                    logger.info(f"📂 Canaux chargés: {len(DYNAMIC_PREDICTION_CHANNELS)}")
    except Exception as e:
        logger.error(f"Erreur chargement canaux: {e}")

def save_dynamic_channels():
    try:
        with open(CHANNELS_FILE, 'w') as f:
            json.dump(DYNAMIC_PREDICTION_CHANNELS, f)
        logger.info(f"💾 Canaux sauvegardés: {len(DYNAMIC_PREDICTION_CHANNELS)}")
    except Exception as e:
        logger.error(f"Erreur sauvegarde canaux: {e}")

def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_total_value(message: str):
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

# --- Analyse des Écarts ---

def calculate_gap_stats_from_window():
    """
    Calcule les écarts max sur les 20 derniers jeux.
    Ne fonctionne qu'en mode AUTO.
    """
    global auto_even_gap, auto_odd_gap, initial_analysis_done
    
    # Ne recalcule PAS en mode manuel
    if not auto_mode:
        logger.info("👤 Mode manuel actif - Pas de recalcul auto des écarts")
        return False
    
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
        logger.info(f"📊 Écarts AUTO mis à jour - P:{old_even_gap}→{auto_even_gap}, I:{old_odd_gap}→{auto_odd_gap}")
    
    return True

def calculate_current_streaks():
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

def get_current_thresholds():
    """
    Retourne les seuils actuels selon le mode.
    Mode AUTO: utilise auto_even_gap/auto_odd_gap
    Mode MANUEL: utilise manual_even_gap/manual_odd_gap
    """
    if auto_mode:
        return auto_even_gap, auto_odd_gap
    else:
        return manual_even_gap, manual_odd_gap

def should_predict() -> tuple:
    global initial_analysis_done
    
    if len(games_history) < GAMES_FOR_ANALYSIS:
        return (False, None)
    
    # En mode auto, faire l'analyse si pas encore faite
    if auto_mode and not initial_analysis_done:
        if not calculate_gap_stats_from_window():
            return (False, None)
    
    # En mode manuel, pas besoin d'analyse auto
    if not auto_mode:
        initial_analysis_done = True  # Considère comme "prêt"
    
    active_predictions = [p for p in pending_predictions.values() if p['status'] == '🔮']
    if active_predictions:
        return (False, None)
    
    calculate_current_streaks()
    
    # Utilise les seuils selon le mode
    even_threshold, odd_threshold = get_current_thresholds()
    
    logger.info(f"{'🤖' if auto_mode else '👤'} Mode {'AUTO' if auto_mode else 'MANUEL'} - "
                f"Seuils: P={even_threshold}, I={odd_threshold} | "
                f"Séries: P={current_even_streak}, I={current_odd_streak}")
    
    if current_even_streak >= (even_threshold - 1) and current_even_streak > 0:
        return (True, "IMPAIR")
    
    if current_odd_streak >= (odd_threshold - 1) and current_odd_streak > 0:
        return (True, "PAIR")
    
    return (False, None)

async def send_prediction_to_channels(target_game: int, prediction: str):
    global total_predictions_made
    
    try:
        emoji = "🔵" if prediction == "PAIR" else "🔴"
        prediction_msg = f"🎯 Jeu #{target_game} : {emoji} {prediction}"
        
        message_ids = {}
        
        for channel_id in DYNAMIC_PREDICTION_CHANNELS:
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
        
        even_thr, odd_thr = get_current_thresholds()
        channels_str = ', '.join([str(c) for c in message_ids.keys() if message_ids[c] != 0])
        
        await notify_admin(f"🔮 Nouvelle prédiction: Jeu #{target_game} = {prediction}\n"
                          f"{'🤖' if auto_mode else '👤'} Mode: {'AUTO' if auto_mode else 'MANUEL'}\n"
                          f"📊 Seuils: P={even_thr}/I={odd_thr}\n"
                          f"📡 Canaux: {channels_str}")
        
        return message_ids
        
    except Exception as e:
        logger.error(f"Erreur prédiction: {e}")
        return {}

async def update_prediction_status(game_number: int, new_status: str, won_at_offset: int = None):
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
            await notify_admin(f"✅ Prédiction #{game_number} GAGNÉE {status_text}")
        elif new_status == '❌':
            total_predictions_lost += 1
            del pending_predictions[game_number]
            await notify_admin(f"❌ Prédiction #{game_number} PERDUE")
        
        return True
        
    except Exception as e:
        logger.error(f"Erreur update: {e}")
        return False

async def check_prediction_result(game_number: int, total: int, is_even: bool):
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
    try:
        if ADMIN_ID and ADMIN_ID != 0:
            await client.send_message(ADMIN_ID, f"🤖 *Bot Notification*\n\n{message}", parse_mode='markdown')
    except Exception as e:
        logger.error(f"Erreur notif admin: {e}")

# --- Traitement des Messages ---

async def process_message(message_text: str, chat_id: int, is_edit: bool = False):
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
        
        if status == 'pending':
            pending_finalization[game_number] = {
                'message_text': message_text,
                'received_at': datetime.now()
            }
            return
        
        if status == 'finalized':
            if game_number in pending_finalization:
                del pending_finalization[game_number]
            
            if total is None:
                return
            
            if game_number in games_history and not is_edit:
                return
            
            is_even_result = is_even(total)
            
            if is_even_result:
                total_even_count += 1
            else:
                total_odd_count += 1
            
            games_history[game_number] = {
                'total': total,
                'is_even': is_even_result,
                'timestamp': datetime.now().isoformat()
            }
            
            if len(games_history) > GAMES_FOR_ANALYSIS:
                oldest = min(games_history.keys())
                del games_history[oldest]
            
            # Recalcul auto seulement en mode auto
            if auto_mode and len(games_history) >= GAMES_FOR_ANALYSIS:
                calculate_gap_stats_from_window()
            
            await check_prediction_result(game_number, total, is_even_result)
            
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
        "`/info` - Canaux et config\n"
        "`/channels` - Liste canaux\n"
        "`/addchannel <id>` - Ajouter canal\n"
        "`/removechannel <id>` - Retirer canal\n"
        "`/histo` - Historique\n"
        "`/setmode auto/manual` - Mode\n"
        "`/setgap pair/impair <n>` - Écarts manuels\n"
        "`/stats` - Statistiques\n"
        "`/reset` - Reset"
    )

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    calculate_current_streaks()
    even_thr, odd_thr = get_current_thresholds()
    
    msg = (
        f"📊 **État**\n"
        f"🎮 Dernier: #{last_game_number}\n"
        f"📈 P:{total_even_count} I:{total_odd_count}\n"
        f"🔥 Séries: P={current_even_streak} I={current_odd_streak}\n"
        f"⚙️ Mode: {'🤖 AUTO' if auto_mode else '👤 MANUEL'}\n"
        f"📊 Seuils actifs: P={even_thr} I={odd_thr}\n"
    )
    
    # Afficher les deux types de seuils pour info
    if auto_mode:
        msg += f"📈 (Manuels: P={manual_even_gap} I={manual_odd_gap})\n"
    else:
        msg += f"📈 (Auto: P={auto_even_gap} I={auto_odd_gap})\n"
    
    msg += (f"📡 Canaux: {len(DYNAMIC_PREDICTION_CHANNELS)}\n"
            f"🔮 En cours: {len([p for p in pending_predictions.values() if p['status'] == '🔮'])}\n"
            f"✅ {total_predictions_won} | ❌ {total_predictions_lost}")
    
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/info'))
async def cmd_info(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    even_thr, odd_thr = get_current_thresholds()
    
    channels_str = '\n'.join([f"• `{c}`" for c in DYNAMIC_PREDICTION_CHANNELS])
    
    msg = (
        f"ℹ️ **Configuration**\n"
        f"📡 Source: `{SOURCE_CHANNEL_ID}`\n"
        f"📡 Prédictions ({len(DYNAMIC_PREDICTION_CHANNELS)}):\n{channels_str}\n\n"
        f"⚙️ Mode: {'🤖 AUTO' if auto_mode else '👤 MANUEL'}\n"
        f"📊 Seuils actifs: P={even_thr} I={odd_thr}\n"
        f"🎮 Dernier: `{last_game_number}`\n"
        f"⏳ En attente: {len(pending_finalization)}"
    )
    await event.respond(msg)

@client.on(events.NewMessage(pattern='/channels'))
async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    if not DYNAMIC_PREDICTION_CHANNELS:
        await event.respond("📭 Aucun canal")
        return
    
    lines = [f"📡 **Canaux ({len(DYNAMIC_PREDICTION_CHANNELS)}/20)**\n"]
    
    for i, channel_id in enumerate(DYNAMIC_PREDICTION_CHANNELS, 1):
        status = "❓"
        try:
            entity = await client.get_entity(channel_id)
            status = "✅"
            title = getattr(entity, 'title', 'Inconnu')
            lines.append(f"{i}. `{channel_id}` {status} {title}")
        except:
            lines.append(f"{i}. `{channel_id}` {status} (inaccessible)")
    
    await event.respond("\n".join(lines))

@client.on(events.NewMessage(pattern='/addchannel'))
async def cmd_addchannel(event):
    global DYNAMIC_PREDICTION_CHANNELS
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    parts = event.message.message.split()
    if len(parts) < 2:
        await event.respond("❌ Usage: `/addchannel <id>`")
        return
    
    try:
        new_channel_id = int(parts[1])
        
        if new_channel_id in DYNAMIC_PREDICTION_CHANNELS:
            await event.respond(f"⚠️ Déjà présent")
            return
        
        if len(DYNAMIC_PREDICTION_CHANNELS) >= 20:
            await event.respond(f"❌ Limite 20 atteinte")
            return
        
        try:
            entity = await client.get_entity(new_channel_id)
            title = getattr(entity, 'title', 'Inconnu')
        except Exception as e:
            title = "Inaccessible"
        
        DYNAMIC_PREDICTION_CHANNELS.append(new_channel_id)
        save_dynamic_channels()
        
        await event.respond(
            f"✅ Canal ajouté!\n"
            f"🆔 `{new_channel_id}`\n"
            f"📛 {title}\n"
            f"📊 Total: {len(DYNAMIC_PREDICTION_CHANNELS)}/20"
        )
        
        try:
            await client.send_message(new_channel_id, 
                "🤖 *Bot connecté* - Prédictions automatiques activées",
                parse_mode='markdown')
        except:
            pass
        
    except ValueError:
        await event.respond("❌ ID invalide")
    except Exception as e:
        await event.respond(f"❌ Erreur: {str(e)[:100]}")

@client.on(events.NewMessage(pattern='/removechannel'))
async def cmd_removechannel(event):
    global DYNAMIC_PREDICTION_CHANNELS
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    parts = event.message.message.split()
    if len(parts) < 2:
        await event.respond("❌ Usage: `/removechannel <id>`")
        return
    
    try:
        channel_id_to_remove = int(parts[1])
        
        if channel_id_to_remove not in DYNAMIC_PREDICTION_CHANNELS:
            await event.respond(f"⚠️ Non trouvé")
            return
        
        DYNAMIC_PREDICTION_CHANNELS.remove(channel_id_to_remove)
        save_dynamic_channels()
        
        await event.respond(
            f"✅ Canal retiré!\n"
            f"🆔 `{channel_id_to_remove}`\n"
            f"📊 Total: {len(DYNAMIC_PREDICTION_CHANNELS)}"
        )
        
    except ValueError:
        await event.respond("❌ ID invalide")
    except Exception as e:
        await event.respond(f"❌ Erreur: {str(e)[:100]}")

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
    
    even_max = max(even_gaps) if even_gaps else 0
    odd_max = max(odd_gaps) if odd_gaps else 0
    
    even_thr, odd_thr = get_current_thresholds()
    
    lines.append(f"\n📊 Écarts observés: 🔵{even_max} 🔴{odd_max}")
    lines.append(f"{'🤖' if auto_mode else '👤'} Seuils actifs: 🔵{even_thr} 🔴{odd_thr}")
    
    await event.respond("\n".join(lines))

@client.on(events.NewMessage(pattern='/setmode'))
async def cmd_setmode(event):
    global auto_mode, initial_analysis_done
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    parts = event.message.message.split()
    if len(parts) < 2:
        await event.respond("Usage: `/setmode auto` ou `/setmode manual`")
        return
    
    mode = parts[1].lower()
    
    if mode == 'auto':
        auto_mode = True
        initial_analysis_done = False  # Force recalcul
        calculate_gap_stats_from_window()
        await event.respond(
            f"✅ Mode **AUTO** activé\n"
            f"📊 Écarts calculés: P={auto_even_gap} I={auto_odd_gap}\n"
            f"🤖 Le bot calcule automatiquement les écarts"
        )
        
    elif mode == 'manual':
        auto_mode = False
        initial_analysis_done = True  # Prêt immédiatement
        await event.respond(
            f"✅ Mode **MANUEL** activé\n"
            f"📊 Écarts manuels: P={manual_even_gap} I={manual_odd_gap}\n"
            f"👤 Utilisez `/setgap pair <n>` et `/setgap impair <n>`\n"
            f"❌ Le bot NE calcule PLUS automatiquement les écarts"
        )
    else:
        await event.respond("❌ Mode invalide. Utilisez `auto` ou `manual`")

@client.on(events.NewMessage(pattern='/setgap'))
async def cmd_setgap(event):
    global manual_even_gap, manual_odd_gap
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    parts = event.message.message.split()
    if len(parts) < 3:
        await event.respond("Usage: `/setgap pair <n>` ou `/setgap impair <n>`")
        return
    
    gap_type = parts[1].lower()
    try:
        gap_value = int(parts[2])
        if gap_value < 2 or gap_value > 10:
            await event.respond("❌ Entre 2 et 10")
            return
    except ValueError:
        await event.respond("❌ Nombre invalide")
        return
    
    if gap_type == 'pair':
        manual_even_gap = gap_value
        msg = f"✅ Écart PAIR manuel: **{gap_value}**"
        if auto_mode:
            msg += f"\n⚠️ Mode AUTO actif - Passez en manuel: `/setmode manual`"
        else:
            msg += f"\n👤 Mode MANUEL - Seuil actif: {gap_value - 1} consécutifs"
        await event.respond(msg)
        
    elif gap_type == 'impair':
        manual_odd_gap = gap_value
        msg = f"✅ Écart IMPAIR manuel: **{gap_value}**"
        if auto_mode:
            msg += f"\n⚠️ Mode AUTO actif - Passez en manuel: `/setmode manual`"
        else:
            msg += f"\n👤 Mode MANUEL - Seuil actif: {gap_value - 1} consécutifs"
        await event.respond(msg)
    else:
        await event.respond("❌ Type invalide. Utilisez `pair` ou `impair`")

@client.on(events.NewMessage(pattern='/stats'))
async def cmd_stats(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    win_rate = (total_predictions_won / total_predictions_made * 100) if total_predictions_made > 0 else 0
    
    even_thr, odd_thr = get_current_thresholds()
    
    msg = (
        f"📈 **Statistiques**\n\n"
        f"🎮 Jeux analysés: {len(games_history)}\n"
        f"🔵 Pairs: {total_even_count} | 🔴 Impairs: {total_odd_count}\n\n"
        f"⚙️ Mode: {'🤖 AUTO' if auto_mode else '👤 MANUEL'}\n"
        f"📊 Seuils actifs: P={even_thr} I={odd_thr}\n\n"
        f"🔮 Prédictions:\n"
        f"• Total: {total_predictions_made}\n"
        f"• ✅ Gagnées: {total_predictions_won}\n"
        f"• ❌ Perdues: {total_predictions_lost}\n"
        f"• 📊 Taux: {win_rate:.1f}%"
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
    
    await notify_admin(f"🚨 **RESET EFFECTUÉ**\nRaison: {reason}")
    logger.warning("✅ Reset terminé")

async def check_prediction_timeouts():
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
                await perform_reset(f"Timeout après {PREDICTION_TIMEOUT_MINUTES}min")
                
        except Exception as e:
            logger.error(f"Erreur timeout check: {e}")

async def schedule_daily_reset():
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
    even_thr, odd_thr = get_current_thresholds()
    
    html = f"""<!DOCTYPE html>
    <html>
    <head><title>Bot Prédiction</title>
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
            <p>Mode: {'🤖 AUTO' if auto_mode else '👤 MANUEL'}</p>
            <p>Seuils: P={even_thr} I={odd_thr}</p>
            <p>Canaux: {len(DYNAMIC_PREDICTION_CHANNELS)}</p>
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
    global source_channel_ok, DYNAMIC_PREDICTION_CHANNELS
    
    load_dynamic_channels()
    
    try:
        await client.start(bot_token=BOT_TOKEN)
        source_channel_ok = True
        logger.info(f"✅ Bot connecté | {len(DYNAMIC_PREDICTION_CHANNELS)} canaux")
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
