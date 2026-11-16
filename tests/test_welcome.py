"""Welcome configuration and first-entry delivery, with fake Telegram only."""
import asyncio
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramForbiddenError
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendVideoNote
from aiogram.types import CallbackQuery, Chat, Message, User, VideoNote, Voice

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from envelope_store import EnvelopeStore
from envelopes import EnvelopeFlow, GIFT_GREETING, GIFT_JOIN_NOTICE


class FakeBot(Bot):
    def __init__(self):
        super().__init__('123456:OFFLINE_WELCOME_TEST')
        self.calls = []
        self.fail_circle = False
        self.fail_text = False
        self.fail_notice = False
        self.block_notice = False

    async def __call__(self, method, request_timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, SendVideoNote)):
            if isinstance(method, SendMessage) and method.chat_id == 101 and method.text == GIFT_JOIN_NOTICE:
                if self.block_notice:
                    raise TelegramForbiddenError(method=method, message='Offline blocked')
                if self.fail_notice:
                    raise TelegramNetworkError(method=method, message='Offline notice failure')
            if method.chat_id == 202 and ((self.fail_circle and isinstance(method, SendVideoNote))
                                          or (self.fail_text and isinstance(method, SendMessage))):
                raise TelegramNetworkError(method=method, message='Offline simulated failure')
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type='private'))
        raise AssertionError(type(method).__name__)


class WelcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'state.sqlite3'
        self.store = EnvelopeStore(self.path)
        self.now = 1800000000.0
        self.flow = EnvelopeFlow(self.store, {101}, None, clock=lambda: self.now)
        self.bot = FakeBot()

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.directory.cleanup()

    def message(self, user=101, text=None, **fields):
        return Message(message_id=1, date=datetime.now(timezone.utc), text=text,
                       chat=Chat(id=user, type='private'),
                       from_user=User(id=user, is_bot=False, first_name='Test'), **fields).as_(self.bot)

    async def callback(self, data, user=101):
        return await self.flow.handle_callback(CallbackQuery(id='offline', chat_instance='offline', data=data,
            from_user=self.message(user).from_user, message=self.message(user)).as_(self.bot), self.bot)

    async def action(self, name):
        draft = self.store.gift_draft(101)
        await self.callback(f"env:welcome:d:{draft['control_id']}:{name}")

    async def upload(self, file_id='CIRCLE'):
        await self.flow.handle_message(self.message(video_note=VideoNote(file_id=file_id,
            file_unique_id=file_id, duration=2, length=240)), self.bot)

    async def configured(self, file_id='SAVED'):
        await self.callback('env:welcome:record')
        await self.upload(file_id)
        await self.action('save')

    async def join(self):
        self.store.create_invite(101, 'offline_invite')
        await self.flow.accept_invite(self.message(202), self.bot, 'gift_offline_invite')

    def sent_to(self, user):
        return [c for c in self.bot.calls if isinstance(c, (SendMessage, SendVideoNote)) and c.chat_id == user]

    async def test_circle_requires_confirmation_and_previews_only_to_owner(self):
        await self.callback('env:welcome:record')
        await self.upload()
        self.assertIsNone(self.store.gift_circle(101))
        self.assertTrue(any(isinstance(c, SendVideoNote) for c in self.sent_to(101)))
        self.assertEqual(self.sent_to(202), [])
        await self.action('save')
        self.assertEqual(self.store.gift_circle(101), 'CIRCLE')
        self.assertIsNone(self.store.gift_draft(101))
        self.assertEqual(self.sent_to(202), [])

    async def test_retake_cancel_and_stale_buttons_preserve_saved_circle(self):
        await self.configured('OLD')
        await self.callback('env:welcome:record')
        await self.upload('FIRST')
        old = self.store.gift_draft(101)['control_id']
        await self.action('retake')
        await self.callback(f'env:welcome:d:{old}:save')
        self.assertTrue(self.store.gift_draft(101)['active'])
        await self.upload('SECOND')
        self.assertEqual(self.store.gift_draft(101)['file_id'], 'SECOND')
        self.assertEqual(self.store.gift_circle(101), 'OLD')
        await self.action('cancel')
        self.assertEqual(self.store.gift_circle(101), 'OLD')
        self.assertIsNone(self.store.gift_draft(101))

    async def test_paused_welcome_does_not_consume_other_media_and_survives_restart(self):
        self.store.save_draft(101, {'id':'existing-envelope','step':'content','kind':'circle','active':True})
        await self.callback('env:welcome:record')
        self.assertFalse(self.store.draft(101)['active'])
        await self.upload('PENDING')
        await self.callback('env:home')
        self.assertFalse(self.store.gift_draft(101)['active'])
        self.flow = EnvelopeFlow(EnvelopeStore(self.path), {101}, None)
        self.assertFalse(await self.flow.handle_message(self.message(video_note=VideoNote(
            file_id='UNRELATED',file_unique_id='other',duration=1,length=240)),self.bot))
        await self.callback('env:welcome:resume')
        self.assertEqual(self.store.gift_draft(101)['file_id'], 'PENDING')
        await self.flow.handle_message(self.message(text='/cancel'),self.bot)
        self.assertEqual(self.store.draft(101)['id'], 'existing-envelope')
        self.assertIsNone(self.store.gift_draft(101))

    async def test_wrong_type_cannot_become_welcome_circle(self):
        await self.callback('env:welcome:record')
        await self.flow.handle_message(self.message(voice=Voice(file_id='VOICE',file_unique_id='voice',duration=2)),self.bot)
        self.assertIsNone(self.store.gift_draft(101)['file_id'])
        await self.action('save')
        self.assertIsNone(self.store.gift_circle(101))

    async def test_first_join_sends_text_then_circle_and_repeated_start_does_not_replay(self):
        await self.configured()
        self.bot.calls.clear()
        await self.join()
        sent = self.sent_to(202)
        self.assertEqual([type(c) for c in sent], [SendMessage, SendVideoNote])
        self.assertEqual(sent[0].text, GIFT_GREETING)
        self.assertEqual(sent[1].video_note, 'SAVED')
        self.assertEqual([c.text for c in self.sent_to(101)], [GIFT_JOIN_NOTICE])
        await asyncio.gather(self.flow.accept_invite(self.message(202),self.bot,'gift_offline_invite'),
                             self.flow.retry_gift_welcome(self.bot))
        self.assertEqual(sum(isinstance(c,SendVideoNote) for c in self.sent_to(202)),1)
        self.assertEqual(sum(isinstance(c,SendMessage) and c.text==GIFT_GREETING for c in self.sent_to(202)),1)
        restarted=EnvelopeFlow(EnvelopeStore(self.path),{101},None)
        self.assertFalse(await restarted.resume_gift_welcome(self.message(202),self.bot))
        await restarted.retry_gift_welcome(self.bot)
        self.assertEqual([c.text for c in self.sent_to(101)], [GIFT_JOIN_NOTICE])
        self.assertEqual(self.store.gift_delivery(202)['owner_notified'],1)

    async def test_notice_failure_retries_independently_after_restart(self):
        self.bot.fail_notice=True
        await self.join()
        self.assertEqual(self.store.gift_delivery(202)['owner_notified'],0)
        self.bot.fail_notice=False
        self.flow=EnvelopeFlow(EnvelopeStore(self.path),{101},None,clock=lambda:self.now)
        await self.flow.retry_gift_welcome(self.bot)
        self.assertEqual(len(self.sent_to(101)),1)
        self.now+=61
        # Retrying only the owner alert must not consume the partner's normal /start.
        self.assertFalse(await self.flow.resume_gift_welcome(self.message(202),self.bot))
        self.assertEqual(self.store.gift_delivery(202)['owner_notified'],1)
        await self.flow.retry_gift_welcome(self.bot)
        self.assertEqual(len(self.sent_to(101)),2)
        self.assertEqual(len(self.sent_to(202)),1)

    async def test_owner_block_does_not_interrupt_gift_or_retry_forever(self):
        await self.configured()
        self.bot.calls.clear()
        self.bot.block_notice=True
        await self.join()
        self.assertEqual([type(c) for c in self.sent_to(202)],[SendMessage,SendVideoNote])
        self.assertEqual(self.store.gift_delivery(202)['owner_notice_blocked'],1)
        self.now+=61
        await self.flow.retry_gift_welcome(self.bot)
        self.assertEqual(len(self.sent_to(101)),1)

    async def test_notification_does_not_depend_on_partner_delivery(self):
        self.bot.fail_text=True
        await self.join()
        self.assertEqual([c.text for c in self.sent_to(101)],[GIFT_JOIN_NOTICE])
        self.assertEqual(self.store.gift_delivery(202)['text_sent'],0)
        self.assertEqual(self.store.gift_delivery(202)['owner_notified'],1)

    async def test_no_notice_before_valid_partner_claim(self):
        self.store.create_invite(101,'offline_invite')
        await self.flow.retry_gift_welcome(self.bot)
        self.assertEqual(self.bot.calls,[])
        await self.flow.accept_invite(self.message(101),self.bot,'gift_offline_invite')
        await self.flow.accept_invite(self.message(303),self.bot,'gift_invalid')
        self.assertFalse(any(isinstance(c,SendMessage) and c.text==GIFT_JOIN_NOTICE for c in self.bot.calls))
        self.assertIsNone(self.store.gift_delivery(101))
        self.assertIsNone(self.store.gift_delivery(303))

    async def test_owner_opening_invite_explains_and_preserves_partner_access(self):
        self.store.create_invite(101,'offline_invite')
        await self.flow.accept_invite(self.message(101),self.bot,'gift_offline_invite')
        self.assertIsNone(self.store.invited_partner(101))
        self.assertIsNone(self.store.gift_delivery(101))
        self.assertEqual(len(self.sent_to(101)),1)
        self.assertIn('Это приглашение для твоей половинки',self.sent_to(101)[0].text)
        self.assertNotIn('уже использовано или заменено',self.sent_to(101)[0].text)
        self.assertEqual(self.sent_to(202),[])
        self.bot.calls.clear()
        await self.flow.accept_invite(self.message(202),self.bot,'gift_offline_invite')
        self.assertEqual(self.store.invited_partner(101),202)
        self.assertEqual([c.text for c in self.sent_to(101)],[GIFT_JOIN_NOTICE])
        self.assertEqual(self.sent_to(202)[0].text,GIFT_GREETING)

    def test_upgrade_preserves_existing_gift_and_does_not_notify_historical_join(self):
        legacy=Path(self.directory.name)/'legacy.sqlite3'
        with sqlite3.connect(legacy) as db:
            db.executescript('''CREATE TABLE gift_welcome_delivery (
                recipient INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,greeting TEXT NOT NULL,file_id TEXT,
                text_sent INTEGER NOT NULL DEFAULT 0,circle_sent INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0);
                INSERT INTO gift_welcome_delivery VALUES (202,101,'Saved greeting','SAVED',1,0,0,123);''')
        migrated=EnvelopeStore(legacy)
        row=migrated.gift_delivery(202)
        self.assertEqual((row['greeting'],row['file_id'],row['text_sent'],row['circle_sent'],row['retry_at']),
                         ('Saved greeting','SAVED',1,0,123))
        self.assertEqual(row['owner_notified'],1)
        migrated.prepare_gift_delivery(101,303,'New greeting')
        EnvelopeStore(legacy)
        self.assertEqual(migrated.gift_delivery(303)['owner_notified'],0)

    async def test_no_circle_keeps_text_only_onboarding(self):
        await self.join()
        self.assertEqual([type(c) for c in self.sent_to(202)],[SendMessage])
        self.assertEqual(self.sent_to(202)[0].text,GIFT_GREETING)

    async def test_failed_circle_retries_after_restart_without_repeating_text(self):
        await self.configured()
        self.bot.calls.clear()
        self.bot.fail_circle=True
        await self.join()
        delivery=self.store.gift_delivery(202)
        self.assertEqual((delivery['text_sent'],delivery['circle_sent']),(1,0))
        self.bot.fail_circle=False
        self.flow=EnvelopeFlow(EnvelopeStore(self.path),{101},None,clock=lambda:self.now)
        self.now+=61
        await self.flow.retry_gift_welcome(self.bot)
        self.assertEqual(sum(isinstance(c,SendMessage) and c.text==GIFT_GREETING for c in self.sent_to(202)),1)
        self.assertEqual(self.store.gift_delivery(202)['circle_sent'],1)

    async def test_failed_text_cannot_send_circle_ahead_of_greeting(self):
        await self.configured()
        self.bot.calls.clear()
        self.bot.fail_text=True
        await self.join()
        self.assertFalse(any(isinstance(c,SendVideoNote) for c in self.sent_to(202)))
        self.bot.fail_text=False
        self.assertTrue(await self.flow.resume_gift_welcome(self.message(202),self.bot))
        self.assertEqual([type(c) for c in self.sent_to(202)][-2:],[SendMessage,SendVideoNote])

    async def test_invalid_invite_and_partner_cannot_access_owner_settings(self):
        await self.configured()
        self.bot.calls.clear()
        await self.flow.accept_invite(self.message(303),self.bot,'gift_invalid')
        self.assertFalse(any(isinstance(c,SendVideoNote) for c in self.bot.calls))
        self.assertIsNone(self.store.gift_delivery(303))
        await self.join()
        await self.callback('env:welcome:remove',202)
        self.assertEqual(self.store.gift_circle(101),'SAVED')
        self.assertIsNone(self.store.gift_draft(202))

    async def test_removal_requires_confirmation_and_does_not_delete_existing_envelopes(self):
        await self.configured()
        self.store.save_draft(101,{'id':'KEEP','active':False})
        await self.callback('env:welcome:remove')
        self.assertEqual(self.store.gift_circle(101),'SAVED')
        await self.action('cancel')
        self.assertEqual(self.store.gift_circle(101),'SAVED')
        await self.callback('env:welcome:remove')
        await self.action('save')
        self.assertIsNone(self.store.gift_circle(101))
        self.assertEqual(self.store.draft(101)['id'],'KEEP')


if __name__=='__main__':
    unittest.main()
