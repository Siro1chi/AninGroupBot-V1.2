import os
import json
import hashlib
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, MenuButtonWebApp, WebAppInfo
from aiogram.exceptions import TelegramForbiddenError
from aiohttp import web

# ========== Конфигурация ==========
TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("BOT_TOKEN не задан")

PORT = int(os.environ.get('PORT', 8080))
WEBAPP_URL = os.environ.get('WEBAPP_URL', 'https://your-domain.com') 
DATA_FILE = "rooms_data.json"
MAX_NAME_LEN = 30

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== Глобальные структуры ==========
rooms: Dict[str, dict] = {}
user_room: Dict[int, str] = {}
message_mappings: Dict[int, dict] = {} 
lock = asyncio.Lock()

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== FSM ==========
class CreateRoomStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_password = State()

class SetNameStates(StatesGroup):
    waiting_for_name = State()

# ========== Вспомогательные функции ==========
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hash_: str) -> bool:
    return hash_password(password) == hash_

def save_rooms():
    data = {}
    for room_id, room in rooms.items():
        data[room_id] = {
            "name": room["name"],
            "password_hash": room.get("password_hash"),
            "created_by": room.get("created_by")
        }
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Сохранено {len(rooms)} комнат")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

def load_rooms():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        loaded = {}
        for room_id, info in data.items():
            loaded[room_id] = {
                "name": info["name"],
                "password_hash": info.get("password_hash"),
                "members": [],
                "names": {},
                "created_by": info.get("created_by")
            }
        logger.info(f"Загружено {len(loaded)} комнат")
        return loaded
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        return {}

def find_room_by_name(name: str) -> Optional[Tuple[str, dict]]:
    name_lower = name.lower()
    for rid, r in rooms.items():
        if r["name"].lower() == name_lower:
            return rid, r
    return None

async def remove_user_from_members(user_id: int, room_id: str):
    room = rooms.get(room_id)
    if not room or user_id not in room["members"]:
        return
    name = room["names"].get(user_id, "Кто-то")
    room["members"].remove(user_id)
    tasks = [bot.send_message(mid, f"👋 {name} покинул(а) чат") for mid in room["members"]]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
# ========== Команды пользователей ==========
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(text="Комнаты MiniApp", web_app=WebAppInfo(url=WEBAPP_URL))
    )
    await message.answer(
        "👥 Добро пожаловать в AnonGroupBot!\n\n"
        "Вы можете использовать мини-приложение (кнопка слева) или команды:\n\n"
        "/create – создать комнату\n"
        "/rooms – список комнат\n"
        "/join <название> [пароль] – войти\n"
        "/leave – покинуть комнату\n"
        "/setname – установить имя\n"
        "/enter – войти в игровой чат\n"
        "/exit – выйти из игрового чата\n"
        "/users – список участников"
    )

@dp.message(Command("create"))
async def cmd_create(message: Message, state: FSMContext):
    await state.set_state(CreateRoomStates.waiting_for_name)
    await message.answer("Введите название комнаты:")

@dp.message(CreateRoomStates.waiting_for_name)
async def create_room_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    async with lock:
        if find_room_by_name(name):
            await message.answer(f"❌ Комната '{name}' уже существует.")
            return
        await state.update_data(room_name=name)
    await state.set_state(CreateRoomStates.waiting_for_password)
    await message.answer("Введите пароль (или 'нет'):")

@dp.message(CreateRoomStates.waiting_for_password)
async def create_room_password(message: Message, state: FSMContext):
    password_raw = message.text.strip()
    data = await state.get_data()
    room_name = data["room_name"]
    password = None if password_raw.lower() == "нет" else password_raw
    async with lock:
        if find_room_by_name(room_name):
            await message.answer(f"❌ Комната '{room_name}' уже существует.")
            await state.clear()
            return
        room_id = f"room_{len(rooms)+1}_{message.from_user.id}"
        rooms[room_id] = {
            "name": room_name,
            "password_hash": hash_password(password) if password else None,
            "members": [],
            "names": {},
            "created_by": message.from_user.id
        }
        save_rooms()
    await message.answer(f"✅ Создано! Войдите: /join {room_name}")
    await state.clear()

@dp.message(Command("rooms"))
async def cmd_rooms(message: Message):
    async with lock:
        if not rooms:
            await message.answer("Нет комнат. Создайте: /create")
            return
        lines = [f"{'🔒' if r['password_hash'] else '🔓'} {r['name']} (участников: {len(r['members'])})" for r in rooms.values()]
        await message.answer("Доступные комнаты:\n" + "\n".join(lines))

@dp.message(Command("join"))
async def cmd_join(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer("❌ /join Название [пароль]")
        return
    room_name = args[1].strip()
    password = args[2].strip() if len(args) > 2 else None
    user_id = message.from_user.id
    async with lock:
        target = find_room_by_name(room_name)
        if not target:
            await message.answer(f"❌ Комната '{room_name}' не найдена")
            return
        rid, room = target
        if room["password_hash"] and (not password or not verify_password(password, room["password_hash"])):
            await message.answer("❌ Неверный пароль")
            return
        if user_id in user_room and user_room[user_id] != rid:
            await remove_user_from_members(user_id, user_room[user_id])
            del user_room[user_id]
        user_room[user_id] = rid
        await message.answer(f"✅ Ты в комнате '{room_name}'. Установи имя: /setname")

@dp.message(Command("leave"))
async def cmd_leave(message: Message):
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            await message.answer("❌ Ты не в комнате")
            return
        rid = user_room[user_id]
        await remove_user_from_members(user_id, rid)
        del user_room[user_id]
        await message.answer("✅ Ты покинул(а) комнату")

@dp.message(Command("setname"))
async def cmd_setname(message: Message, state: FSMContext):
    async with lock:
        if message.from_user.id not in user_room:
            await message.answer("❌ Сначала /join")
            return
    await state.set_state(SetNameStates.waiting_for_name)
    await message.answer("Введите ваше имя:")

@dp.message(SetNameStates.waiting_for_name)
async def setname_process(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) > MAX_NAME_LEN:
        await message.answer(f"❌ Максимум {MAX_NAME_LEN} символов.")
        return
    if not name:
        await message.answer("Имя не может быть пустым.")
        return
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            await message.answer("❌ Ты не в комнате.")
            await state.clear()
            return
        rid = user_room[user_id]
        rooms[rid]["names"][user_id] = name
    await message.answer(f"Теперь тебя зовут: {name}")
    await state.clear()
# ========== Игровой чат и Модерация ==========
@dp.message(Command("enter"))
async def cmd_enter(message: Message):
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            await message.answer("❌ Сначала /join")
            return
        rid = user_room[user_id]
        room = rooms[rid]
        if user_id not in room["names"]:
            await message.answer("❌ Сначала /setname")
            return
        if user_id in room["members"]:
            await message.answer("Ты уже в чате")
            return
        room["members"].append(user_id)
        name = room["names"][user_id]
        tasks = [bot.send_message(mid, f"🌸 {name} присоединился(ась) к чату") for mid in room["members"] if mid != user_id]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await message.answer("✅ Ты вошёл(а) в игровой чат")

@dp.message(Command("exit"))
async def cmd_exit(message: Message):
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            await message.answer("❌ Ты не в комнате")
            return
        rid = user_room[user_id]
        room = rooms[rid]
        if user_id not in room["members"]:
            await message.answer("Ты не в игровом чате")
            return
        name = room["names"].get(user_id, "Кто-то")
        room["members"].remove(user_id)
        tasks = [bot.send_message(mid, f"👋 {name} покинул(а) чат") for mid in room["members"]]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await message.answer("👋 Ты вышел(ла) из чата")

@dp.message(Command("users"))
async def cmd_users(message: Message):
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            await message.answer("❌ Сначала /join")
            return
        room = rooms[user_room[user_id]]
        if not room["members"]:
            await message.answer("В чате никого нет")
            return
        names = [room["names"].get(uid, "Без имени") for uid in room["members"]]
        await message.answer("👥 Участники чата:\n" + "\n".join(names))

@dp.message(Command("room_info"))
async def cmd_room_info(message: Message):
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            await message.answer("❌ Ты не в комнате")
            return
        room = rooms[user_room[user_id]]
        if room.get("created_by") != user_id:
            await message.answer("❌ Только создатель")
            return
        await message.answer(f"📋 Комната '{room['name']}'\nПароль: {'Да' if room['password_hash'] else 'Нет'}\nВ чате: {len(room['members'])}")

@dp.message(Command("setpassword"))
async def cmd_setpassword(message: Message):
    args = message.text.split(maxsplit=1)
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            await message.answer("❌ Ты не в комнате")
            return
        room = rooms[user_room[user_id]]
        if room.get("created_by") != user_id:
            await message.answer("❌ Только создатель")
            return
        if len(args) < 2 or not args[1].strip():
            room["password_hash"] = None
            await message.answer("🔓 Пароль удалён")
        else:
            room["password_hash"] = hash_password(args[1].strip())
            await message.answer("🔒 Пароль установлен")
        save_rooms()

@dp.message(Command("rename"))
async def cmd_rename(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ /rename НовоеНазвание")
        return
    new_name = args[1].strip()
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            await message.answer("❌ Ты не в комнате")
            return
        room = rooms[user_room[user_id]]
        if room.get("created_by") != user_id:
            await message.answer("❌ Только создатель")
            return
        if find_room_by_name(new_name):
            await message.answer(f"❌ Имя '{new_name}' уже занято")
            return
        old_name = room["name"]
        room["name"] = new_name
        save_rooms()
        await message.answer(f"✅ Переименовано из '{old_name}' в '{new_name}'")

@dp.message(Command("kick"))
async def cmd_kick(message: Message):
    if not message.reply_to_message:
        await message.answer("❌ Ответьте на сообщение того, кого хотите кикнуть")
        return
    target_msg_id = message.reply_to_message.message_id
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            return
        rid = user_room[user_id]
        room = rooms[rid]
        if room.get("created_by") != user_id:
            await message.answer("❌ Только создатель")
            return
        mapping = message_mappings.get(target_msg_id)
        if not mapping:
            await message.answer("❌ Не удалось определить автора сообщения")
            return
        target_id = mapping["author_id"]
        if target_id == user_id:
            await message.answer("❌ Нельзя кикнуть себя")
            return
        if target_id not in room["members"]:
            return
        name = room["names"].get(target_id, "Кто-то")
        room["members"].remove(target_id)
        tasks = [bot.send_message(mid, f"👢 {name} кикнут(а)") for mid in room["members"]]
        tasks.append(bot.send_message(target_id, "❌ Вас кикнули из чата"))
        await asyncio.gather(*tasks, return_exceptions=True)
# ========== Обработка анонимных сообщений (Медиа + Реплаи) ==========
@dp.message(F.text | F.photo | F.sticker | F.voice | F.video | F.video_note | F.animation | F.document | F.audio)
async def handle_chat_message(message: Message):
    user_id = message.from_user.id
    async with lock:
        if user_id not in user_room:
            return
        rid = user_room[user_id]
        room = rooms.get(rid)
        if not room or user_id not in room["members"] or user_id not in room["names"]:
            return
        name = room["names"][user_id]
        members_copy = room["members"].copy()
        alone = len(members_copy) == 1

    if alone:
        await message.answer("⚠️ В комнате никого нет, сообщение никто не увидит")
        return

    reply_prefix = ""
    if message.reply_to_message:
        orig_msg_id = message.reply_to_message.message_id
        if orig_msg_id in message_mappings:
            reply_prefix = f"↪️ <i>В ответ {message_mappings[orig_msg_id]['author_name']}:</i>\n"
        else:
            reply_prefix = f"↪️ <i>В ответ анониму:</i>\n"

    async def send_to_member(mid, msg: Message, author_name: str, prefix: str):
        try:
            sent_msg = None
            if msg.text:
                sent_msg = await bot.send_message(mid, f"🎭 <b>{author_name}</b>:\n{prefix}{msg.text}", parse_mode="HTML")
            elif msg.photo:
                cap = f"🎭 {author_name}\n" + prefix + (msg.caption if msg.caption else "")
                sent_msg = await bot.send_photo(mid, msg.photo[-1].file_id, caption=cap, parse_mode="HTML")
            elif msg.voice:
                cap = f"🎭 {author_name}\n" + prefix + "🗣 [Голосовое сообщение]"
                sent_msg = await bot.send_voice(mid, msg.voice.file_id, caption=cap, parse_mode="HTML")
            elif msg.video:
                cap = f"🎭 {author_name}\n" + prefix + (msg.caption if msg.caption else "")
                sent_msg = await bot.send_video(mid, msg.video.file_id, caption=cap, parse_mode="HTML")
            elif msg.video_note:
                await bot.send_message(mid, f"🎭 <b>{author_name}</b> видеосообщение:\n{prefix}", parse_mode="HTML")
                sent_msg = await bot.send_video_note(mid, msg.video_note.file_id)
            elif msg.document:
                cap = f"🎭 {author_name}\n" + prefix + (msg.caption if msg.caption else "")
                sent_msg = await bot.send_document(mid, msg.document.file_id, caption=cap, parse_mode="HTML")
            elif msg.audio:
                cap = f"🎭 {author_name}\n" + prefix + (msg.caption if msg.caption else "")
                sent_msg = await bot.send_audio(mid, msg.audio.file_id, caption=cap, parse_mode="HTML")
            elif msg.sticker or msg.animation:
                type_name = "стикер" if msg.sticker else "анимацию"
                await bot.send_message(mid, f"🎭 <b>{author_name}</b> отправил(а) {type_name}:\n{prefix}", parse_mode="HTML")
                if msg.sticker:
                    sent_msg = await bot.send_sticker(mid, msg.sticker.file_id)
                else:
                    sent_msg = await bot.send_animation(mid, msg.animation.file_id)
            
            if sent_msg:
                message_mappings[sent_msg.message_id] = {"author_id": user_id, "author_name": author_name}
        except Exception as e:
            logger.error(f"Ошибка пересылки для {mid}: {e}")

    tasks = [send_to_member(mid, message, name, reply_prefix) for mid in members_copy if mid != user_id]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

# ========== Веб-сервер API и Запуск ==========
async def health_check(request):
    return web.Response(text="OK")

async def get_rooms_api(request):
    async with lock:
        data = []
        for rid, r in rooms.items():
            data.append({"id": rid, "name": r["name"], "has_password": r["password_hash"] is not None, "members_count": len(r["members"])})
    return web.json_response(data)

# НОВЫЙ ЭНДПОИНТ: Обработка входа через веб-интерфейс Mini App
async def join_room_api(request):
    try:
        body = await request.json()
        user_id = int(body.get("user_id"))
        room_name = body.get("room_name")
        
        async with lock:
            target = find_room_by_name(room_name)
            if not target:
                return web.json_response({"success": False, "error": f"Комната '{room_name}' не найдена"})
            
            rid, room = target
            # Если у комнаты есть пароль, через простой список пускать нельзя (нужно слать в чат)
            if room["password_hash"]:
                try:
                    await bot.send_message(user_id, f"🔒 Комната '{room_name}' защищена паролем. Войдите через команду чата:\n`/join {room_name} <пароль>`", parse_mode="Markdown")
                except Exception:
                    pass
                return web.json_response({"success": False, "error": "Эта комната под паролем. Инструкция отправлена в бот."})

            if user_id in user_room and user_room[user_id] != rid:
                await remove_user_from_members(user_id, user_room[user_id])
                del user_room[user_id]
                
            user_room[user_id] = rid
            
        try:
            await bot.send_message(user_id, f"✅ Вы успешно вошли в комнату '{room_name}' через Mini App!\nУстановите имя: /setname")
        except Exception:
            pass
            
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})

async def webapp_page(request):
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
        return web.Response(text=html, content_type='text/html')
    except Exception as e:
        return web.Response(text=f"Ошибка: {e}", status=500)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_get('/webapp', webapp_page)
    app.router.add_get('/api/get_rooms', get_rooms_api)
    app.router.add_post('/api/join_room', join_room_api) # Регистрируем POST эндпоинт
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Сервер развернут на порту {PORT}")

async def clean_empty_rooms():
    while True:
        await asyncio.sleep(3600)
        async with lock:
            to_delete = [rid for rid, room in rooms.items() if not room["members"]]
            for rid in to_delete:
                del rooms[rid]
            if to_delete:
                save_rooms()

async def main():
    global rooms
    rooms = load_rooms()
    await start_web_server()
    asyncio.create_task(clean_empty_rooms())
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
