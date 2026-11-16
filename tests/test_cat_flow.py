"""Проверки без сети и без сообщений в настоящий Telegram."""
import asyncio
import importlib
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage, SendPhoto, SendVideo, SendAnimation, SendVoice, SendVideoNote
from aiogram.types import CallbackQuery, Chat, Message, User, Update, VideoNote

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = tempfile.TemporaryDirectory(prefix='kyskin-bot-test-')
R = Path(RUNTIME.name)
shutil.copy(ROOT/'content_store.py', R/'content_store.py')
shutil.copy(ROOT/'envelope_store.py', R/'envelope_store.py')
shutil.copy(ROOT/'envelopes.py', R/'envelopes.py')
shutil.copy(ROOT/'attention.py', R/'attention.py')
shutil.copy(ROOT/'attention_store.py', R/'attention_store.py')
shutil.copy(ROOT/'bot.py', R/'bot.py')
(R/'media').mkdir()
for i in range(12):
    (R/'media'/f'photo-{i}.jpg').write_bytes(b'fixture')
sys.path.insert(0, str(R))
with patch.dict(os.environ, {'BOT_TOKEN':'123456:TEST_ONLY_NO_NETWORK', 'ALLOWED_USER_IDS':'101,202', 'CONTACT_USERNAME':''}):
    store = importlib.import_module('content_store')
    spec = importlib.util.spec_from_file_location('tested_bot', R/'bot.py')
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class RecordingBot(Bot):
    def __init__(self):
        super().__init__('123456:TEST_ONLY_NO_NETWORK')
        self.calls = []
        self.counter = 100
        self.fail_photo = False
        self.fail_video = False

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
            return True
        if isinstance(method, SendPhoto) and self.fail_photo:
            raise RuntimeError('Simulated delivery failure')
        if isinstance(method, SendVideo) and self.fail_video:
            raise RuntimeError('Simulated video failure')
        if isinstance(method, (SendMessage, SendPhoto, SendVideo, SendAnimation, SendVoice, SendVideoNote)):
            self.counter += 1
            return Message(message_id=self.counter, date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type='private'), text=getattr(method, 'text', None))
        raise AssertionError(f'Unexpected method {type(method).__name__}')

    @property
    def photos(self):
        return [c for c in self.calls if isinstance(c, SendPhoto)]


def message(bot, text='', chat_id=101, message_id=1, chat_type='private'):
    return Message(message_id=message_id, date=datetime.now(timezone.utc),
                   chat=Chat(id=chat_id, type=chat_type), text=text,
                   from_user=User(id=chat_id, is_bot=False, first_name='Test')).as_(bot)


def callback(bot, data, message_id, chat_id=101, user_id=None):
    return CallbackQuery(id=f'cb-{len(bot.calls)}', chat_instance='test', data=data,
                         from_user=User(id=user_id or chat_id, is_bot=False, first_name='Test'),
                         message=message(bot, chat_id=chat_id, message_id=message_id)).as_(bot)


class ContentTests(unittest.TestCase):
    def setUp(self):
        store.HISTORY_FILE = R/'content-history.sqlite3'
        store.HISTORY_FILE.unlink(missing_ok=True)

    def test_media_cycle_and_recent_boundary(self):
        values = store.list_media()
        chosen = []
        for _ in values:
            value = store.choose_fresh(101, 'cat_media', values)
            self.assertNotIn(value, chosen)
            chosen.append(value)
            store.mark_sent(101, 'cat_media', value)
        following = store.choose_fresh(101, 'cat_media', values)
        self.assertNotIn(following, chosen[-3:])
        self.assertIn(following, values)
        self.assertEqual(store.choose_fresh(202, 'cat_media', [chosen[0]]), chosen[0])
        self.assertEqual(store.choose_fresh(101, 'cat_videos', [chosen[0]]), chosen[0])

    def test_existing_history_is_preserved(self):
        store.mark_sent(101, 'cat_media', 'old-photo')
        store.commit_delivery(101, [('cat_media','new-photo'), ('mood_content:good','fact-1')], 8, 'good', 'content')
        with sqlite3.connect(store.HISTORY_FILE) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sent_history').fetchone()[0], 3)
        self.assertEqual(store.choose_fresh(101, 'cat_media', ['old-photo','new-photo','unseen-photo']), 'unseen-photo')
        self.assertEqual(store.get_active_view(101)['message_id'], 8)

    def test_media_filters_and_removed_files(self):
        (R/'media'/'movie.mp4').write_bytes(b'fixture')
        (R/'media'/'ignored.txt').write_bytes(b'fixture')
        all_photos = store.list_media(extensions=store.PHOTO_EXTENSIONS)
        self.assertEqual(len(all_photos), 12)
        self.assertTrue(all(Path(p).suffix == '.jpg' for p in all_photos))
        removed = R/all_photos[0]
        removed.unlink()
        self.assertNotIn(all_photos[0], store.list_media(extensions=store.PHOTO_EXTENSIONS))
        removed.write_bytes(b'fixture')
        (R/'media'/'movie.mp4').unlink()
        (R/'media'/'ignored.txt').unlink()

    def test_hidden_import_files_are_not_sent(self):
        hidden = R/'media'/'.preparing-test'
        hidden.mkdir()
        try:
            (hidden/'prepared.mp4').write_bytes(b'incomplete')
            (hidden/'prepared.jpg').write_bytes(b'incomplete')
            self.assertEqual(len(store.list_media()), 12)
        finally:
            shutil.rmtree(hidden)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        store.HISTORY_FILE = R/'flow-history.sqlite3'
        store.HISTORY_FILE.unlink(missing_ok=True)
        app.chat_locks.clear()
        self.bot = RecordingBot()

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def test_welcome_circle_setup_and_first_entry_through_dispatcher(self):
        database=app.EnvelopeStore(R/'welcome-integration.sqlite3')
        database.create_invite(101,'offline_welcome')
        flow=app.EnvelopeFlow(database,{101},app.main_keyboard)
        care=app.AttentionFlow(flow)
        with patch.object(app,'envelopes',flow),patch.object(app,'attention',care),patch.object(app,'allowed_user_ids',flow.allowed_ids):
            await app.dp.feed_update(self.bot,Update(update_id=9880,callback_query=callback(self.bot,'env:welcome:record',1)))
            native=message(self.bot).model_copy(update={'text':None,'video_note':VideoNote(
                file_id='HELLO_CIRCLE',file_unique_id='hello',duration=2,length=240)})
            await app.dp.feed_update(self.bot,Update(update_id=9881,message=native))
            draft=database.gift_draft(101)
            self.assertEqual(draft['step'],'review')
            self.assertIsNone(database.gift_circle(101))
            await app.dp.feed_update(self.bot,Update(update_id=9882,callback_query=callback(
                self.bot,f"env:welcome:d:{draft['control_id']}:save",2)))
            self.assertEqual(database.gift_circle(101),'HELLO_CIRCLE')
            self.assertFalse(any(getattr(c,'chat_id',None)==202 for c in self.bot.calls))
            await app.dp.feed_update(self.bot,Update(update_id=9883,message=message(
                self.bot,'/start gift_offline_welcome',202)))
            circle=[c for c in self.bot.calls if isinstance(c,SendVideoNote) and c.chat_id==202]
            self.assertEqual(len(circle),1)
            self.assertEqual(circle[0].video_note,'HELLO_CIRCLE')

    async def test_gift_start_and_native_recording_through_dispatcher(self):
        invite_store = app.EnvelopeStore(R/'gift-integration.sqlite3')
        invite_store.create_invite(101, 'offline_invite')
        flow = app.EnvelopeFlow(invite_store, {101}, app.main_keyboard)
        with patch.object(app, 'envelopes', flow), patch.object(app, 'allowed_user_ids', flow.allowed_ids):
            await app.dp.feed_update(self.bot, Update(update_id=9870, message=message(self.bot, '/start gift_offline_invite', 202)))
            self.assertEqual(flow.partner(101), 202)
            invite_store.save_draft(202, {'id':'offline_draft', 'step':'content', 'active':True, 'kind':'circle', 'text':''})
            native = message(self.bot, chat_id=202).model_copy(update={'text':None, 'video_note':VideoNote(file_id='NATIVE', file_unique_id='unique', duration=2, length=240)})
            await app.dp.feed_update(self.bot, Update(update_id=9871, message=native))
            self.assertEqual(invite_store.draft(202)['step'], 'review-content')
            self.assertEqual(invite_store.draft(202)['file_id'], 'NATIVE')
            owner_messages=[c for c in self.bot.calls if getattr(c,'chat_id',None)==101]
            self.assertEqual(len(owner_messages),1)
            self.assertEqual(owner_messages[0].text,'Он уже туть 🤍 Твоя половинка подключилась')

    async def open_cat(self, chat_id=101):
        await app.main_menu(message(self.bot, app.CAT_BUTTON, chat_id), self.bot)
        return store.get_active_view(chat_id)['message_id']

    async def test_attention_button_and_callbacks_through_dispatcher(self):
        attention_store = app.EnvelopeStore(R/'attention-integration.sqlite3')
        flow = app.EnvelopeFlow(attention_store, {101}, app.main_keyboard)
        attention = app.AttentionFlow(flow)
        with patch.object(app, 'envelopes', flow), patch.object(app, 'attention', attention), patch.object(app, 'allowed_user_ids', flow.allowed_ids):
            attention_store.save_draft(101, {'id':'draft', 'active':True, 'step':'caption', 'kind':'circle', 'file_id':'NOTE'})
            await app.dp.feed_update(self.bot, Update(update_id=9880, message=message(self.bot, app.ATTENTION_BUTTON)))
            self.assertFalse(attention_store.draft(101)['active'])
            await app.dp.feed_update(self.bot, Update(update_id=9881, callback_query=callback(self.bot, 'care:choose:circle', 1)))
            token = attention.store.draft(101)['token']
            await app.dp.feed_update(self.bot, Update(update_id=9882, callback_query=callback(self.bot, f'care:send:{token}', 1)))
            self.assertTrue(attention.store.get(token, 101)['demo'])
            await app.dp.feed_update(self.bot, Update(update_id=9883, callback_query=callback(self.bot, f'care:reply:{token}:0:ready', 1)))
            native = message(self.bot).model_copy(update={'text':None, 'video_note':VideoNote(file_id='CARE_NATIVE', file_unique_id='care', duration=2, length=240)})
            await app.dp.feed_update(self.bot, Update(update_id=9884, message=native))
            recording = attention.store.recording(101)
            self.assertEqual(recording['file_id'], 'CARE_NATIVE')
            self.assertEqual(attention_store.draft(101)['file_id'], 'NOTE')
            await app.dp.feed_update(self.bot, Update(update_id=9885, callback_query=callback(self.bot, f"care:m:{recording['control_id']}:send", 1)))
            self.assertEqual([c.video_note for c in self.bot.calls if isinstance(c, SendVideoNote)], ['CARE_NATIVE'])
            self.assertTrue(all(c.chat_id == 101 for c in self.bot.calls if isinstance(c, SendMessage)))

    async def test_direct_cat_entry_more_and_four_final_wishes(self):
        self.assertEqual([[b.text for b in row] for row in app.main_keyboard.keyboard], [['Хочу к тебе 💛'], ['Откроешь потом 💌'], ['Нужен котик']])
        used_paths = set()
        for _ in range(4):
            await app.dp.feed_update(self.bot, Update(update_id=9900 + len(self.bot.calls), message=message(self.bot, app.CAT_BUTTON)))
            photo1 = self.bot.photos[-1]
            self.assertNotIn(photo1.photo.path, used_paths)
            used_paths.add(photo1.photo.path)
            self.assertEqual(photo1.caption, 'Воть')
            labels = [b.text for row in photo1.reply_markup.inline_keyboard for b in row]
            self.assertEqual(labels, ['Ещё 🐾', 'Усё 💛'])
            active = store.get_active_view(101)['message_id']
            await app.handle_choice(callback(self.bot, 'cat:more', active), self.bot)
            photo2 = self.bot.photos[-1]
            self.assertEqual(photo2.caption, 'Воть ещё')
            self.assertNotIn(photo2.photo.path, used_paths)
            used_paths.add(photo2.photo.path)
            active = store.get_active_view(101)['message_id']
            await app.handle_choice(callback(self.bot, 'cat:finish', active), self.bot)
            final = self.bot.photos[-1]
            self.assertIn(final.caption, app.FINAL_CAT_CAPTIONS)
            self.assertIsNone(final.reply_markup)
            self.assertNotIn(final.photo.path, used_paths)
            used_paths.add(final.photo.path)
            self.assertEqual(store.get_active_view(101)['stage'], 'finished')
        self.assertEqual(len(self.bot.photos), 12)
        finals = [p.caption for p in self.bot.photos if p.reply_markup is None]
        self.assertEqual(set(finals), set(app.FINAL_CAT_CAPTIONS))
        self.assertFalse(any(isinstance(c, SendMessage) for c in self.bot.calls))
        with sqlite3.connect(store.HISTORY_FILE) as db:
            self.assertEqual({r[0] for r in db.execute('SELECT kind FROM sent_history')}, {'cat_media', 'final_cat_captions'})

    async def test_fast_double_click_and_stale_buttons(self):
        first = await self.open_cat()
        active = store.get_active_view(101)['message_id']
        await asyncio.gather(app.handle_choice(callback(self.bot, 'cat:more', active), self.bot),
                             app.handle_choice(callback(self.bot, 'cat:more', active), self.bot))
        self.assertEqual(len(self.bot.photos), 2)
        await app.handle_choice(callback(self.bot, 'cat:finish', active), self.bot)
        await app.handle_choice(callback(self.bot, 'mood:bad', first), self.bot)
        self.assertEqual(len(self.bot.photos), 2)
        self.assertTrue(any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in self.bot.calls))

    async def test_failure_does_not_mark_photo_or_wish_as_sent(self):
        self.bot.fail_photo = True
        with self.assertRaisesRegex(RuntimeError, 'Simulated'):
            await self.open_cat()
        self.assertIsNone(store.get_active_view(101))
        with sqlite3.connect(store.HISTORY_FILE) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sent_history').fetchone()[0], 0)
        self.bot.fail_photo = False
        active = await self.open_cat()
        self.bot.fail_photo = True
        with self.assertRaisesRegex(RuntimeError, 'Simulated'):
            await app.handle_choice(callback(self.bot, 'cat:finish', active), self.bot)
        self.assertEqual(store.get_active_view(101)['stage'], 'cat_content')
        with sqlite3.connect(store.HISTORY_FILE) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sent_history').fetchone()[0], 1)

    async def test_restart_keeps_active_view_and_history(self):
        await self.open_cat()
        previous = store.get_active_view(101).copy()
        db = store.HISTORY_FILE
        importlib.reload(store)
        store.HISTORY_FILE = db
        app.chat_locks.clear()
        self.assertEqual(store.get_active_view(101), previous)
        await app.handle_choice(callback(self.bot, 'cat:more', previous['message_id']), self.bot)
        self.assertNotEqual(self.bot.photos[0].photo.path, self.bot.photos[1].photo.path)
        self.assertEqual(self.bot.photos[1].caption, 'Воть ещё')

    async def test_privacy_and_myid(self):
        await app.main_menu(message(self.bot, app.CAT_BUTTON, chat_id=303), self.bot)
        await app.main_menu(message(self.bot, app.CAT_BUTTON, chat_id=101, chat_type='group'), self.bot)
        await app.handle_choice(callback(self.bot, 'cat:more', 1, chat_id=303), self.bot)
        self.assertEqual(len(self.bot.photos), 0)
        await app.show_my_id(message(self.bot, '/myid', chat_id=303))
        self.assertEqual(self.bot.calls[-1].text, 'Твой Telegram ID: 303')
        self.assertIsNone(store.get_active_view(303))

    async def test_video_animation_routing_and_photo_finish(self):
        with patch.object(app, 'list_media', return_value=['media/clip.mp4']):
            await self.open_cat()
        video = next(c for c in self.bot.calls if isinstance(c, SendVideo))
        self.assertTrue(video.supports_streaming)
        self.assertEqual(video.caption, 'Воть')
        active = store.get_active_view(101)['message_id']
        with patch.object(app, 'list_media', return_value=['media/clip.gif']):
            await app.handle_choice(callback(self.bot, 'cat:more', active), self.bot)
        animation = next(c for c in self.bot.calls if isinstance(c, SendAnimation))
        self.assertTrue(animation.caption.startswith('Воть ещё'))
        active = store.get_active_view(101)['message_id']
        await app.handle_choice(callback(self.bot, 'cat:finish', active), self.bot)
        self.assertIn(self.bot.photos[-1].caption, app.FINAL_CAT_CAPTIONS)
        with sqlite3.connect(store.HISTORY_FILE) as conn:
            kinds = {row[0] for row in conn.execute('SELECT kind FROM sent_history')}
        self.assertTrue({'cat_media', 'cat_videos', 'cat_gifs'}.issubset(kinds))

    async def test_video_failure_preserves_existing_view_and_history(self):
        first = await self.open_cat()
        self.bot.fail_video = True
        with patch.object(app, 'list_media', return_value=['media/clip.mp4']):
            with self.assertRaisesRegex(RuntimeError, 'Simulated video'):
                await app.handle_choice(callback(self.bot, 'cat:more', first), self.bot)
        self.assertEqual(store.get_active_view(101)['message_id'], first)
        with sqlite3.connect(store.HISTORY_FILE) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sent_history').fetchone()[0], 1)

    async def test_empty_collection_does_not_close_existing_gallery(self):
        first = await self.open_cat()
        with patch.object(app, 'list_media', return_value=[]):
            await app.handle_choice(callback(self.bot, 'cat:finish', first), self.bot)
        self.assertEqual(store.get_active_view(101)['message_id'], first)
        self.assertEqual(len(self.bot.photos), 1)
        self.assertTrue(any(isinstance(c, SendMessage) and 'Котик пока не загрузился' in c.text for c in self.bot.calls))

    async def test_old_mood_buttons_do_not_send_legacy_content(self):
        first = await self.open_cat()
        for data in ['mood:good', 'mood:neutral', 'more:bad', 'finish:good']:
            await app.handle_choice(callback(self.bot, data, first), self.bot)
        self.assertEqual(len(self.bot.photos), 1)
        self.assertEqual(store.get_active_view(101)['message_id'], first)

    async def test_cat_callbacks_pause_native_drafts_from_other_sections(self):
        database = app.EnvelopeStore(R/'cat-navigation.sqlite3')
        flow = app.EnvelopeFlow(database, {101, 202}, app.main_keyboard)
        attention = app.AttentionFlow(flow)
        with patch.object(app, 'envelopes', flow), patch.object(app, 'attention', attention):
            first = await self.open_cat()
            database.save_draft(101, {'id':'paused-envelope', 'active':True, 'kind':'circle', 'step':'content'})
            await app.handle_choice(callback(self.bot, 'cat:more', first), self.bot)
            self.assertFalse(database.draft(101)['active'])
            attention.store.save_draft(202, 'navigation-request', 'voice')
            attention.store.create(202, 101, 'navigation-request', False)
            await app.handle_choice(callback(self.bot, 'care:reply:navigation-request:0:ready', 1), self.bot)
            self.assertTrue(attention.store.recording(101)['active'])
            active = store.get_active_view(101)['message_id']
            await app.handle_choice(callback(self.bot, 'cat:more', active), self.bot)
            self.assertFalse(attention.store.recording(101)['active'])
            self.assertFalse(database.draft(101)['active'])
            self.assertFalse(any(isinstance(c, (SendVoice, SendVideoNote)) for c in self.bot.calls))


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        RUNTIME.cleanup()
