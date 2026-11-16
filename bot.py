"""Личный бот для пары: внимание, послания на потом и котик по желанию."""

import asyncio
import fcntl
import logging
import os
import random
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from envelope_store import EnvelopeStore
from envelopes import ENVELOPE_BUTTON, EnvelopeFlow
from attention import ATTENTION_BUTTON, AttentionFlow

from content_store import (
    PHOTO_EXTENSIONS, choose_fresh, commit_delivery, get_active_view,
    list_media, set_active_view,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')
token = os.getenv('BOT_TOKEN')
try:
    allowed_user_ids = {int(v.strip()) for v in os.getenv('ALLOWED_USER_IDS', '').split(',') if v.strip()}
except ValueError as error:
    raise RuntimeError('В ALLOWED_USER_IDS должны быть числовые Telegram ID через запятую') from error
if not token:
    raise RuntimeError('Токен не найден в файле .env')

dp = Dispatcher()
chat_locks: dict[int, asyncio.Lock] = {}
CAT_BUTTON = 'Нужен котик'
FINAL_CAT_CAPTIONS = [
    'Шпасибо 🙇🏻‍♀️ а это вам на удачу 🍀',
    'Шпасибо 🙇🏻‍♀️ а это вам на счастье 🍀',
    'Шпасибо 🙇🏻‍♀️ а это вам на благополучие 🍀',
    'Шпасибо 🙇🏻‍♀️ а это вам на успех 🍀',
]
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=ATTENTION_BUTTON)], [KeyboardButton(text=ENVELOPE_BUTTON)], [KeyboardButton(text=CAT_BUTTON)]], resize_keyboard=True,
    is_persistent=True,
)
envelopes = EnvelopeFlow(EnvelopeStore(BASE_DIR / 'data' / 'envelopes.sqlite3'), allowed_user_ids, main_keyboard)
attention = AttentionFlow(envelopes)


def cat_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Ещё 🐾', callback_data='cat:more'),
        InlineKeyboardButton(text='Усё 💛', callback_data='cat:finish'),
    ]])


def allowed(message: Message, user_id: int | None) -> bool:
    return message.chat.type == 'private' and user_id in allowed_user_ids


async def clear_old_buttons(bot: Bot, chat_id: int, old_view: dict | None) -> None:
    if old_view is None or old_view['stage'] == 'finished':
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=old_view['message_id'], reply_markup=None)
    except (TelegramBadRequest, TelegramNetworkError):
        # Старое сообщение могло быть удалено пользователем.
        logging.info('Не удалось убрать старые кнопки: сообщение недоступно или уже без кнопок')


async def open_cats(message: Message, bot: Bot) -> None:
    async with chat_locks.setdefault(message.chat.id, asyncio.Lock()):
        await send_cat(bot, message.chat.id)


async def send_cat(bot: Bot, chat_id: int, *, more: bool = False, final: bool = False) -> bool:
    # Вызывается внутри общего замка чата: быстрые нажатия не выбирают одно и то же.
    files = list_media(extensions=PHOTO_EXTENSIONS if final else None)
    groups = {
        'cat_media': [v for v in files if Path(v).suffix.lower() in PHOTO_EXTENSIONS],
        'cat_videos': [v for v in files if Path(v).suffix.lower() == '.mp4'],
        'cat_gifs': [v for v in files if Path(v).suffix.lower() == '.gif'],
    }
    groups = {kind: values for kind, values in groups.items() if values}
    # Вес равен количеству файлов, поэтому типы не получают искусственную квоту 50/50.
    # У фото и видео отдельные круги: финальное фото не сбрасывает историю видео.
    kind = random.choices(list(groups), weights=[len(v) for v in groups.values()])[0] if groups else 'cat_media'
    media = choose_fresh(chat_id, kind, groups.get(kind, []))
    if media is None:
        await bot.send_message(chat_id, 'Котик пока не загрузился. Попробуй чуть позже 🐾')
        return False
    records = [(kind, media)]
    if final:
        caption = choose_fresh(chat_id, 'final_cat_captions', FINAL_CAT_CAPTIONS) or FINAL_CAT_CAPTIONS[0]
        records.append(('final_cat_captions', caption))
        markup = None
    else:
        caption = 'Воть ещё' if more else 'Воть'
        markup = cat_keyboard()
    old = get_active_view(chat_id)
    file = FSInputFile(BASE_DIR / media)
    if Path(media).suffix.lower() == '.mp4':
        sent = await bot.send_video(chat_id, file, caption=caption, reply_markup=markup, supports_streaming=True)
    elif Path(media).suffix.lower() == '.gif':
        sent = await bot.send_animation(chat_id, file, caption=caption, reply_markup=markup)
    else:
        sent = await bot.send_photo(chat_id, file, caption=caption, reply_markup=markup)
    # История медиа и активные кнопки записываются только после успешной отправки.
    commit_delivery(chat_id, records, sent.message_id, None, 'finished' if final else 'cat_content')
    await clear_old_buttons(bot, chat_id, old)
    return True


@dp.message(CommandStart())
async def start(message: Message, bot: Bot) -> None:
    if message.chat.type != 'private':
        return
    payload = (message.text or '').partition(' ')[2].strip()
    if payload.startswith('gift_') and await envelopes.accept_invite(message, bot, payload):
        return
    async with chat_locks.setdefault(message.chat.id, asyncio.Lock()):
        old = get_active_view(message.chat.id)
        envelopes.store.pause(message.chat.id)
        attention.store.pause_recording(message.chat.id)
        if await envelopes.resume_gift_welcome(message, bot):
            return
        if message.from_user and allowed(message, message.from_user.id):
            envelopes.store.remember_user(message.from_user)
        sent = await message.answer('Стать чуть ближе, оставить послание на потом или заглянуть к котику 💛', reply_markup=main_keyboard)
        set_active_view(message.chat.id, sent.message_id, None, 'finished')
        await clear_old_buttons(bot, message.chat.id, old)


@dp.message(Command('myid'))
async def show_my_id(message: Message) -> None:
    if message.chat.type == 'private' and message.from_user is not None:
        await message.answer(f'Твой Telegram ID: {message.from_user.id}')


@dp.message()
async def main_menu(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not allowed(message, user_id):
        if message.chat.type == 'private':
            await message.answer('Это личный бот. Попроси владелицу добавить твой ID из команды /myid.')
        return
    if await attention.handle_message(message, bot):
        return
    if await envelopes.handle_message(message, bot):
        return
    if message.text in {CAT_BUTTON, '🐱 Нужен котик'}:
        await open_cats(message, bot)
    else:
        await message.answer('Что хочется сделать? 💛', reply_markup=main_keyboard)


@dp.callback_query()
async def handle_choice(callback: CallbackQuery, bot: Bot) -> None:
    if not isinstance(callback.message, Message) or not callback.data:
        await callback.answer()
        return
    if not allowed(callback.message, callback.from_user.id):
        await callback.answer('Это личный бот.', show_alert=True)
        return
    if await attention.handle_callback(callback, bot):
        return
    if await envelopes.handle_callback(callback, bot):
        return
    parts = callback.data.split(':')
    if len(parts) != 2 or parts[0] != 'cat' or parts[1] not in {'more', 'finish'}:
        await callback.answer('Меню обновилось. Нажми «Нужен котик».', show_alert=True)
        return
    action = parts[1]
    chat_id = callback.message.chat.id
    async with chat_locks.setdefault(chat_id, asyncio.Lock()):
        view = get_active_view(chat_id)
        if (view is None or view['message_id'] != callback.message.message_id
            or view['stage'] != 'cat_content'):
            await callback.answer('Эта кнопка уже использована. Выбери последнюю кнопку в чате.', show_alert=True)
            return
        await callback.answer()
        envelopes.store.pause(chat_id)
        await send_cat(bot, chat_id, more=action == 'more', final=action == 'finish')


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not allowed_user_ids:
        raise RuntimeError('Укажи ALLOWED_USER_IDS в .env для доступа к личным фотографиям')
    async with Bot(token=token, session=AiohttpSession(timeout=180)) as bot:
        worker = asyncio.create_task(envelopes.worker(bot))
        try:
            await dp.start_polling(bot)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


if __name__ == '__main__':
    # Prevent two copies from competing for Telegram updates.
    with (BASE_DIR / '.bot-instance.lock').open('a') as instance_lock:
        try:
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Бот уже запущен. Используй работающий экземпляр.')
        asyncio.run(main())
