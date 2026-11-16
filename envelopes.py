"""Native Telegram recordings and time-locked personal envelopes."""

import asyncio
import logging
import time
import re
import uuid
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ENVELOPE_BUTTON = 'Откроешь потом 💌'
GIFT_JOIN_NOTICE = 'Он уже туть 🤍 Твоя половинка подключилась'
GIFT_GREETING = (
    'Хочу, чтобы ты улыбнулся, когда увидишь, что я для тебя приготовила 🤍\n\n'
    'Я приготовила для тебя маленький сюрприз — наш уголок, куда можно заглянуть, когда хочется друг к другу.\n\n'
    'Здесь можно попросить внимания, оставить послание на потом или получить порцию нашего котика 🐾\n\n'
    'У нас бывает разное время на часах, но мне всегда хочется находить время для нас.\n\n'
    'Обнимаю тебя, котёнок 🫂'
)
KINDS = {'text': 'Текст 💬', 'photo': 'Фото 📷', 'voice': 'Голосовое 🎙️', 'circle': 'Кружочек 🎥'}
ZONES = {'astana': 'Asia/Almaty', 'calgary': 'America/Edmonton'}
TITLES = ['Когда проснёшься ☀️', 'После работы 🌙', 'Когда соскучишься 💛']
RETAKE_LABELS = {'circle': 'Переснять 🎥', 'voice': 'Перезаписать 🎙️', 'photo': 'Заменить фото 📷'}


def caption_fits(text):
    return len(text.encode('utf-16-le')) // 2 <= 1000


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


def display_time(timestamp):
    return '\n'.join(f'{label}: {datetime.fromtimestamp(timestamp, ZoneInfo(zone)):%d.%m.%Y в %H:%M}'
                     for label, zone in [('Астана', ZONES['astana']), ('Калгари', ZONES['calgary'])])


def parse_local_time(text, zone):
    """Reject local times skipped/repeated at the daylight saving transition."""
    wall = datetime.strptime(text.strip(), '%d.%m.%Y %H:%M')
    tz = ZoneInfo(zone)
    candidates = set()
    for fold in (0, 1):
        instant = wall.replace(tzinfo=tz, fold=fold).timestamp()
        if datetime.fromtimestamp(instant, tz).replace(tzinfo=None) == wall:
            candidates.add(instant)
    if len(candidates) != 1:
        raise ValueError('Ambiguous or nonexistent local time')
    return candidates.pop()


class EnvelopeFlow:
    def __init__(self, store, allowed_ids, main_keyboard, *, clock=time.time):
        self.store = store
        self.owner_id = next(iter(allowed_ids)) if len(allowed_ids) == 1 else None
        self.allowed_ids = allowed_ids
        paired = self.store.invited_partner(self.owner_id) if self.owner_id is not None else None
        if paired is not None:
            self.allowed_ids.add(paired)
        self.main_keyboard = main_keyboard
        self.clock = clock
        self.locks = {}
        self.envelope_locks = {}
        self.welcome_locks = {}

    def partner(self, user_id):
        if user_id not in self.allowed_ids or len(self.allowed_ids) != 2:
            return None
        return next(iter(self.allowed_ids - {user_id}))

    def permitted(self, message, user_id):
        return message.chat.type == 'private' and message.chat.id == user_id and user_id in self.allowed_ids

    async def accept_invite(self, message, bot, payload):
        user_id = message.from_user.id if message.from_user else None
        if self.owner_id is None or message.chat.type != 'private' or message.chat.id != user_id:
            return False
        if user_id == self.owner_id and payload.startswith('gift_'):
            await bot.send_message(user_id,
                'Это приглашение для твоей половинки 🎁\n\n'
                'Ты уже в боте. Твой переход не использует приглашение. '
                'Он сможет войти по действующей ссылке со своего аккаунта.',
                reply_markup=self.main_keyboard)
            return True
        if not payload.startswith('gift_') or not self.store.claim_invite(self.owner_id, user_id, payload[5:]):
            await bot.send_message(user_id, 'Это приглашение уже использовано или заменено. Попроси новую ссылку у своей половинки 💌')
            return True
        self.allowed_ids.add(user_id)
        self.store.remember_user(message.from_user)
        before = self.store.gift_delivery(user_id)
        self.store.prepare_gift_delivery(self.owner_id, user_id, GIFT_GREETING)
        await self.deliver_gift_welcome(bot, user_id)
        await self.notify_gift_join(bot, user_id)
        if before and before['text_sent'] and before['circle_sent']:
            await bot.send_message(user_id, 'Наш уголок уже открыт 💛', reply_markup=self.main_keyboard)
        return True

    async def deliver_gift_welcome(self, bot, user_id):
        async with self.welcome_locks.setdefault(user_id, asyncio.Lock()):
            delivery = self.store.gift_delivery(user_id)
            if (not delivery or self.partner(user_id) != delivery['owner_id']
                    or delivery['text_sent'] and delivery['circle_sent']):
                return False
            try:
                if not delivery['text_sent']:
                    await bot.send_message(user_id, delivery['greeting'], reply_markup=self.main_keyboard)
                    self.store.gift_delivery_result(user_id, text_sent=True)
                if delivery['file_id'] and not delivery['circle_sent']:
                    await bot.send_video_note(user_id, delivery['file_id'])
                    self.store.gift_delivery_result(user_id, circle_sent=True)
            except TelegramAPIError as error:
                delay = error.retry_after if isinstance(error, TelegramRetryAfter) else 60
                self.store.gift_delivery_result(user_id, blocked=isinstance(error, TelegramForbiddenError),
                                                retry_at=self.clock() + delay)
                logging.warning('Приветствие сохранено, доставка отложена: %s', type(error).__name__)
            return True

    async def resume_gift_welcome(self, message, bot):
        user_id = message.from_user.id if message.from_user else None
        if not self.permitted(message, user_id):
            return False
        delivered = await self.deliver_gift_welcome(bot, user_id)
        await self.notify_gift_join(bot, user_id)
        return delivered

    async def notify_gift_join(self, bot, user_id):
        async with self.welcome_locks.setdefault(user_id, asyncio.Lock()):
            delivery = self.store.gift_delivery(user_id)
            if (not delivery or self.partner(user_id) != delivery['owner_id']
                    or delivery['owner_notified'] or delivery['owner_notice_blocked']
                    or delivery['owner_notice_retry_at'] > self.clock()):
                return
            try:
                await bot.send_message(delivery['owner_id'], GIFT_JOIN_NOTICE)
                self.store.gift_owner_notice_result(user_id, sent=True)
            except TelegramAPIError as error:
                delay = error.retry_after if isinstance(error, TelegramRetryAfter) else 60
                self.store.gift_owner_notice_result(user_id, blocked=isinstance(error, TelegramForbiddenError),
                                                    retry_at=self.clock() + delay)
                logging.warning('Уведомление о подключении отложено: %s', type(error).__name__)

    async def retry_gift_welcome(self, bot):
        partner = self.partner(self.owner_id) if self.owner_id is not None else None
        delivery = self.store.gift_delivery(partner) if partner else None
        if delivery and not delivery['blocked'] and delivery['retry_at'] <= self.clock():
            await self.deliver_gift_welcome(bot, partner)
        if delivery:
            await self.notify_gift_join(bot, partner)

    async def welcome_menu(self, bot, user_id):
        self.store.pause(user_id)
        await bot.send_message(user_id, 'Такое послание он увидит, когда откроет личное приглашение и нажмёт «Начать» 🎁')
        await bot.send_message(user_id, GIFT_GREETING)
        file_id = self.store.gift_circle(user_id)
        if file_id:
            await bot.send_video_note(user_id, file_id)
        text = ('Этот кружочек придёт сразу следом за посланием 🎥' if file_id
                else 'Пока в приветствии только текст. Можешь добавить свой кружочек 🎥')
        if self.partner(user_id) is not None:
            await bot.send_message(user_id, 'Приветствие уже вручено 💌 Новое послание можно оставить в конверте.',
                                   reply_markup=keyboard([[('К конвертам 💌', 'env:menu')]]))
            return
        rows = [[('Заменить кружочек 🎥' if file_id else 'Добавить кружочек 🎥', 'env:welcome:record')]]
        if self.store.gift_draft(user_id):
            rows.append([('Продолжить настройку', 'env:welcome:resume')])
        if file_id:
            rows.append([('Оставить только текст', 'env:welcome:remove')])
        rows.append([('К конвертам 💌', 'env:menu')])
        await bot.send_message(user_id, text, reply_markup=keyboard(rows))

    def save_welcome_step(self, user_id, draft, step):
        draft.update(step=step, active=True, control_id=uuid.uuid4().hex)
        self.store.save_gift_draft(user_id, draft)

    async def welcome_prompt(self, bot, user_id, draft):
        prefix = f"env:welcome:d:{draft['control_id']}:"
        if draft['step'] == 'review':
            await bot.send_message(user_id, GIFT_GREETING)
            await bot.send_video_note(user_id, draft['file_id'])
            text = 'Вот так будут выглядеть послание и кружочек 💌 Сохранить для первого входа?'
            rows = [[('Сохранить для приветствия 💌', prefix + 'save')], [('Переснять 🎥', prefix + 'retake')]]
        elif draft['step'] == 'remove':
            text = 'Убрать кружочек из приветствия? Послание останется.'
            rows = [[('Да, оставить только текст', prefix + 'save')]]
        else:
            text = 'Сними и отправь кружочек прямо в этот чат обычной кнопкой Telegram 🎥\n\nСначала покажу предпросмотр. Он сохранится для приветствия только после подтверждения.'
            rows = [[('Оставить предыдущий кружочек', prefix + 'keep')]] if draft.get('file_id') else []
        rows += [[('Отменить изменения 🌿', prefix + 'cancel')], [('В меню 🐾', 'env:home')]]
        await bot.send_message(user_id, text, reply_markup=keyboard(rows))

    async def welcome_action(self, bot, user_id, parts):
        if user_id != self.owner_id:
            await bot.send_message(user_id, 'Эта настройка доступна владелице бота.')
            return
        if not parts:
            await self.welcome_menu(bot, user_id)
            return
        if self.partner(user_id) is not None:
            await bot.send_message(user_id, 'Приветствие уже вручено 💌 Новое послание можно оставить в конверте.')
            return
        if parts in (['record'], ['remove'], ['resume']):
            self.store.pause(user_id)
            draft = self.store.gift_draft(user_id) if parts == ['resume'] else None
            if not draft:
                draft = {'file_id': self.store.gift_circle(user_id), 'step': 'record'}
            step = 'remove' if parts == ['remove'] else draft['step'] if parts == ['resume'] else 'record'
            self.save_welcome_step(user_id, draft, step)
            await self.welcome_prompt(bot, user_id, draft)
            return
        draft = self.store.gift_draft(user_id)
        if len(parts) != 3 or parts[0] != 'd' or not draft or parts[1] != draft['control_id']:
            await bot.send_message(user_id, 'Эта кнопка уже устарела. Открой «Приветствие для сюрприза 🎁» ещё раз.')
            return
        self.store.pause(user_id)
        action = parts[2]
        if action == 'cancel':
            self.store.discard_gift_draft(user_id)
            await bot.send_message(user_id, 'Изменения отменены 🌿 Сохранённое приветствие осталось прежним.',
                                   reply_markup=keyboard([[('Приветствие 🎁', 'env:welcome')], [('К конвертам 💌', 'env:menu')]]))
        elif action == 'save' and self.store.confirm_gift_circle(user_id, parts[1]):
            await bot.send_message(user_id, 'Приветствие сохранено 💌 Оно придёт ему после первого входа по личной ссылке.',
                                   reply_markup=keyboard([[('Посмотреть приветствие 🎁', 'env:welcome')], [('К конвертам 💌', 'env:menu')]]))
        elif action == 'retake' and draft['step'] == 'review':
            self.store.pause(user_id)
            self.save_welcome_step(user_id, draft, 'record')
            await self.welcome_prompt(bot, user_id, draft)
        elif action == 'keep' and draft.get('file_id') and draft['step'] == 'record':
            self.store.pause(user_id)
            self.save_welcome_step(user_id, draft, 'review')
            await self.welcome_prompt(bot, user_id, draft)

    def draft_markup(self, draft, rows):
        prefix = f"env:d:{draft.get('control_id', draft['id'])}:"
        rows = list(rows)
        if draft.get('file_id') and draft.get('kind') in RETAKE_LABELS and draft['step'] != 'content':
            rows.append([(RETAKE_LABELS[draft['kind']], 'retake')])
        rows.append([('Отменить конверт 🌿', 'cancel')])
        return keyboard([[(label, prefix + action) for label, action in row] for row in rows]
                        + [[('В меню 🐾', 'env:menu')]])

    async def menu(self, bot, user_id):
        self.store.pause(user_id)
        setting = self.store.settings(user_id)
        rows = [[('Оставить конверт 💌', 'env:new')],
                [('Для тебя 💛', 'env:list:received:0'), ('От тебя 💌', 'env:list:sent:0')]]
        draft = self.store.draft(user_id)
        if draft:
            rows.append([('Продолжить черновик', 'env:resume')])
            rows.append([('Отменить черновик 🌿', f"env:d:{draft.get('control_id', draft['id'])}:cancel")])
        label = 'Отключить уведомления 🔕' if setting['notifications'] else 'Включить уведомления об открытии 🔔'
        rows.append([(label, 'env:notifications')])
        if user_id == self.owner_id:
            rows.append([('Приветствие для сюрприза 🎁', 'env:welcome')])
            rows.append([('Приглашение для сюрприза 🎁', 'env:gift')])
        rows.append([('В меню 🐾', 'env:home')])
        await bot.send_message(user_id, 'Откроешь потом 💌\n\nЗдесь ваши послания с назначенным временем открытия.', reply_markup=keyboard(rows))

    async def prompt(self, bot, user_id, draft):
        step = draft['step']
        rows = []
        if step == 'kind':
            text = 'Что оставим в конверте? 💌'
            rows = [[(label, f'kind:{kind}')] for kind, label in KINDS.items()]
        elif step == 'content':
            text = {'text': 'Напиши послание — до 2000 символов 💌',
                    'photo': 'Отправь одно фото прямо в этот чат 📷',
                    'voice': 'Запиши и отправь голосовое обычной кнопкой Telegram 🎙️',
                    'circle': 'Сними и отправь кружочек обычной кнопкой Telegram 🎥'}[draft['kind']]
            if draft.get('file_id') and draft.get('retake_return'):
                text += '\n\nПредыдущий вариант пока сохранён. Можешь отправить новый или оставить прежний.'
                rows = [[('Оставить предыдущий вариант', 'keep-media')]]
        elif step == 'review-content':
            text = {'circle': 'Кружочек получен 💛', 'voice': 'Голосовое получено 💛', 'photo': 'Фото получено 💛'}[draft['kind']]
            text += '\n\nМожешь продолжить, заменить запись или отменить конверт. Если отправишь новый вариант, он заменит этот.'
            rows = [[('Подходит, дальше 💌', 'accept-media')]]
        elif step == 'caption':
            text = 'Добавим подпись? Напиши её следующим сообщением — до 1000 символов 💌'
            rows = [[('Без подписи', 'skip-caption')]]
        elif step == 'title':
            text = 'Что написать на закрытом конверте? Выбери надпись или напиши свою — до 60 символов.'
            rows = [[(label, f'title:{i}')] for i, label in enumerate(TITLES)]
        elif step == 'zone':
            text = 'По времени какого города ты будешь задавать дату открытия?'
            rows = [[('Астана', 'zone:astana'), ('Калгари', 'zone:calgary')]]
        elif step == 'time':
            city = 'Астаны' if self.store.settings(user_id)['zone'] == ZONES['astana'] else 'Калгари'
            text = f'Когда можно открыть конверт? 💌\nСвою дату задаём по времени {city}.'
            rows = [[('Через минуту', 'time:1'), ('Через час', 'time:60')],
                    [('Выбрать дату и время', 'custom-time')], [('Изменить мой часовой пояс', 'change-zone')]]
        elif step == 'custom-time':
            text = 'Напиши дату и время открытия в формате ДД.ММ.ГГГГ ЧЧ:ММ.\nНапример: 25.12.2026 18:30.'
        elif step == 'preview':
            await self.send_payload(bot, user_id, draft)
            text = f"Запечатать конверт? 💌\n\nНадпись: {draft['title']}\n{display_time(draft['release_at'])}\n\nДо этого времени содержимое будет закрыто."
            rows = [[('Запечатать 💌', 'seal')], [('Изменить время', 'edit-time')]]
        else:
            return
        await bot.send_message(user_id, text, reply_markup=self.draft_markup(draft, rows))

    async def send_payload(self, bot, user_id, envelope):
        # Reuse Telegram file_id. No download or conversion of a native recording.
        kind, text = envelope['kind'], envelope.get('text', '')
        if kind == 'text':
            await bot.send_message(user_id, text)
        elif kind == 'photo':
            await bot.send_photo(user_id, envelope['file_id'], caption=text or None)
        elif kind == 'voice':
            await bot.send_voice(user_id, envelope['file_id'], caption=text or None)
        else:
            # Telegram video notes have no caption field.
            await bot.send_video_note(user_id, envelope['file_id'])
            if text:
                await bot.send_message(user_id, text)

    def move(self, user_id, draft, step):
        draft['step'] = step
        # Every changed step invalidates buttons attached to older recordings.
        draft['control_id'] = uuid.uuid4().hex
        self.store.save_draft(user_id, draft)

    async def discard(self, bot, user_id):
        removed = self.store.discard_draft(user_id)
        await bot.send_message(user_id, 'Конверт отменён 🌿 Можно спокойно начать заново.' if removed else 'Сейчас нет незавершённого конверта 🌿', reply_markup=keyboard([[('Новый конверт 💌', 'env:new')], [('В меню 🐾', 'env:home')]]))

    def time_step(self, user_id):
        return 'time' if self.store.settings(user_id)['zone'] else 'zone'

    async def handle_message(self, message, bot):
        user_id = message.from_user.id if message.from_user else None
        if not self.permitted(message, user_id):
            return False
        self.store.remember_user(message.from_user)
        if message.text == '/cancel':
            async with self.locks.setdefault(user_id, asyncio.Lock()):
                welcome = self.store.gift_draft(user_id) if user_id == self.owner_id else None
                if welcome and welcome.get('active'):
                    self.store.discard_gift_draft(user_id)
                    await bot.send_message(user_id, 'Изменения приветствия отменены 🌿 Сохранённый вариант остался прежним.')
                    return True
                await self.discard(bot, user_id)
            return True
        if message.text in {ENVELOPE_BUTTON, '/envelope'}:
            async with self.locks.setdefault(user_id, asyncio.Lock()):
                await self.menu(bot, user_id)
            return True
        if message.text in {'Нужен котик', '🐱 Нужен котик'}:
            self.store.pause(user_id)
            return False
        if message.text and message.text.startswith('/'):
            return False
        async with self.locks.setdefault(user_id, asyncio.Lock()):
            welcome = self.store.gift_draft(user_id) if user_id == self.owner_id else None
            if welcome and welcome.get('active'):
                if self.partner(user_id) is not None:
                    self.store.discard_gift_draft(user_id)
                    await bot.send_message(user_id, 'Приветствие уже вручено 💌 Для нового кружочка открой «Откроешь потом 💌».')
                    return True
                if message.video_note and welcome['step'] in {'record', 'review'}:
                    welcome['file_id'] = message.video_note.file_id
                    self.save_welcome_step(user_id, welcome, 'review')
                await self.welcome_prompt(bot, user_id, welcome)
                return True
            draft = self.store.draft(user_id)
            if not draft or not draft.get('active'):
                return False
            step, text = draft['step'], (message.text or '').strip()
            kind = draft.get('kind')
            media = (message.photo[-1] if message.photo else None) if kind == 'photo' else getattr(message, {'voice': 'voice', 'circle': 'video_note'}.get(kind, '_none'), None)
            if kind in RETAKE_LABELS and media and step != 'kind':
                if getattr(message, 'media_group_id', None):
                    await bot.send_message(user_id, 'Для одного конверта отправь одно фото отдельно от альбома 💌')
                    return True
                caption = (message.caption or '').strip()
                if not caption_fits(caption):
                    await bot.send_message(user_id, 'Пришли подпись до 1000 символов вместе с записью или фото.')
                    return True
                previous = bool(draft.get('file_id'))
                if not previous:
                    after = 'title' if caption else 'caption'
                elif step == 'content':
                    after = draft.pop('retake_return', 'caption')
                elif step == 'review-content':
                    after = draft.get('after_review', 'caption')
                else:
                    after = step
                draft.update(file_id=media.file_id, text=caption or (draft.get('text', '') if previous else ''), after_review=after)
                self.move(user_id, draft, 'review-content')
                await self.prompt(bot, user_id, draft)
                return True
            if step == 'content':
                if getattr(message, 'media_group_id', None):
                    await bot.send_message(user_id, 'Для одного конверта отправь одно фото отдельно от альбома 💌')
                    return True
                if kind == 'text' and text and len(text) <= 2000:
                    draft['text'] = text
                    self.move(user_id, draft, 'title')
                else:
                    await self.prompt(bot, user_id, draft)
                    return True
            elif step == 'caption' and text and caption_fits(text):
                draft['text'] = text
                self.move(user_id, draft, 'title')
            elif step == 'title' and text and len(text) <= 60:
                draft['title'] = text
                self.move(user_id, draft, self.time_step(user_id))
            elif step == 'custom-time' and text:
                try:
                    release = parse_local_time(text, self.store.settings(user_id)['zone'])
                    if release <= self.clock():
                        raise ValueError('Past time')
                except (ValueError, OverflowError):
                    await bot.send_message(user_id, 'Нужно будущее время в формате ДД.ММ.ГГГГ ЧЧ:ММ. Если часы переводятся, выбери другое время.')
                    return True
                draft['release_at'] = release
                self.move(user_id, draft, 'preview')
            else:
                await self.prompt(bot, user_id, draft)
                return True
            await self.prompt(bot, user_id, draft)
            return True

    async def handle_callback(self, callback, bot):
        if not callback.data or not callback.data.startswith('env:'):
            return False
        user_id = callback.from_user.id
        if not callback.message or not self.permitted(callback.message, user_id):
            await callback.answer('Это личный бот.', show_alert=True)
            return True
        async with self.locks.setdefault(user_id, asyncio.Lock()):
            parts = callback.data.split(':')
            self.store.remember_user(callback.from_user)
            await callback.answer()
            action = parts[1]
            if action != 'welcome':
                self.store.pause_gift_draft(user_id)
            if action == 'welcome':
                await self.welcome_action(bot, user_id, parts[2:])
            elif action == 'menu':
                await self.menu(bot, user_id)
            elif action == 'home':
                self.store.pause(user_id)
                await bot.send_message(user_id, 'Что хочется сделать? 💛', reply_markup=self.main_keyboard)
            elif action == 'notifications':
                enabled = not self.store.settings(user_id)['notifications']
                self.store.set_setting(user_id, notifications=enabled)
                await bot.send_message(user_id, 'Уведомления об открытии включены 🔔' if enabled else 'Уведомления отключены 🔕')
                await self.menu(bot, user_id)
            elif action == 'gift' and user_id == self.owner_id:
                if self.partner(user_id) is not None:
                    await bot.send_message(user_id, 'Твоя половинка уже подключена 💛')
                    return True
                me = await bot.get_me()
                token = secrets.token_urlsafe(24)
                self.store.create_invite(user_id, token)
                link = f'https://t.me/{me.username}?start=gift_{token}'
                await bot.send_message(user_id, 'Личное приглашение для твоей половинки 🎁\n\n' + link + '\n\nОтправь его, когда сюрприз будет готов. Заранее запускать бот ему не нужно. Если ты уже создавала ссылку, эта заменяет предыдущую.', disable_web_page_preview=True)
            elif action in {'new', 'replace', 'resume'}:
                draft = self.store.draft(user_id)
                if action == 'new' and draft:
                    await bot.send_message(user_id, 'У тебя есть черновик 💌', reply_markup=keyboard([[('Продолжить', 'env:resume')], [('Начать новый конверт', 'env:replace')], [('К конвертам', 'env:menu')]]))
                    return True
                if not draft or action == 'replace':
                    draft = {'id': uuid.uuid4().hex, 'step': 'kind', 'active': True, 'text': ''}
                draft['active'] = True
                self.store.save_draft(user_id, draft)
                await self.prompt(bot, user_id, draft)
            elif action == 'chat':
                partner = self.partner(user_id)
                username = self.store.username(partner) if partner else None
                if username and re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
                    link, label = f'https://t.me/{username}', 'В наш чат 💛'
                elif partner:
                    link, label = f'tg://user?id={partner}', 'Открыть профиль 💛'
                else:
                    await bot.send_message(user_id, 'Сначала нужно настроить вашу пару.')
                    return True
                await bot.send_message(user_id, 'Продолжите общение вдвоём 💛', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=link)]]))
            elif action == 'list' and len(parts) == 4 and parts[2] in {'sent', 'received'} and parts[3].isdigit():
                await self.listing(bot, user_id, parts[2], min(int(parts[3]), 100000))
            elif action in {'view', 'open', 'cancel'} and len(parts) == 3:
                async with self.envelope_locks.setdefault(parts[2], asyncio.Lock()):
                    if action == 'cancel':
                        if self.store.cancel(parts[2], user_id):
                            await bot.send_message(user_id, 'Конверт отменён 🌿 Он не будет доступен получателю.', reply_markup=keyboard([[('Новый конверт 💌', 'env:new')], [('К конвертам 💌', 'env:menu')]]))
                        else:
                            await bot.send_message(user_id, 'Этот конверт уже открыт, отменён или недоступен. Открой список «От тебя 💌».')
                    else:
                        await self.view(bot, user_id, parts[2], open_now=action == 'open')
            elif action == 'd' and len(parts) >= 4:
                draft = self.store.draft(user_id)
                if not draft or parts[2] != draft.get('control_id', draft['id']) or (not draft.get('active') and parts[3] != 'cancel'):
                    await bot.send_message(user_id, 'Этот шаг уже завершён. Открой «Откроешь потом 💌».')
                    return True
                await self.draft_action(bot, user_id, draft, parts[3:])
            return True

    async def draft_action(self, bot, user_id, draft, parts):
        action, step = parts[0], draft['step']
        value = parts[1] if len(parts) == 2 else ''
        if action == 'cancel':
            await self.discard(bot, user_id)
            return
        elif action == 'retake' and draft.get('kind') in RETAKE_LABELS and draft.get('file_id'):
            draft['retake_return'] = draft.get('after_review', 'caption') if step == 'review-content' else step
            self.move(user_id, draft, 'content')
        elif action == 'keep-media' and step == 'content' and draft.get('file_id') and draft.get('retake_return'):
            self.move(user_id, draft, draft.pop('retake_return'))
        elif action == 'accept-media' and step == 'review-content':
            target = draft.pop('after_review', 'caption')
            if target == 'preview' and draft.get('release_at', 0) <= self.clock():
                target = self.time_step(user_id)
            self.move(user_id, draft, target)
        elif action == 'kind' and step == 'kind' and value in KINDS:
            draft['kind'] = value
            self.move(user_id, draft, 'content')
        elif action == 'skip-caption' and step == 'caption':
            draft['text'] = ''
            self.move(user_id, draft, 'title')
        elif action == 'title' and step == 'title' and value in {'0', '1', '2'}:
            draft['title'] = TITLES[int(value)]
            self.move(user_id, draft, self.time_step(user_id))
        elif action == 'zone' and step == 'zone' and value in ZONES:
            self.store.set_setting(user_id, zone=ZONES[value])
            self.move(user_id, draft, 'time')
        elif action == 'change-zone' and step == 'time':
            self.move(user_id, draft, 'zone')
        elif action == 'time' and step == 'time' and value in {'1', '60'}:
            draft['release_at'] = self.clock() + int(value) * 60
            self.move(user_id, draft, 'preview')
        elif action == 'custom-time' and step == 'time':
            self.move(user_id, draft, 'custom-time')
        elif action == 'edit-time' and step == 'preview':
            self.move(user_id, draft, self.time_step(user_id))
        elif action == 'seal' and step == 'preview':
            recipient = self.partner(user_id)
            if recipient is None and user_id != self.owner_id:
                await bot.send_message(user_id, 'Сначала нужно настроить вашу пару.')
                return
            if draft['release_at'] <= self.clock():
                self.move(user_id, draft, self.time_step(user_id))
                await bot.send_message(user_id, 'Время открытия уже прошло. Выберем новое 💌')
            else:
                self.store.seal(user_id, recipient or 0, draft)
                ending = 'Он появится в разделе «Для тебя» у твоей половинки.' if recipient else 'Он будет ждать твою половинку после первого запуска по приглашению 🎁'
                await bot.send_message(user_id, f"Конверт запечатан 💌\n{display_time(draft['release_at'])}\n\n{ending}", reply_markup=keyboard([[('Отменить конверт 🌿', f"env:cancel:{draft['id']}")], [('К конвертам 💌', 'env:menu')]]))
                return
        else:
            await bot.send_message(user_id, 'Этот шаг уже завершён. Продолжи с последних кнопок 💌')
            return
        await self.prompt(bot, user_id, draft)

    async def listing(self, bot, user_id, direction, offset):
        self.store.pause(user_id)
        items, more = self.store.listing(user_id, direction, offset)
        rows = [[(f"{'🌿' if e['cancelled'] else '💛' if e['opened'] else '💌' if e['release_at'] <= self.clock() else '🔒'} {e['title'][:35]}", f"env:view:{e['id']}")] for e in items]
        if offset:
            rows.append([('Назад', f'env:list:{direction}:{max(0, offset-8)}')])
        if more:
            rows.append([('Дальше', f'env:list:{direction}:{offset+8}')])
        rows.append([('К конвертам 💌', 'env:menu')])
        await bot.send_message(user_id, ('От тебя 💌' if direction == 'sent' else 'Для тебя 💛') if items else 'Здесь пока нет конвертов 💌', reply_markup=keyboard(rows))

    async def view(self, bot, user_id, envelope_id, *, open_now=False):
        envelope = self.store.get(envelope_id, user_id)
        if not envelope:
            await bot.send_message(user_id, 'Конверт не найден.')
            return
        mine = envelope['sender'] == user_id
        if envelope['cancelled']:
            await bot.send_message(user_id, f"Конверт отменён 🌿\n{envelope['title']}", reply_markup=keyboard([[('Новый конверт 💌', 'env:new')], [('К конвертам 💌', 'env:menu')]]))
            return
        ready = self.clock() >= envelope['release_at']
        rows = []
        if open_now and (mine or ready):
            await self.send_payload(bot, user_id, envelope)
            if not mine:
                self.store.mark_opened(envelope_id, user_id)
                rows.append([('Ответить конвертом 💌', 'env:new')])
                rows.append([('В наш чат 💛', 'env:chat')])
            rows.append([('В меню 🐾', 'env:home')])
            if mine and not envelope['opened']:
                rows.insert(0, [('Отменить конверт 🌿', f'env:cancel:{envelope_id}')])
            await bot.send_message(user_id, 'Твоё послание 💌' if mine else 'Конверт открыт 💛', reply_markup=keyboard(rows))
            return
        if mine or ready:
            rows.append([('Посмотреть послание 💌' if mine else 'Открыть 💌', f'env:open:{envelope_id}')])
        else:
            rows.append([('Проверить время 🔒', f'env:view:{envelope_id}')])
        if mine and not envelope['opened']:
            rows.append([('Отменить конверт 🌿', f'env:cancel:{envelope_id}')])
        rows.append([('К конвертам 💌', 'env:menu')])
        await bot.send_message(user_id, f"{envelope['title']}\n\n{display_time(envelope['release_at'])}\n\n{'Уже можно открыть 💌' if ready else 'Запечатано до назначенного времени 🔒'}", reply_markup=keyboard(rows))

    async def notify_due(self, bot):
        for envelope in self.store.claim_due(self.clock()):
            if self.partner(envelope['sender']) != envelope['recipient']:
                self.store.notification_result(envelope['id'], blocked=True)
                continue
            if not self.store.settings(envelope['recipient'])['notifications']:
                self.store.notification_result(envelope['id'])
                continue
            current = self.store.get(envelope['id'], envelope['recipient'])
            if current is None or current['opened']:
                self.store.notification_result(envelope['id'], sent=True)
                continue
            try:
                await bot.send_message(envelope['recipient'], f"Конверт можно открыть 💌\n{envelope['title']}", reply_markup=keyboard([[('Открыть 💌', f"env:open:{envelope['id']}")]]))
            except TelegramForbiddenError:
                self.store.notification_result(envelope['id'], blocked=True)
            except TelegramAPIError as error:
                delay = error.retry_after if isinstance(error, TelegramRetryAfter) else 60
                self.store.notification_result(envelope['id'], retry_at=self.clock() + delay)
                logging.warning('Уведомление конверта отложено: %s', type(error).__name__)
            else:
                self.store.notification_result(envelope['id'], sent=True)

    async def worker(self, bot):
        while True:
            try:
                await self.retry_gift_welcome(bot)
                await self.notify_due(bot)
            except Exception as error:
                logging.warning('Проверка конвертов отложена: %s', type(error).__name__)
            await asyncio.sleep(15)
