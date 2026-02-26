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
prediction_channel_ok = False

PREDICTION_WINDOW = 3
PREDICTION_TIMEOUT_MINUTES = 20

# Flag pour savoir si l'analyse initiale est faite
initial_analysis_done = False
games_before_analysis = 20

# --- Fonctions d'Analyse ---

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message (format #N suivi de chiffres)."""
    # Supporte #N1074 ou #N 1074
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def extract_total_value(message: str):
    """Extrait la valeur totale (#T) du message - cherche avant #R ou fin de ligne."""
    # Cherche #T suivi de chiffres (avant #R ou autre)
    match = re.search(r"#T\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # Cherche Total avant #R
    match = re.search(r"[Tt]otal[\s:]*(\d+)", message)
    if match:
        return int(match.group(1))
    return None

def is_even(number: int) -> bool:
    return number % 2 == 0

def is_message_finalized(message: str) -> bool:
    """Vérifie si le message est finalisé (✅ ou 🔰 présent)."""
    return '✅' in message or '🔰' in message

def is_message_pending(message: str) -> bool:
    """Vérifie si le message est en cours (⏰ ou ▶️ présent)."""
    return '⏰' in message or '▶️' in message

def get_message_status(message: str) -> str:
    if is_message_finalized(message):
        return 'finalized'
    elif is_message_pending(message):
        return 'pending'
    return 'unknown'

# --- Logique de Calcul des Écarts ---

def calculate_gap_stats():
    """Calcule les écarts max entre numéros pairs et impairs depuis l'historique."""
    global auto_even_gap, auto_odd_gap, initial_analysis_done
    
    if len(games_history) < 5:
        return
    
    sorted_games = sorted(games_history.items(), key=lambda x: x[0])
    
    even_gaps = []
    odd_gaps = []
    
    last_even_game = None
    last_odd_game = None
    
    for game_num, game_data in sorted_games:
        if game_data['is_even']:
            if last_even_game is not None:
                gap = game_num - last_even_game
                even_gaps.append(gap)
            last_even_game = game_num
        else:
            if last_odd_game is not None:
                gap = game_num - last_odd_game
                odd_gaps.append(gap)
            last_odd_game = game_num
    
    # Calculer les écarts max (max réel, pas percentile)
    if even_gaps:
        auto_even_gap = max(even_gaps)
        auto_even_gap = max(2, min(auto_even_gap, 8))
    
    if odd_gaps:
        auto_odd_gap = max(odd_gaps)
        auto_odd_gap = max(2, min(auto_odd_gap, 8))
    
    initial_analysis_done = True
    logger.info(f"📊 Stats calculées - Écart Pair max: {auto_even_gap}, Écart Impair max: {auto_odd_gap}")

def calculate_streaks():
    """Calcule les séries actuelles de pairs et impairs."""
    global current_even_streak, current_odd_streak
    
    if not games_history:
        return
    
    # Trier par numéro de jeu
    sorted_games = sorted(games_history.items(), key=lambda x: x[0])
    
    # Trouver la dernière série
    current_even_streak = 0
    current_odd_streak = 0
    
    # Parcourir à l'envers pour trouver la série en cours
    for game_num, game_data in reversed(sorted_games):
        is_even_result = game_data['is_even']
        
        if current_even_streak == 0 and current_odd_streak == 0:
            # Premier jeu (le plus récent)
            if is_even_result:
                current_even_streak = 1
            else:
                current_odd_streak = 1
        else:
            # Vérifier si on continue la série
            if is_even_result and current_even_streak > 0:
                current_even_streak += 1
            elif not is_even_result and current_odd_streak > 0:
                current_odd_streak += 1
            else:
                # Série interrompue
                break
    
    logger.info(f"📈 Séries calculées - Pairs: {current_even_streak}, Impairs: {current_odd_streak}")

# --- Logique de Prédiction ---

def should_predict() -> tuple:
    """
    Détermine si une prédiction doit être faite.
    Ne prédit que si l'analyse initiale est faite et pas de prédiction active.
    """
    global initial_analysis_done
    
    # Vérifier si assez de jeux pour l'analyse initiale
    if len(games_history) < games_before_analysis:
        logger.info(f"⏳ Analyse initiale en cours... ({len(games_history)}/{games_before_analysis} jeux)")
        return (False, None)
    
    # Faire l'analyse initiale si pas encore faite
    if not initial_analysis_done:
        calculate_gap_stats()
        calculate_streaks()
    
    # Vérifier si prédiction en cours
    active_predictions = [p for p in pending_predictions.values() if p['status'] == '🔮']
    if active_predictions:
        logger.info(f"⏳ Prédiction active en cours, attente de résultat...")
        return (False, None)
    
    even_threshold = auto_even_gap if auto_mode else max_even_gap
    odd_threshold = auto_odd_gap if auto_mode else max_odd_gap
    
    # Recalculer les séries avant de décider
    calculate_streaks()
    
    if current_even_streak >= even_threshold:
        return (True, "IMPAIR")
    
    if current_odd_streak >= odd_threshold:
        return (True, "PAIR")
    
    return (False, None)

async def send_prediction_to_channel(target_game: int, prediction: str):
    """Envoie la prédiction au canal de prédiction."""
    global total_predictions_made
    
    try:
        emoji = "🔵" if prediction == "PAIR" else "🔴"
        prediction_msg = f"🎯 Jeu #{target_game} : {emoji} {prediction}"
        
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
            'created_at': datetime.now().isoformat(),
            'check_count': 0,
            'last_check': datetime.now(),
            'checked_games': []
        }
        
        total_predictions_made += 1
        return msg_id
        
    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str, won_at_offset: int = None):
    """Met à jour le statut d'une prédiction."""
    global total_predictions_won, total_predictions_lost
    
    try:
        if game_number not in pending_predictions:
            return False
        
        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        prediction = pred['prediction']
        
        emoji = "🔵" if prediction == "PAIR" else "🔴"
        
        # Format simple et joli
        if new_status.startswith('✅') and won_at_offset is not None:
            offset_emoji = ['0️⃣', '1️⃣', '2️⃣'][won_at_offset] if won_at_offset < 3 else f"+{won_at_offset}"
            status_text = f"✅{offset_emoji}"
        elif new_status == '❌':
            status_text = "❌"
        else:
            status_text = new_status
        
        updated_msg = f"🎯 Jeu #{game_number} : {emoji} {prediction}\n{status_text}"
        
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {status_text}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")
        
        pred['status'] = status_text
        
        if new_status.startswith('✅'):
            total_predictions_won += 1
            del pending_predictions[game_number]
        elif new_status == '❌':
            total_predictions_lost += 1
            del pending_predictions[game_number]
        
        return True
        
    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

async def check_prediction_result(game_number: int, total: int, is_even: bool):
    """Vérifie si une prédiction active correspond au résultat."""
    for pred_game_num, pred_data in list(pending_predictions.items()):
        if pred_data['status'] != '🔮':
            continue
            
        offset = game_number - pred_game_num
        
        # Vérifier si ce jeu est dans la fenêtre de prédiction (0, 1, ou 2)
        if 0 <= offset < PREDICTION_WINDOW:
            # Vérifier si ce jeu n'a pas déjà été vérifié
            if game_number in pred_data.get('checked_games', []):
                return
                
            predicted_type = pred_data['prediction']
            is_correct = (predicted_type == "PAIR" and is_even) or (predicted_type == "IMPAIR" and not is_even)
            
            # Marquer ce jeu comme vérifié
            if 'checked_games' not in pred_data:
                pred_data['checked_games'] = []
            pred_data['checked_games'].append(game_number)
            pred_data['check_count'] = len(pred_data['checked_games'])
            pred_data['last_check'] = datetime.now()
            
            if is_correct:
                await update_prediction_status(pred_game_num, '✅ GAGNÉ', offset)
                logger.info(f"🎉 Prédiction #{pred_game_num} GAGNÉE au décalage {offset} (jeu #{game_number})")
                return
            
            else:
                # Vérifier si on a atteint la fin de la fenêtre
                if pred_data['check_count'] >= PREDICTION_WINDOW:
                    await update_prediction_status(pred_game_num, '❌', None)
                    logger.info(f"😞 Prédiction #{pred_game_num} PERDUE après vérification sur {pred_data['checked_games']}")
                
                else:
                    remaining = PREDICTION_WINDOW - pred_data['check_count']
                    logger.info(f"⏳ Prédiction #{pred_game_num}: jeu #{game_number} incorrect ({pred_data['check_count']}/{PREDICTION_WINDOW})")

async def check_prediction_timeouts():
    """Vérifie les prédictions en timeout."""
    while True:
        try:
            await asyncio.sleep(60)
            
            now = datetime.now()
            expired_predictions = []
            
            for game_num, pred_data in pending_predictions.items():
                if pred_data['status'] != '🔮':
                    continue
                    
                created_at = datetime.fromisoformat(pred_data['created_at'])
                if now - created_at > timedelta(minutes=PREDICTION_TIMEOUT_MINUTES):
                    expired_predictions.append(game_num)
            
            if expired_predictions:
                logger.warning(f"🚨 {len(expired_predictions)} prédiction(s) expirée(s)!")
                for game_num in expired_predictions:
                    if game_num in pending_predictions:
                        del pending_predictions[game_num]
                logger.warning("🧹 Prédictions expirées effacées")
                
        except Exception as e:
            logger.error(f"Erreur vérification timeout: {e}")

# --- Traitement des Messages ---

async def process_message(message_text: str, chat_id: int, is_edit: bool = False):
    """Traite un message du canal source."""
    global last_game_number, last_total, current_even_streak, current_odd_streak
    global total_even_count, total_odd_count, initial_analysis_done
    
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        
        total = extract_total_value(message_text)
        status = get_message_status(message_text)
        
        logger.info(f"📨 Message reçu - Jeu #{game_number}, Status: {status}, Total: {total}")
        
        # Si message en attente (⏰ ou ▶️), stocker et attendre
        if status == 'pending':
            pending_finalization[game_number] = {
                'message_text': message_text,
                'received_at': datetime.now()
            }
            logger.info(f"⏳ Jeu #{game_number} en attente de finalisation (⏰/▶️ détecté)")
            return
        
        # Si message finalisé (✅ ou 🔰)
        if status == 'finalized':
            # Vérifier si on avait ce jeu en attente
            if game_number in pending_finalization:
                del pending_finalization[game_number]
                logger.info(f"✅ Jeu #{game_number} finalisé (était en attente)")
            
            if total is None:
                logger.warning(f"⚠️ Total non trouvé dans: {message_text[:100]}")
                return
            
            # Vérifier si déjà traité (sauf si édition)
            if game_number in games_history and not is_edit:
                logger.info(f"🔄 Jeu #{game_number} déjà traité")
                return
            
            is_even_result = is_even(total)
            
            logger.info(f"🎮 Jeu #{game_number} - Total: {total} ({'PAIR' if is_even_result else 'IMPAIR'})")
            
            # Mettre à jour les compteurs
            if is_even_result:
                total_even_count += 1
            else:
                total_odd_count += 1
            
            # Stocker dans l'historique
            games_history[game_number] = {
                'total': total,
                'is_even': is_even_result,
                'timestamp': datetime.now().isoformat(),
                'status': 'finalized'
            }
            
            # Limiter l'historique
            if len(games_history) > MAX_HISTORY_SIZE:
                oldest = min(games_history.keys())
                del games_history[oldest]
            
            # Recalculer les séries avec le nouveau jeu
            calculate_streaks()
            
            # Vérifier les prédictions existantes
            await check_prediction_result(game_number, total, is_even_result)
            
            # Analyse auto tous les 20 jeux
            if len(games_history) % DEFAULT_AUTO_CHECK_INTERVAL == 0 and auto_mode:
                calculate_gap_stats()
                logger.info(f"🔄 Recalcul auto des écarts ({len(games_history)} jeux)")
            
            # Vérifier si on doit prédire (seulement si analyse initiale faite)
            should_pred, prediction_type = should_predict()
            
            if should_pred and prediction_type:
                # PRÉDICTION SUR +2 au lieu de +1
                target_game = game_number + 2
                if target_game not in pending_predictions:
                    await send_prediction_to_channel(target_game, prediction_type)
                    logger.info(f"🔮 Prédiction: Jeu #{target_game} = {prediction_type} "
                               f"(série P:{current_even_streak}/I:{current_odd_streak})")
            
            last_game_number = game_number
            last_total = total
            
    except Exception as e:
        logger.error(f"Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())

# --- Gestion des Messages ---

@client.on(events.NewMessage())
async def handle_message(event):
    """Gère les nouveaux messages."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            await process_message(message_text, chat_id, is_edit=False)
    
    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    """Gère les messages édités."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id
        
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"✏️ Message édité détecté")
            await process_message(message_text, chat_id, is_edit=True)
    
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

# --- Commandes Administrateur ---

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond(
        "🤖 **Bot de Prédiction Pair/Impair**\n\n"
        "Commandes:\n"
        "`/status` - État du bot\n"
        "`/info` - Canaux et dernier numéro\n"
        "`/histo` - Historique 20 jeux\n"
        "`/setmode auto/manual` - Mode\n"
        "`/setgap pair/impair <n>` - Écarts\n"
        "`/stats` - Statistiques\n"
        "`/help` - Aide"
    )

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    mode_str = "🤖 Auto" if auto_mode else "👤 Manuel"
    even_gap = auto_even_gap if auto_mode else max_even_gap
    odd_gap = auto_odd_gap if auto_mode else max_odd_gap
    
    status_msg = (
        f"📊 **État**\n"
        f"🎮 Dernier: #{last_game_number} | Total: {last_total}\n"
        f"📈 Pairs: {total_even_count} | Impairs: {total_odd_count}\n"
        f"🔥 Séries: P={current_even_streak} | I={current_odd_streak}\n"
        f"⚙️ Mode: {mode_str} | Écarts: P={even_gap}/I={odd_gap}\n"
        f"🔮 Actives: {len([p for p in pending_predictions.values() if p['status'] == '🔮'])}\n"
        f"✅ Gagnées: {total_predictions_won} | ❌ Perdues: {total_predictions_lost}"
    )
    
    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/info'))
async def cmd_info(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    info_msg = (
        f"ℹ️ **Configuration**\n\n"
        f"📡 Source: `{SOURCE_CHANNEL_ID}`\n"
        f"📡 Prédiction: `{PREDICTION_CHANNEL_ID}`\n"
        f"🎮 Dernier numéro source: `{last_game_number}`\n"
        f"⏳ En attente: {len(pending_finalization)}\n"
        f"🔮 Prédictions actives: {len([p for p in pending_predictions.values() if p['status'] == '🔮'])}"
    )
    
    await event.respond(info_msg)

@client.on(events.NewMessage(pattern='/histo'))
async def cmd_histo(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    if not games_history:
        await event.respond("📭 Vide")
        return
    
    # 20 derniers jeux
    sorted_games = sorted(games_history.items(), key=lambda x: x[0], reverse=True)[:20]
    sorted_games.reverse()
    
    lines = ["📜 **20 derniers jeux**\n"]
    
    for game_num, game_data in sorted_games:
        emoji = "🔵" if game_data['is_even'] else "🔴"
        lines.append(f"#{game_num}: {game_data['total']} {emoji}")
    
    # Calcul correct des écarts
    even_positions = []
    odd_positions = []
    
    for i, (game_num, game_data) in enumerate(sorted_games):
        if game_data['is_even']:
            even_positions.append(i)
        else:
            odd_positions.append(i)
    
    # Calculer écarts entre positions consécutives
    even_gaps = []
    for i in range(1, len(even_positions)):
        even_gaps.append(even_positions[i] - even_positions[i-1])
    
    odd_gaps = []
    for i in range(1, len(odd_positions)):
        odd_gaps.append(odd_positions[i] - odd_positions[i-1])
    
    even_max = max(even_gaps) if even_gaps else 0
    odd_max = max(odd_gaps) if odd_gaps else 0
    
    lines.append(f"\n📊 **Écarts max:**")
    lines.append(f"🔵 PAIR: {even_max} | 🔴 IMPAIR: {odd_max}")
    
    if auto_mode:
        lines.append(f"\n🤖 Seuils: P={auto_even_gap}/I={auto_odd_gap}")
    
    await event.respond("\n".join(lines))

@client.on(events.NewMessage(pattern='/setmode'))
async def cmd_setmode(event):
    global auto_mode
    
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
        calculate_gap_stats()
        await event.respond(f"✅ Auto | Écarts: P={auto_even_gap}/I={auto_odd_gap}")
    elif mode == 'manual':
        auto_mode = False
        await event.respond(f"✅ Manuel | Écarts: P={max_even_gap}/I={max_odd_gap}")
    else:
        await event.respond("❌ auto ou manual")

@client.on(events.NewMessage(pattern='/setgap'))
async def cmd_setgap(event):
    global max_even_gap, max_odd_gap
    
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    parts = event.message.message.split()
    if len(parts) < 3:
        await event.respond("Usage: `/setgap pair 3` ou `/setgap impair 3`")
        return
    
    gap_type = parts[1].lower()
    try:
        val = int(parts[2])
        if val < 2 or val > 10:
            await event.respond("Entre 2 et 10")
            return
    except ValueError:
        await event.respond("Nombre invalide")
        return
    
    if gap_type == 'pair':
        max_even_gap = val
        await event.respond(f"✅ Écart PAIR: {val}")
    elif gap_type == 'impair':
        max_odd_gap = val
        await event.respond(f"✅ Écart IMPAIR: {val}")
    else:
        await event.respond("❌ pair ou impair")

@client.on(events.NewMessage(pattern='/stats'))
async def cmd_stats(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("⛔ Admin uniquement")
        return
    
    win_rate = (total_predictions_won / total_predictions_made * 100) if total_predictions_made > 0 else 0
    
    stats_msg = (
        f"📈 **Stats**\n"
        f"Jeux: {len(games_history)}\n"
        f"🔵 Pairs: {total_even_count} | 🔴 Impairs: {total_odd_count}\n"
        f"🔮 Total: {total_predictions_made}\n"
        f"✅ Gagnées: {total_predictions_won}\n"
        f"❌ Perdues: {total_predictions_lost}\n"
        f"📊 Taux: {win_rate:.1f}%"
    )
    
    await event.respond(stats_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    
    help_msg = (
        "📖 **Aide**\n\n"
        "**Fonctionnement:**\n"
        "Analyse 20 jeux → calcule écarts → prédit sur +2\n"
        "Vérification: 0️⃣=prédit, 1️⃣=+1, 2️⃣=+2, ❌=perdu\n\n"
        "**Commandes:**\n"
        "`/status` `/info` `/histo`\n"
        "`/setmode auto/manual`\n"
        "`/setgap pair/impair <n>`\n"
        "`/stats` `/help`"
    )
    
    await event.respond(help_msg)

# --- Serveur Web ---

async def index(request):
    html = f"""<!DOCTYPE html>
    <html>
    <head><title>Bot Prédiction</title>
    <style>
        body {{ font-family: Arial; max-width: 600px; margin: 50px auto; padding: 20px; }}
        .status {{ background: #f0f0f0; padding: 15px; border-radius: 8px; }}
    </style>
    </head>
    <body>
        <h1>🎯 Bot Prédiction</h1>
        <div class="status">
            <p>Statut: ✅ En ligne</p>
            <p>Dernier: #{last_game_number}</p>
            <p>Mode: {'Auto' if auto_mode else 'Manuel'}</p>
            <p>Prédictions: {len([p for p in pending_predictions.values() if p['status'] == '🔮'])}</p>
        </div>
    </body>
    </html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Web serveur port {PORT}")

async def schedule_daily_reset():
    wat_tz = timezone(timedelta(hours=1))
    reset_time = time(0, 59, tzinfo=wat_tz)
    
    logger.info(f"🕐 Reset quotidien: {reset_time} WAT")
    
    while True:
        now = datetime.now(wat_tz)
        target = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target:
            target += timedelta(days=1)
        
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)
        
        logger.warning("🚨 RESET!")
        
        global games_history, pending_predictions, pending_finalization
        global current_even_streak, current_odd_streak, initial_analysis_done
        global total_even_count, total_odd_count, total_predictions_made
        global total_predictions_won, total_predictions_lost, last_game_number, last_total
        
        games_history.clear()
        pending_predictions.clear()
        pending_finalization.clear()
        current_even_streak = current_odd_streak = 0
        total_even_count = total_odd_count = 0
        total_predictions_made = total_predictions_won = total_predictions_lost = 0
        last_game_number = last_total = 0
        initial_analysis_done = False
        
        logger.warning("✅ Reset terminé")

async def start_bot():
    global source_channel_ok, prediction_channel_ok
    try:
        await client.start(bot_token=BOT_TOKEN)
        source_channel_ok = prediction_channel_ok = True
        logger.info("✅ Bot connecté")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur connexion: {e}")
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
        logger.error(f"Erreur main: {e}")
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
