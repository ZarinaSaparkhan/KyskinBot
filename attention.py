"""Human-triggered requests and exact personal replies, with a gift-safe demo."""

import asyncio
import logging
import uuid

from aiogram.exceptions import TelegramAPIError

from attention_store import AttentionStore
from envelopes import ENVELOPE_BUTTON, keyboard

ATTENTION_BUTTON = 'Хочу к тебе 💛'
WISHES = {
    'circle': ('Кружочек 🎥', 'Хочется увидеть тебя в кружочке 💛'),
    'voice': ('Услышать голос 🎙️', 'Хочется услышать твой голос 💛'),
    'call': ('Созвониться 📞', 'Хочется немного поговорить с тобой 💛'),
    'nearby': ('Просто побыть рядом 💛', 'Хочется побыть с тобой на созвоне, даже если каждый занимается своими делами 💛'),
}
REPLIES = {
    'ready': 'я туть 🥰',
    'later': 'шушуть попозже котенок, я тоже хочу к тебе 🥺💛',
    'planning': 'давай запланируем время 🗓️💛',
}
RECORDINGS = {
    'circle': ('кружочек', 'Записать кружочек 🎥', 'Переснять 🎥', 'video_note'),
    'voice': ('голосовое', 'Записать голосовое 🎙️', 'Перезаписать 🎙️', 'voice'),
}


class AttentionFlow:
    def __init__(self, envelopes):
        self.envelopes = envelopes
        self.store = AttentionStore(envelopes.store)
        self.user_locks = envelopes.locks
        self.request_locks = {}

    def demo_mode(self, user_id):
        return self.envelopes.partner(user_id) is None and user_id == self.envelopes.owner_id

    def access(self, request_id, user_id):
        request = self.store.get(request_id, user_id)
        if not request:
            return None
        if request['demo']:
            return request if request['sender'] == request['recipient'] == user_id == self.envelopes.owner_id else None
        return request if self.envelopes.partner(request['sender']) == request['recipient'] else None

    def recording_request(self, user_id, draft):
        request = self.access(draft['request_id'], user_id) if draft else None
        if (request and request['recipient'] == user_id and request['kind'] in RECORDINGS
                and request['status'] != 'done' and request['revision'] == draft['revision']
                and not request['reply_file_id']):
            return request
        return None

    async def recording_prompt(self, bot, user_id, draft):
        noun, _, retake, _ = RECORDINGS[draft['kind']]
        prefix = f"care:m:{draft['control_id']}:"
        rows = []
        if draft.get('file_id') and not draft.get('retaking'):
            text = 'Запись готова 💛 Можно посмотреть или послушать её выше. Передать?'
            rows += [[('Отправить 💌', prefix + 'send')], [(retake, prefix + 'retake')]]
        else:
            text = f'Запиши и отправь {noun} прямо в этот чат 💛\n\nПеред передачей бот попросит твоё подтверждение.'
            if draft.get('file_id'):
                rows.append([('Оставить предыдущую запись', prefix + 'keep')])
        if self.access(draft['request_id'], user_id)['demo']:
            text += '\n\nЭто проба: запись и ответ увидишь только ты 👀'
        rows += [[('Отправить только ответ', prefix + 'text')], [('Не отправлять запись 🌿', prefix + 'back')], [('В меню 🐾', 'care:home')]]
        await bot.send_message(user_id, text, reply_markup=keyboard(rows))

    async def start_recording(self, bot, user_id, request):
        draft = self.store.recording(user_id)
        if not self.recording_request(user_id, draft) or draft['request_id'] != request['id']:
            draft = {'request_id': request['id'], 'revision': request['revision'], 'kind': request['kind']}
        draft['active'] = True
        self.store.save_recording(user_id, draft)
        await self.recording_prompt(bot, user_id, draft)

    @staticmethod
    def reply_needs_delivery(request):
        return (request['reply_delivery'] in {'pending', 'failed'}
                or bool(request['reply_file_id']) and request['media_delivery'] != 'sent')

    async def menu(self, bot, user_id):
        self.envelopes.store.pause(user_id)
        self.store.pause_recording(user_id)
        self.store.clear_draft(user_id)
        demo = self.demo_mode(user_id)
        text = 'Как хочется побыть вместе? 💛'
        if demo:
            text += '\n\nПока готовим сюрприз, здесь можно попробовать оба экрана. Все сообщения увидишь только ты.'
        rows = []
        draft = self.store.recording(user_id)
        recording = self.recording_request(user_id, draft)
        if recording:
            rows.append([('Продолжить ответ 💌', f"care:record:{recording['id']}:{recording['revision']}")])
        outgoing = self.store.outgoing(user_id, demo)
        if outgoing:
            rows.append([('Твоя просьба 💛' if not demo else 'Продолжить пробу 👀', f"care:view:{outgoing['id']}:sender")])
        else:
            rows.extend([[(label, f'care:choose:{kind}')] for kind, (label, _) in WISHES.items()])
        for request in self.store.incoming(user_id):
            if self.access(request['id'], user_id):
                rows.append([(f"От твоей половинки: {WISHES[request['kind']][0]}", f"care:view:{request['id']}:receiver")])
        rows.append([('В меню 🐾', 'care:home')])
        await bot.send_message(user_id, text, reply_markup=keyboard(rows))

    async def view(self, bot, user_id, request, role='sender'):
        demo = bool(request['demo'])
        role = role if demo else ('sender' if request['sender'] == user_id else 'receiver')
        status, request_id = request['status'], request['id']
        if status == 'done':
            await bot.send_message(user_id, 'Проба завершена 🌿' if demo else 'Вы уже пообщались 💛', reply_markup=keyboard([[('Хочу к тебе 💛', 'care:menu')], [('В меню 🐾', 'care:home')]]))
            return
        text = ('Проба: твой экран 💛' if role == 'sender' else 'Проба: экран партнёра 👀') if demo else ('Твоя просьба 💛' if role == 'sender' else 'От твоей половинки 💛')
        text += '\n\n' + WISHES[request['kind']][1]
        rows = []
        if role == 'sender':
            if status != 'pending':
                text += f'\n\nОтвет: «{REPLIES[status]}»'
            elif demo:
                text += '\n\nПосмотри, какой выбор будет у твоей половинки.'
            elif request['delivery'] != 'sent':
                text += '\n\nПросьба сохранена. Доставка пока не подтверждена.'
                rows.append([('Попробовать отправить ещё раз', f'care:retry:{request_id}')])
            else:
                text += '\n\nПросьба отправлена 💌'
            if demo:
                rows.append([('Посмотреть за партнёра 👀', f'care:view:{request_id}:receiver')])
        else:
            if status != 'pending':
                text += f'\n\nТвой ответ: «{REPLIES[status]}»'
            if self.reply_needs_delivery(request):
                text += '\n\nОтвет сохранён. Доставка пока не подтверждена.'
                rows.append([('Повторить передачу ответа', f'care:retry-reply:{request_id}')])
            if request['reply_file_id'] and request['media_delivery'] == 'sent':
                text += '\n\nЗапись отправлена 💌' if not demo else '\n\nПробная запись показана только тебе 👀'
            replies = ('ready', 'later', 'planning') if status == 'pending' else ('ready',) if status in {'later', 'planning'} else ()
            rows.extend([[(REPLIES[reply], f"care:reply:{request_id}:{request['revision']}:{reply}")] for reply in replies])
            if status == 'ready' and request['kind'] in RECORDINGS and not request['reply_file_id']:
                rows.append([(RECORDINGS[request['kind']][1], f"care:record:{request_id}:{request['revision']}")])
            if demo:
                rows.append([('Посмотреть свой экран 💛', f'care:view:{request_id}:sender')])
        if status in {'ready', 'planning'}:
            rows.append([('В наш чат 💛', f'care:demo-chat:{request_id}' if demo else 'env:chat')])
        rows.append([('Завершить пробу 🌿' if demo else 'Мы уже пообщались 💛', f'care:done:{request_id}')])
        rows.append([('В меню 🐾', 'care:home')])
        await bot.send_message(user_id, text, reply_markup=keyboard(rows))

    async def deliver(self, bot, request, *, reply=False):
        if request['demo'] and not reply:
            return
        target = request['sender'] if reply else request['recipient']
        text = f"Твоя половинка ответила 💛\n\n«{REPLIES[request['status']]}»" if reply else 'От твоей половинки 💛\n\n' + WISHES[request['kind']][1]
        if request['demo']:
            text = f"Проба: ответ партнёра 👀\n\n«{REPLIES[request['status']]}»"
        role = 'sender' if reply else 'receiver'
        markup = keyboard([[('Посмотреть 💛', f"care:view:{request['id']}:{role}")]])
        # The first received request includes the actual response buttons.
        if not reply:
            markup = keyboard([[(label, f"care:reply:{request['id']}:{request['revision']}:{status}")] for status, label in REPLIES.items()] + [[('В меню 🐾', 'care:home')]])
        if not reply or request['reply_delivery'] != 'sent':
            try:
                await bot.send_message(target, text, reply_markup=markup)
            except TelegramAPIError as error:
                self.store.delivery(request['id'], 'failed', reply=reply)
                logging.warning('Просьба внимания сохранена, доставка отложена: %s', type(error).__name__)
                return
            else:
                self.store.delivery(request['id'], 'sent', reply=reply)
        if reply and request['reply_file_id'] and request['media_delivery'] != 'sent':
            try:
                if request['kind'] == 'voice':
                    await bot.send_voice(target, request['reply_file_id'])
                else:
                    await bot.send_video_note(target, request['reply_file_id'])
            except TelegramAPIError as error:
                self.store.media_delivery(request['id'], 'failed')
                logging.warning('Запись внимания сохранена, доставка отложена: %s', type(error).__name__)
            else:
                self.store.media_delivery(request['id'], 'sent')

    async def handle_message(self, message, bot):
        user_id = message.from_user.id if message.from_user else None
        if not self.envelopes.permitted(message, user_id):
            return False
        async with self.user_locks.setdefault(user_id, asyncio.Lock()):
            self.envelopes.store.remember_user(message.from_user)
            if message.text == ATTENTION_BUTTON:
                await self.menu(bot, user_id)
                return True
            if message.text in {ENVELOPE_BUTTON, '/envelope', 'Нужен котик', '🐱 Нужен котик'}:
                self.store.pause_recording(user_id)
                return False
            draft = self.store.recording(user_id)
            if not draft or not draft.get('active'):
                return False
            if message.text == '/cancel':
                self.store.discard_recording(user_id)
                await bot.send_message(user_id, 'Запись убрана 🌿 Просьба остаётся в «Хочу к тебе 💛».', reply_markup=self.envelopes.main_keyboard)
                return True
            if message.text and message.text.startswith('/'):
                self.store.pause_recording(user_id)
                return False
            async with self.request_locks.setdefault(draft['request_id'], asyncio.Lock()):
                request = self.recording_request(user_id, draft)
                if not request:
                    self.store.discard_recording(user_id)
                    await bot.send_message(user_id, 'Просьба уже изменилась. Открой «Хочу к тебе 💛» ещё раз.')
                    return True
                media = getattr(message, RECORDINGS[request['kind']][3], None)
                if media and not message.media_group_id:
                    draft.update(file_id=media.file_id, retaking=False)
                    self.store.save_recording(user_id, draft)
                await self.recording_prompt(bot, user_id, draft)
            return True

    async def handle_callback(self, callback, bot):
        if not callback.data:
            return False
        if not callback.data.startswith('care:'):
            if callback.data.startswith(('env:', 'cat:', 'mood:', 'more:', 'finish:')) and callback.message and self.envelopes.permitted(callback.message, callback.from_user.id):
                async with self.user_locks.setdefault(callback.from_user.id, asyncio.Lock()):
                    self.store.pause_recording(callback.from_user.id)
            return False
        user_id = callback.from_user.id
        if not callback.message or not self.envelopes.permitted(callback.message, user_id):
            await callback.answer('Это личный бот.', show_alert=True)
            return True
        async with self.user_locks.setdefault(user_id, asyncio.Lock()):
            await callback.answer()
            self.envelopes.store.remember_user(callback.from_user)
            self.envelopes.store.pause(user_id)
            parts = callback.data.split(':')
            if parts[1] not in {'m', 'record'}:
                self.store.pause_recording(user_id)
            if parts == ['care', 'menu']:
                await self.menu(bot, user_id)
                return True
            if parts == ['care', 'home']:
                self.store.clear_draft(user_id)
                await bot.send_message(user_id, 'Что хочется сделать? 💛', reply_markup=self.envelopes.main_keyboard)
                return True
            if len(parts) == 4 and parts[1] == 'm':
                draft = self.store.recording(user_id)
                if not draft or not draft.get('active') or draft['control_id'] != parts[2]:
                    await bot.send_message(user_id, 'Эта кнопка уже использована. Открой просьбу заново 💛')
                    return True
                async with self.request_locks.setdefault(draft['request_id'], asyncio.Lock()):
                    request = self.recording_request(user_id, draft)
                    if not request:
                        self.store.discard_recording(user_id)
                        await bot.send_message(user_id, 'Просьба уже изменилась. Открой «Хочу к тебе 💛» ещё раз.')
                        return True
                    action = parts[3]
                    if action in {'send', 'text'}:
                        result = self.store.confirm_recording(user_id, parts[2], text_only=action == 'text')
                        if result:
                            await self.deliver(bot, result, reply=True)
                            await self.view(bot, user_id, self.store.get(result['id'], user_id), 'sender' if result['demo'] else 'receiver')
                        else:
                            await self.recording_prompt(bot, user_id, draft)
                    elif action == 'back':
                        self.store.discard_recording(user_id)
                        await self.view(bot, user_id, request, 'receiver')
                    elif action in {'retake', 'keep'} and draft.get('file_id'):
                        draft['retaking'] = action == 'retake'
                        self.store.save_recording(user_id, draft)
                        await self.recording_prompt(bot, user_id, draft)
                return True
            if len(parts) == 3 and parts[1] == 'choose' and parts[2] in WISHES:
                demo = self.demo_mode(user_id)
                existing = self.store.outgoing(user_id, demo)
                if existing:
                    await self.view(bot, user_id, existing)
                    return True
                token = uuid.uuid4().hex
                self.store.save_draft(user_id, token, parts[2])
                text = ('Попробуем эту просьбу? Никому её не отправляем 👀' if demo else 'Передать твоей половинке? 💛') + '\n\n' + WISHES[parts[2]][1]
                await bot.send_message(user_id, text, reply_markup=keyboard([[('Попробовать 💛' if demo else 'Да, передать 💛', f'care:send:{token}')], [('Выбрать другое', 'care:menu')], [('В меню 🐾', 'care:home')]]))
                return True
            if len(parts) == 3 and parts[1] == 'send':
                draft = self.store.draft(user_id)
                if not draft or draft['token'] != parts[2]:
                    await bot.send_message(user_id, 'Этот шаг уже завершён. Открой «Хочу к тебе 💛».')
                    return True
                partner = self.envelopes.partner(user_id)
                demo = self.demo_mode(user_id)
                if partner is None and not demo:
                    await bot.send_message(user_id, 'Сначала подключитесь по приглашению 💌')
                    return True
                request, created = self.store.create(user_id, user_id if demo else partner, parts[2], demo)
                if request:
                    async with self.request_locks.setdefault(request['id'], asyncio.Lock()):
                        if created:
                            await self.deliver(bot, request)
                        await self.view(bot, user_id, self.store.get(request['id'], user_id))
                return True
            if len(parts) >= 3:
                request_id = parts[2]
                async with self.request_locks.setdefault(request_id, asyncio.Lock()):
                    request = self.access(request_id, user_id)
                    if not request:
                        await bot.send_message(user_id, 'Эта просьба недоступна.')
                        return True
                    action = parts[1]
                    role = 'sender' if request['sender'] == user_id else 'receiver'
                    if action == 'view' and len(parts) == 4 and parts[3] in {'sender', 'receiver'}:
                        await self.view(bot, user_id, request, parts[3])
                    elif action == 'done' and len(parts) == 3:
                        self.store.finish(request_id, user_id)
                        await self.menu(bot, user_id)
                    elif action == 'demo-chat' and request['demo']:
                        await bot.send_message(user_id, 'В готовом боте эта кнопка откроет ваш личный чат 💛', reply_markup=keyboard([[('К пробе 👀', f'care:view:{request_id}:sender')]]))
                    elif action == 'reply' and len(parts) == 5 and parts[3].isdigit() and parts[4] in REPLIES:
                        if user_id != request['recipient'] or request['status'] == 'done':
                            await bot.send_message(user_id, 'Ответить может получатель незавершённой просьбы.')
                            return True
                        if (parts[4] == 'ready' and request['kind'] in RECORDINGS
                                and request['revision'] == int(parts[3]) and request['status'] in {'pending', 'later', 'planning'}):
                            await self.start_recording(bot, user_id, request)
                            return True
                        if self.store.respond(request_id, user_id, int(parts[3]), parts[4]):
                            request = self.store.get(request_id, user_id)
                            await self.deliver(bot, request, reply=True)
                        await self.view(bot, user_id, self.store.get(request_id, user_id), 'sender' if request['demo'] else 'receiver')
                    elif action == 'retry' and role == 'sender' and request['status'] == 'pending' and request['delivery'] != 'sent':
                        await self.deliver(bot, request)
                        await self.view(bot, user_id, self.store.get(request_id, user_id))
                    elif (action == 'record' and len(parts) == 4 and parts[3].isdigit()
                          and user_id == request['recipient'] and request['status'] != 'done'
                          and request['kind'] in RECORDINGS and request['revision'] == int(parts[3]) and not request['reply_file_id']):
                        await self.start_recording(bot, user_id, request)
                    elif action == 'retry-reply' and user_id == request['recipient'] and request['status'] in REPLIES and self.reply_needs_delivery(request):
                        await self.deliver(bot, request, reply=True)
                        await self.view(bot, user_id, self.store.get(request_id, user_id), role)
            return True
