"""No network: native recordings, release gates, persistence and gift onboarding."""

import sys
import tempfile
import unittest
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto, SendVoice, SendVideoNote, GetMe
from aiogram.types import CallbackQuery, Chat, Message, User, Voice, VideoNote, PhotoSize

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from envelope_store import EnvelopeStore
from envelopes import EnvelopeFlow, parse_local_time, ZONES, caption_fits


class RecordingBot(Bot):
    def __init__(self):
        super().__init__('123456:OFFLINE_TEST_ONLY')
        self.calls = []
        self.media_started = None
        self.media_continue = None

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name='Test', username='test_envelope_bot')
        if isinstance(method, (SendMessage, SendPhoto, SendVoice, SendVideoNote)):
            if self.media_started and isinstance(method, (SendPhoto, SendVoice, SendVideoNote)) and method.chat_id == 202:
                self.media_started.set()
                await self.media_continue.wait()
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type='private'))
        raise AssertionError(type(method).__name__)


class EnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'envelopes.sqlite3'
        self.store = EnvelopeStore(self.path)
        self.now = 1800000000.0
        self.flow = EnvelopeFlow(self.store, {101, 202}, None, clock=lambda: self.now)
        self.bot = RecordingBot()

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.directory.cleanup()

    def message(self, user=101, text=None, **fields):
        return Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=user, type='private'),
                       from_user=User(id=user, is_bot=False, first_name='Test', username=f'person_{user}'),
                       text=text, **fields).as_(self.bot)

    async def callback(self, data, user=101):
        cb = CallbackQuery(id='test', chat_instance='test', data=data,
                           from_user=self.message(user).from_user, message=self.message(user)).as_(self.bot)
        await self.flow.handle_callback(cb, self.bot)

    async def draft_action(self, action, user=101):
        draft = self.store.draft(user)
        await self.callback(f"env:d:{draft.get('control_id', draft['id'])}:{action}", user)

    async def prepared(self, kind='voice', caption=None):
        await self.callback('env:new')
        await self.draft_action(f'kind:{kind}')
        content = {'voice': dict(voice=Voice(file_id='VOICE', file_unique_id='v', duration=3)),
                   'circle': dict(video_note=VideoNote(file_id='CIRCLE', file_unique_id='c', duration=3, length=240)),
                   'photo': dict(photo=[PhotoSize(file_id='PHOTO', file_unique_id='p', width=240, height=240)]),
                   'text': dict(text='Личное послание')}
        await self.flow.handle_message(self.message(**content[kind]), self.bot)
        if kind != 'text':
            await self.draft_action('accept-media')
            if caption:
                await self.flow.handle_message(self.message(text=caption), self.bot)
            else:
                await self.draft_action('skip-caption')
        await self.draft_action('title:0')
        if self.store.draft(101)['step'] == 'zone':
            await self.draft_action('zone:astana')
        await self.draft_action('time:1')
        return self.store.draft(101)['id']

    async def test_native_formats_and_deadline_cannot_be_bypassed(self):
        for kind, method in [('voice', SendVoice), ('circle', SendVideoNote), ('photo', SendPhoto), ('text', SendMessage)]:
            with self.subTest(kind=kind):
                envelope_id = await self.prepared(kind)
                await self.draft_action('seal')
                self.bot.calls.clear()
                await self.callback(f'env:open:{envelope_id}', 202)
                self.assertFalse(self.store.get(envelope_id, 202)['opened'])
                self.assertFalse(any(isinstance(c, (SendVoice, SendPhoto, SendVideoNote)) for c in self.bot.calls))
                self.assertFalse(any(getattr(c, 'text', None) == 'Личное послание' for c in self.bot.calls))
                self.now += 61
                self.bot.calls.clear()
                await self.callback(f'env:open:{envelope_id}', 202)
                self.assertTrue(self.store.get(envelope_id, 202)['opened'])
                self.assertTrue(any(isinstance(c, method) and c.chat_id == 202 for c in self.bot.calls))

    async def test_no_recipient_delivery_during_composition(self):
        envelope_id = await self.prepared()
        await self.draft_action('seal')
        self.assertTrue(self.store.get(envelope_id, 101))
        self.assertFalse(any(getattr(c, 'chat_id', None) == 202 for c in self.bot.calls))
        await self.flow.notify_due(self.bot)
        self.assertFalse(any(getattr(c, 'chat_id', None) == 202 for c in self.bot.calls))

    async def test_draft_and_media_survive_reopening_database(self):
        await self.callback('env:new')
        await self.draft_action('kind:circle')
        await self.flow.handle_message(self.message(video_note=VideoNote(file_id='NATIVE_NOTE', file_unique_id='n', duration=4, length=240)), self.bot)
        reopened = EnvelopeStore(self.path)
        self.assertEqual(reopened.draft(101)['file_id'], 'NATIVE_NOTE')
        self.assertEqual(reopened.draft(101)['step'], 'review-content')
        self.assertEqual(reopened.draft(101)['kind'], 'circle')

    async def test_wrong_format_does_not_advance_or_store_file(self):
        await self.callback('env:new')
        await self.draft_action('kind:voice')
        await self.flow.handle_message(self.message(video_note=VideoNote(file_id='WRONG', file_unique_id='w', duration=1, length=240)), self.bot)
        self.assertEqual(self.store.draft(101)['step'], 'content')
        self.assertNotIn('file_id', self.store.draft(101))

    async def test_stranger_cannot_open_an_envelope(self):
        envelope_id = await self.prepared()
        await self.draft_action('seal')
        self.now += 61
        self.bot.calls.clear()
        await self.callback(f'env:open:{envelope_id}', 303)
        self.assertIsNone(self.store.get(envelope_id, 303))
        self.assertFalse(any(isinstance(c, SendVoice) for c in self.bot.calls))

    async def test_duplicate_seal_and_stale_buttons_are_safe(self):
        envelope_id = await self.prepared()
        await self.draft_action('seal')
        await self.callback(f'env:d:{envelope_id}:seal')
        await self.callback(f'env:d:{envelope_id}:kind:text')
        items, _ = self.store.listing(101, 'sent')
        self.assertEqual(len(items), 1)
        self.assertIsNone(self.store.draft(101))

    async def test_recipient_controls_notifications_and_no_duplicates(self):
        await self.prepared()
        await self.draft_action('seal')
        self.now += 61
        self.bot.calls.clear()
        await self.flow.notify_due(self.bot)
        self.assertEqual(self.bot.calls, [])
        self.store.set_setting(202, notifications=True)
        await self.flow.notify_due(self.bot)
        self.assertEqual(len(self.bot.calls), 1)
        self.assertEqual(self.bot.calls[0].chat_id, 202)
        await self.flow.notify_due(self.bot)
        self.assertEqual(len(self.bot.calls), 1)

    async def test_sealed_message_survives_restart(self):
        envelope_id = await self.prepared('photo')
        await self.draft_action('seal')
        envelope = EnvelopeStore(self.path).get(envelope_id, 202)
        self.assertEqual(envelope['file_id'], 'PHOTO')
        self.assertEqual(envelope['release_at'], self.now + 60)

    async def test_gift_can_be_prepared_before_partner_ever_starts(self):
        self.flow = EnvelopeFlow(self.store, {101}, None, clock=lambda: self.now)
        envelope_id = await self.prepared('circle')
        await self.draft_action('seal')
        self.assertEqual(self.store.get(envelope_id, 101)['recipient'], 0)
        self.assertFalse(any(getattr(c, 'chat_id', 101) != 101 for c in self.bot.calls))
        self.store.create_invite(101, 'offline_test_invite')
        await self.flow.accept_invite(self.message(202), self.bot, 'gift_offline_test_invite')
        self.assertEqual(self.flow.partner(101), 202)
        self.assertEqual(self.store.get(envelope_id, 202)['recipient'], 202)
        self.assertFalse(self.store.claim_invite(101, 303, 'offline_test_invite'))
        restarted = EnvelopeFlow(EnvelopeStore(self.path), {101}, None)
        self.assertEqual(restarted.partner(101), 202)

    async def test_rotated_or_invalid_gift_does_not_grant_access(self):
        self.flow = EnvelopeFlow(self.store, {101}, None, clock=lambda: self.now)
        self.store.create_invite(101, 'old')
        self.store.create_invite(101, 'new')
        await self.flow.accept_invite(self.message(202), self.bot, 'gift_old')
        self.assertNotIn(202, self.flow.allowed_ids)
        self.assertFalse(self.store.claim_invite(101, 101, 'new'))
        self.assertTrue(self.store.claim_invite(101, 202, 'new'))

    async def test_gift_button_creates_a_real_deep_link_for_owner_only(self):
        self.flow = EnvelopeFlow(self.store, {101}, None, clock=lambda: self.now)
        await self.callback('env:gift')
        output = next(c.text for c in self.bot.calls if isinstance(c, SendMessage) and '?start=gift_' in c.text)
        payload = output.split('?start=')[1].split()[0]
        self.assertLessEqual(len(payload), 64)
        self.assertTrue(self.store.claim_invite(101, 202, payload[5:]))
        self.assertFalse(any(getattr(c, 'chat_id', 101) != 101 for c in self.bot.calls))

    def test_dst_and_local_time_validation(self):
        self.assertEqual(parse_local_time('25.10.2026 09:00', ZONES['astana']),
                         datetime(2026, 10, 25, 4, tzinfo=timezone.utc).timestamp())
        for text in ['08.03.2026 02:30', '02.11.2025 01:30', '31.02.2026 10:00']:
            with self.assertRaises(ValueError):
                parse_local_time(text, ZONES['calgary'])

    def test_caption_limit_accounts_for_emoji(self):
        self.assertTrue(caption_fits('💛' * 500))
        self.assertFalse(caption_fits('💛' * 501))

    async def test_received_circle_offers_continue_retake_and_cancel(self):
        await self.callback('env:new')
        await self.draft_action('kind:circle')
        await self.flow.handle_message(self.message(video_note=VideoNote(file_id='FIRST', file_unique_id='first', duration=2, length=240)), self.bot)
        prompt = self.bot.calls[-1]
        labels = [b.text for row in prompt.reply_markup.inline_keyboard for b in row]
        self.assertIn('Подходит, дальше 💌', labels)
        self.assertIn('Переснять 🎥', labels)
        self.assertIn('Отменить конверт 🌿', labels)

    async def test_retake_preview_keeps_details_and_old_seal_cannot_send(self):
        envelope_id = await self.prepared('circle', caption='Подпись к записи 💛')
        original = self.store.draft(101)
        old_control = original['control_id']
        await self.draft_action('retake')
        self.assertEqual(self.store.draft(101)['file_id'], 'CIRCLE')
        await self.flow.handle_message(self.message(video_note=VideoNote(file_id='RETAKEN', file_unique_id='new', duration=2, length=240)), self.bot)
        self.assertEqual(self.store.draft(101)['step'], 'review-content')
        await self.draft_action('accept-media')
        changed = self.store.draft(101)
        self.assertEqual(changed['id'], envelope_id)
        self.assertEqual(changed['file_id'], 'RETAKEN')
        self.assertEqual(changed['text'], original['text'])
        self.assertEqual(changed['title'], original['title'])
        self.assertEqual(changed['release_at'], original['release_at'])
        self.assertEqual(changed['step'], 'preview')
        await self.callback(f'env:d:{old_control}:seal')
        self.assertIsNotNone(self.store.draft(101))
        self.assertEqual(self.store.listing(101, 'sent')[0], [])
        await self.draft_action('seal')
        self.assertEqual(self.store.get(envelope_id, 101)['file_id'], 'RETAKEN')

    async def test_new_circle_replaces_previous_without_retake_button(self):
        await self.prepared('circle')
        await self.flow.handle_message(self.message(video_note=VideoNote(file_id='REPLACED', file_unique_id='r', duration=2, length=240)), self.bot)
        self.assertEqual(self.store.draft(101)['step'], 'review-content')
        self.assertEqual(self.store.draft(101)['file_id'], 'REPLACED')

    async def test_retake_can_keep_previous_recording(self):
        await self.prepared('voice', caption='Сохранить подпись')
        original = self.store.draft(101)
        await self.draft_action('retake')
        await self.draft_action('keep-media')
        current = self.store.draft(101)
        self.assertEqual(current['file_id'], original['file_id'])
        self.assertEqual(current['text'], original['text'])
        self.assertEqual(current['step'], 'preview')

    async def test_cancel_draft_invalidates_all_old_buttons_and_stops_capture(self):
        await self.prepared('circle')
        old = self.store.draft(101)
        await self.draft_action('cancel')
        self.assertIsNone(self.store.draft(101))
        await self.callback(f"env:d:{old['control_id']}:seal")
        accepted = await self.flow.handle_message(self.message(video_note=VideoNote(file_id='IGNORED', file_unique_id='ignored', duration=2, length=240)), self.bot)
        self.assertFalse(accepted)
        self.assertEqual(self.store.listing(101, 'sent')[0], [])

    async def test_cancel_command_works_before_recording_and_after_pause(self):
        await self.callback('env:new')
        await self.draft_action('kind:circle')
        await self.flow.handle_message(self.message(text='/cancel'), self.bot)
        self.assertIsNone(self.store.draft(101))
        await self.prepared('circle')
        await self.callback('env:menu')
        paused = self.store.draft(101)
        self.assertFalse(paused['active'])
        await self.callback(f"env:d:{paused['control_id']}:cancel")
        self.assertIsNone(self.store.draft(101))

    async def test_cancel_sealed_envelope_hides_it_and_prevents_notifications(self):
        envelope_id = await self.prepared('circle')
        await self.draft_action('seal')
        await self.callback(f'env:cancel:{envelope_id}', 202)
        self.assertFalse(self.store.get(envelope_id, 101)['cancelled'])
        await self.callback(f'env:cancel:{envelope_id}')
        self.assertTrue(EnvelopeStore(self.path).get(envelope_id, 101)['cancelled'])
        self.assertIsNone(self.store.get(envelope_id, 202))
        self.assertEqual(self.store.listing(202, 'received')[0], [])
        self.now += 61
        self.store.set_setting(202, notifications=True)
        self.bot.calls.clear()
        await self.flow.notify_due(self.bot)
        self.assertEqual(self.bot.calls, [])
        await self.callback(f'env:open:{envelope_id}', 202)
        self.assertFalse(any(isinstance(c, SendVideoNote) for c in self.bot.calls))

    async def test_cannot_cancel_content_while_recipient_is_already_receiving_it(self):
        envelope_id = await self.prepared('voice')
        await self.draft_action('seal')
        self.now += 61
        self.bot.media_started = asyncio.Event()
        self.bot.media_continue = asyncio.Event()
        opening = asyncio.create_task(self.callback(f'env:open:{envelope_id}', 202))
        await asyncio.wait_for(self.bot.media_started.wait(), 2)
        cancelling = asyncio.create_task(self.callback(f'env:cancel:{envelope_id}'))
        self.bot.media_continue.set()
        await asyncio.wait_for(asyncio.gather(opening, cancelling), 2)
        envelope = self.store.get(envelope_id, 101)
        self.assertTrue(envelope['opened'])
        self.assertFalse(envelope['cancelled'])

    def test_existing_database_migrates_without_losing_envelopes(self):
        legacy = Path(self.directory.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as db:
            db.execute('CREATE TABLE envelopes (id TEXT PRIMARY KEY,sender INTEGER,recipient INTEGER,release_at REAL,payload TEXT,opened INTEGER DEFAULT 0,notified INTEGER DEFAULT 0,notice_lease REAL DEFAULT 0)')
            db.execute('INSERT INTO envelopes (id,sender,recipient,release_at,payload) VALUES (?,?,?,?,?)',
                       ('old', 101, 202, self.now + 60, json.dumps({'kind':'text','title':'Сохранённый','text':'Послание'})))
        migrated = EnvelopeStore(legacy)
        self.assertEqual(migrated.get('old', 101)['text'], 'Послание')
        self.assertFalse(migrated.get('old', 101)['cancelled'])
        self.assertTrue(migrated.cancel('old', 101))


if __name__ == '__main__':
    unittest.main()
