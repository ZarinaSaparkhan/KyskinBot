"""Attention flow with fake Telegram calls: no real person receives tests."""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendVoice, SendVideoNote
from aiogram.types import CallbackQuery, Chat, Message, User, Voice, VideoNote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from attention import ATTENTION_BUTTON, AttentionFlow, REPLIES, WISHES
from envelope_store import EnvelopeStore
from envelopes import EnvelopeFlow


class RecordingBot(Bot):
    def __init__(self):
        super().__init__('123456:OFFLINE_TEST_ONLY')
        self.calls = []
        self.fail_target = None
        self.fail_media = False

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, SendVoice, SendVideoNote)):
            if method.chat_id == self.fail_target or self.fail_media and isinstance(method, (SendVoice, SendVideoNote)):
                raise TelegramNetworkError(method=method, message='Offline test failure')
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type='private'))
        raise AssertionError(type(method).__name__)

    def messages(self, user):
        return [c for c in self.calls if isinstance(c, SendMessage) and c.chat_id == user]

    def media(self, user):
        return [c for c in self.calls if isinstance(c, (SendVoice, SendVideoNote)) and c.chat_id == user]


class AttentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.database = EnvelopeStore(self.path)
        self.envelopes = EnvelopeFlow(self.database, {101, 202}, None)
        self.flow = AttentionFlow(self.envelopes)
        self.bot = RecordingBot()

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.directory.cleanup()

    def message(self, user=101, text=None, **fields):
        return Message(message_id=1, date=datetime.now(timezone.utc),
                       chat=Chat(id=user, type='private'), text=text,
                       from_user=User(id=user, is_bot=False, first_name='Test', username=f'person_{user}'), **fields).as_(self.bot)

    async def callback(self, data, user=101):
        query = CallbackQuery(id='offline', chat_instance='offline', data=data,
                              from_user=self.message(user).from_user, message=self.message(user)).as_(self.bot)
        return await self.flow.handle_callback(query, self.bot)

    async def request(self, kind='circle', user=101):
        await self.callback(f'care:choose:{kind}', user)
        token = self.flow.store.draft(user)['token']
        await self.callback(f'care:send:{token}', user)
        return token

    async def recording_action(self, action, user=202):
        draft = self.flow.store.recording(user)
        await self.callback(f"care:m:{draft['control_id']}:{action}", user)

    async def upload(self, kind='circle', file_id='NATIVE', user=202):
        fields = {'video_note': VideoNote(file_id=file_id, file_unique_id=file_id, duration=2, length=240)} if kind == 'circle' else {'voice': Voice(file_id=file_id, file_unique_id=file_id, duration=2)}
        return await self.flow.handle_message(self.message(user, **fields), self.bot)

    @staticmethod
    def buttons(message):
        return [b for row in message.reply_markup.inline_keyboard for b in row]

    async def test_all_wishes_require_confirmation_and_match_approved_replies(self):
        for kind, (_, wish) in WISHES.items():
            self.bot.calls.clear()
            await self.callback(f'care:choose:{kind}')
            self.assertFalse(self.bot.messages(202))
            token = self.flow.store.draft(101)['token']
            await self.callback(f'care:send:{token}')
            delivered = self.bot.messages(202)
            self.assertEqual(len(delivered), 1)
            self.assertIn(wish, delivered[0].text)
            buttons = self.buttons(delivered[0])
            self.assertEqual([b.text for b in buttons[:3]], list(REPLIES.values()))
            self.assertTrue(all(len(b.callback_data.encode()) <= 64 for b in buttons))
            await self.callback(f'care:done:{token}')

    async def test_duplicate_confirm_and_choice_do_not_send_twice(self):
        token = await self.request()
        await self.callback(f'care:send:{token}')
        await self.callback('care:choose:voice')
        self.assertEqual(len(self.bot.messages(202)), 1)
        self.assertEqual(self.flow.store.outgoing(101, False)['id'], token)
        self.assertIsNone(self.flow.store.draft(101))

    async def test_later_then_ready_and_stale_replies_notify_only_once(self):
        token = await self.request()
        self.bot.calls.clear()
        await self.callback(f'care:reply:{token}:0:later', 202)
        self.assertIn(REPLIES['later'], self.bot.messages(101)[0].text)
        await self.callback(f'care:reply:{token}:0:planning', 202)
        self.assertEqual(len(self.bot.messages(101)), 1)
        await self.callback(f'care:reply:{token}:1:ready', 202)
        self.assertEqual(len(self.bot.messages(101)), 1)
        await self.recording_action('text')
        self.assertEqual(len(self.bot.messages(101)), 2)
        self.assertIn(REPLIES['ready'], self.bot.messages(101)[1].text)
        self.assertEqual(self.flow.store.get(token, 202)['status'], 'ready')
        self.assertIn('env:chat', [b.callback_data for b in self.buttons(self.bot.messages(202)[-1])])

    async def test_sender_cannot_impersonate_recipient_and_stranger_has_no_access(self):
        token = await self.request()
        self.bot.calls.clear()
        await self.callback(f'care:reply:{token}:0:ready')
        self.assertEqual(self.flow.store.get(token, 101)['status'], 'pending')
        await self.callback(f'care:view:{token}:receiver')
        self.assertFalse(any(b.callback_data.startswith('care:reply:') for b in self.buttons(self.bot.messages(101)[-1])))
        await self.callback(f'care:reply:{token}:0:ready', 303)
        self.assertFalse(self.bot.messages(202))
        self.assertIsNone(self.flow.access(token, 303))

    async def test_home_keeps_request_and_finished_request_rejects_old_reply(self):
        token = await self.request()
        await self.callback('care:home')
        self.assertEqual(self.flow.store.outgoing(101, False)['id'], token)
        await self.callback(f'care:done:{token}')
        self.assertIsNone(self.flow.store.outgoing(101, False))
        self.bot.calls.clear()
        await self.callback(f'care:reply:{token}:0:ready', 202)
        self.assertFalse(self.bot.messages(101))
        self.assertEqual(self.flow.store.get(token, 101)['status'], 'done')
        self.assertNotEqual(await self.request('voice'), token)

    async def test_both_partners_can_request_independently(self):
        first = await self.request()
        second = await self.request('voice', 202)
        self.assertNotEqual(first, second)
        self.assertEqual(self.flow.store.incoming(101)[0]['id'], second)
        self.assertEqual(self.flow.store.incoming(202)[0]['id'], first)

    async def test_request_and_response_survive_restart(self):
        token = await self.request()
        await self.callback(f'care:reply:{token}:0:planning', 202)
        self.flow = AttentionFlow(EnvelopeFlow(EnvelopeStore(self.path), {101, 202}, None))
        self.assertEqual(self.flow.store.outgoing(101, False)['status'], 'planning')
        self.bot.calls.clear()
        await self.callback(f'care:view:{token}:sender')
        self.assertIn(REPLIES['planning'], self.bot.messages(101)[0].text)
        self.assertFalse(self.bot.messages(202))

    async def test_demo_stays_private_and_never_becomes_a_real_request(self):
        self.envelopes = EnvelopeFlow(self.database, {101}, None)
        self.flow = AttentionFlow(self.envelopes)
        token = await self.request()
        await self.callback(f'care:view:{token}:receiver')
        self.assertEqual([b.text for b in self.buttons(self.bot.messages(101)[-1])][:3], list(REPLIES.values()))
        await self.callback(f'care:reply:{token}:0:ready')
        await self.recording_action('text', 101)
        await self.callback(f'care:demo-chat:{token}')
        self.assertTrue(all(c.chat_id == 101 for c in self.bot.calls if isinstance(c, SendMessage)))
        self.database.create_invite(101, 'offline_invite')
        await self.envelopes.accept_invite(self.message(202), self.bot, 'gift_offline_invite')
        self.assertFalse(self.flow.store.incoming(202))
        self.assertIsNone(self.flow.access(token, 202))
        self.assertIsNone(self.flow.store.outgoing(101, False))
        self.assertTrue(self.flow.store.get(token, 101)['demo'])

    async def test_attention_menu_pauses_envelope_without_using_button_as_caption(self):
        self.database.save_draft(101, {'id':'draft', 'active':True, 'step':'caption', 'kind':'circle', 'file_id':'NOTE'})
        self.assertTrue(await self.flow.handle_message(self.message(text=ATTENTION_BUTTON), self.bot))
        draft = self.database.draft(101)
        self.assertFalse(draft['active'])
        self.assertEqual(draft['file_id'], 'NOTE')
        self.assertNotIn('caption', draft)
        self.assertIs(self.flow.user_locks, self.envelopes.locks)

    async def test_failed_delivery_can_be_retried_without_duplicating_request(self):
        self.bot.fail_target = 202
        token = await self.request()
        self.assertEqual(self.flow.store.get(token, 101)['delivery'], 'failed')
        self.bot.fail_target = None
        self.bot.calls.clear()
        await self.callback(f'care:retry:{token}')
        await self.callback(f'care:retry:{token}')
        self.assertEqual(len(self.bot.messages(202)), 1)
        self.assertEqual(self.flow.store.get(token, 101)['delivery'], 'sent')

    async def test_failed_reply_can_be_retried_without_changing_the_answer(self):
        token = await self.request()
        self.bot.fail_target = 101
        await self.callback(f'care:reply:{token}:0:later', 202)
        self.assertEqual(self.flow.store.get(token, 202)['reply_delivery'], 'failed')
        self.bot.fail_target = None
        self.bot.calls.clear()
        await self.callback(f'care:retry-reply:{token}', 202)
        await self.callback(f'care:retry-reply:{token}', 202)
        self.assertEqual(len(self.bot.messages(101)), 1)
        self.assertEqual(self.flow.store.get(token, 202)['revision'], 1)

    async def test_home_discards_unsent_preview_and_old_confirm_has_no_effect(self):
        await self.callback('care:choose:circle')
        token = self.flow.store.draft(101)['token']
        await self.callback('care:home')
        await self.callback(f'care:send:{token}')
        self.assertIsNone(self.flow.store.outgoing(101, False))
        self.assertFalse(self.bot.messages(202))

    async def test_native_recordings_wait_for_confirmation_and_send_exactly_chosen_file(self):
        for kind, method in [('voice', SendVoice), ('circle', SendVideoNote)]:
            token = await self.request(kind)
            self.bot.calls.clear()
            await self.callback(f'care:reply:{token}:0:ready', 202)
            self.assertEqual(self.flow.store.get(token, 101)['status'], 'pending')
            self.assertFalse(self.bot.messages(101))
            await self.upload(kind, f'{kind}_ORIGINAL')
            self.assertFalse(self.bot.media(101))
            draft = self.flow.store.recording(202)
            old_send = f"care:m:{draft['control_id']}:send"
            await self.callback(old_send, 202)
            await self.callback(old_send, 202)
            native = self.bot.media(101)
            self.assertEqual(len(native), 1)
            self.assertIsInstance(native[0], method)
            self.assertEqual(getattr(native[0], 'voice' if kind == 'voice' else 'video_note'), f'{kind}_ORIGINAL')
            self.assertEqual(len(self.bot.messages(101)), 1)
            self.assertEqual(self.flow.store.get(token, 101)['media_delivery'], 'sent')
            self.assertIsNone(self.flow.store.recording(202))
            await self.callback(f'care:done:{token}')

    async def test_retake_rejects_old_confirmation_and_sends_only_replacement(self):
        token = await self.request()
        await self.callback(f'care:reply:{token}:0:ready', 202)
        await self.upload(file_id='FIRST')
        old_send = f"care:m:{self.flow.store.recording(202)['control_id']}:send"
        await self.recording_action('retake')
        self.assertEqual(self.flow.store.recording(202)['file_id'], 'FIRST')
        await self.callback(old_send, 202)
        self.assertFalse(self.bot.media(101))
        await self.recording_action('keep')
        self.assertFalse(self.flow.store.recording(202)['retaking'])
        await self.recording_action('retake')
        await self.upload(file_id='REPLACEMENT')
        await self.callback(old_send, 202)
        self.assertFalse(self.bot.media(101))
        await self.recording_action('send')
        self.assertEqual(self.bot.media(101)[0].video_note, 'REPLACEMENT')

    async def test_only_answer_discards_recording_and_can_add_one_later(self):
        token = await self.request('voice')
        await self.callback(f'care:reply:{token}:0:ready', 202)
        await self.upload('voice', 'NOT_FOR_DELIVERY')
        self.bot.calls.clear()
        await self.recording_action('text')
        self.assertIn(REPLIES['ready'], self.bot.messages(101)[0].text)
        self.assertFalse(self.bot.media(101))
        request = self.flow.store.get(token, 202)
        await self.callback(f"care:record:{token}:{request['revision']}", 202)
        await self.upload('voice', 'AFTER_READY')
        await self.recording_action('send')
        self.assertEqual(len(self.bot.messages(101)), 1)
        self.assertEqual(self.bot.media(101)[0].voice, 'AFTER_READY')

    async def test_later_is_immediate_and_native_answer_can_follow(self):
        token = await self.request('voice')
        self.bot.calls.clear()
        await self.callback(f'care:reply:{token}:0:later', 202)
        self.assertEqual(len(self.bot.messages(101)), 1)
        self.assertIsNone(self.flow.store.recording(202))
        await self.callback(f'care:reply:{token}:1:ready', 202)
        await self.upload('voice')
        self.assertEqual(self.flow.store.get(token, 101)['status'], 'later')
        self.assertEqual(len(self.bot.messages(101)), 1)
        await self.recording_action('send')
        self.assertEqual(len(self.bot.messages(101)), 2)
        self.assertEqual(len(self.bot.media(101)), 1)

    async def test_wrong_native_format_never_advances(self):
        token = await self.request('voice')
        await self.callback(f'care:reply:{token}:0:ready', 202)
        await self.upload('circle')
        self.assertNotIn('file_id', self.flow.store.recording(202))
        await self.recording_action('send')
        self.assertEqual(self.flow.store.get(token, 101)['status'], 'pending')
        self.assertFalse(self.bot.media(101))

    async def test_cancel_recording_preserves_request_and_sends_nothing(self):
        token = await self.request()
        for action in ['back', '/cancel']:
            await self.callback(f'care:reply:{token}:0:ready', 202)
            await self.upload()
            self.bot.calls.clear()
            if action == '/cancel':
                self.assertTrue(await self.flow.handle_message(self.message(202, '/cancel'), self.bot))
            else:
                await self.recording_action(action)
            self.assertIsNone(self.flow.store.recording(202))
            self.assertEqual(self.flow.store.get(token, 101)['status'], 'pending')
            self.assertFalse(self.bot.messages(101))
            self.assertFalse(self.bot.media(101))

    async def test_navigation_pauses_recording_and_resume_preserves_file(self):
        token = await self.request()
        await self.callback(f'care:reply:{token}:0:ready', 202)
        await self.upload(file_id='PAUSED')
        old_send = f"care:m:{self.flow.store.recording(202)['control_id']}:send"
        self.assertFalse(await self.flow.handle_message(self.message(202, 'Откроешь потом 💌'), self.bot))
        self.assertFalse(self.flow.store.recording(202)['active'])
        await self.callback(old_send, 202)
        self.assertFalse(self.bot.media(101))
        await self.callback(f'care:record:{token}:0', 202)
        self.assertEqual(self.flow.store.recording(202)['file_id'], 'PAUSED')
        await self.recording_action('send')
        self.assertEqual(self.bot.media(101)[0].video_note, 'PAUSED')

    async def test_finishing_request_invalidates_recording_confirmation(self):
        token = await self.request()
        await self.callback(f'care:reply:{token}:0:ready', 202)
        await self.upload()
        old_send = f"care:m:{self.flow.store.recording(202)['control_id']}:send"
        await self.callback(f'care:done:{token}')
        self.bot.calls.clear()
        await self.callback(old_send, 202)
        self.assertFalse(self.bot.media(101))
        self.assertIsNone(self.flow.store.recording(202))

    async def test_recording_survives_restart_and_cannot_be_confirmed_by_sender(self):
        token = await self.request('voice')
        await self.callback(f'care:reply:{token}:0:ready', 202)
        await self.upload('voice', 'PERSISTENT')
        send = f"care:m:{self.flow.store.recording(202)['control_id']}:send"
        self.flow = AttentionFlow(EnvelopeFlow(EnvelopeStore(self.path), {101, 202}, None))
        self.bot.calls.clear()
        await self.callback(send)
        await self.callback(send, 303)
        self.assertFalse(self.bot.media(101))
        await self.callback(send, 202)
        self.assertEqual(self.bot.media(101)[0].voice, 'PERSISTENT')

    async def test_partial_delivery_retries_only_failed_media(self):
        token = await self.request('voice')
        await self.callback(f'care:reply:{token}:0:ready', 202)
        await self.upload('voice')
        self.bot.fail_media = True
        self.bot.calls.clear()
        await self.recording_action('send')
        self.assertEqual(len(self.bot.messages(101)), 1)
        self.assertEqual(self.flow.store.get(token, 202)['media_delivery'], 'failed')
        self.bot.fail_media = False
        self.bot.calls.clear()
        await self.callback(f'care:retry-reply:{token}', 202)
        await self.callback(f'care:retry-reply:{token}', 202)
        self.assertEqual(len(self.bot.media(101)), 1)
        self.assertFalse(self.bot.messages(101))
        self.assertEqual(self.flow.store.get(token, 202)['media_delivery'], 'sent')

    async def test_demo_native_recording_is_delivered_only_to_owner(self):
        self.envelopes = EnvelopeFlow(self.database, {101}, None)
        self.flow = AttentionFlow(self.envelopes)
        token = await self.request()
        await self.callback(f'care:reply:{token}:0:ready')
        await self.upload(user=101)
        await self.recording_action('send', 101)
        self.assertEqual(len(self.bot.media(101)), 1)
        self.assertTrue(all(c.chat_id == 101 for c in self.bot.calls if isinstance(c, (SendMessage, SendVoice, SendVideoNote))))
        self.assertTrue(self.flow.store.get(token, 101)['demo'])

    async def test_failed_text_holds_attachment_until_reply_retry(self):
        token = await self.request()
        await self.callback(f'care:reply:{token}:0:ready', 202)
        await self.upload()
        self.bot.fail_target = 101
        self.bot.calls.clear()
        await self.recording_action('send')
        self.assertFalse(self.bot.media(101))
        self.assertEqual(self.flow.store.get(token, 202)['reply_delivery'], 'failed')
        self.bot.fail_target = None
        self.bot.calls.clear()
        await self.callback(f'care:retry-reply:{token}', 202)
        self.assertEqual(len(self.bot.messages(101)), 1)
        self.assertEqual(len(self.bot.media(101)), 1)

    async def test_old_attention_database_migration_preserves_requests(self):
        with self.database.connect() as db:
            db.executescript('''DROP TABLE attention_requests;
                CREATE TABLE attention_requests (
                    id TEXT PRIMARY KEY, sender INTEGER, recipient INTEGER, kind TEXT,
                    demo INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', revision INTEGER DEFAULT 0,
                    delivery TEXT DEFAULT 'pending', reply_delivery TEXT DEFAULT 'none');
                INSERT INTO attention_requests VALUES ('legacy',101,202,'voice',0,'later',1,'sent','sent');''')
        self.flow = AttentionFlow(EnvelopeFlow(EnvelopeStore(self.path), {101, 202}, None))
        request = self.flow.store.get('legacy', 202)
        self.assertEqual(request['status'], 'later')
        self.assertEqual(request['revision'], 1)
        self.assertIsNone(request['reply_file_id'])
        self.assertEqual(request['media_delivery'], 'none')
        await self.callback('care:reply:legacy:1:ready', 202)
        await self.upload('voice', 'AFTER_MIGRATION')
        await self.recording_action('send')
        self.assertEqual(self.bot.media(101)[0].voice, 'AFTER_MIGRATION')


if __name__ == '__main__':
    unittest.main()
