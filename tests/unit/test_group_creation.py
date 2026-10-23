"""Offline regression tests; these never log in or create real Telegram groups."""

import asyncio
import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import telegram
from telethon import functions, types
from telethon.errors import FloodWaitError

from efb_telegram_master import utils
from efb_telegram_master.chat_binding import ChatBindingManager, ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.group_creator import GroupCreator, GroupCreationError


@pytest.fixture
def creator(tmp_path):
    item = GroupCreator({'enabled': True, 'api_id': 1, 'api_hash': 'private-test-value'}, tmp_path, 123)
    item.session_path.touch()
    return item


@pytest.fixture
def client(creator, monkeypatch):
    group = types.Channel(id=456, title='Test group', photo=types.ChatPhotoEmpty(),
                          date=datetime.now(timezone.utc), megagroup=True, access_hash=789)
    bot = types.User(id=7, bot=True, first_name='EFB', username='example_bot')
    async def request(call):
        if isinstance(call, functions.channels.CreateChannelRequest):
            return SimpleNamespace(chats=[group])
        return SimpleNamespace()

    item = AsyncMock()
    item.connect = AsyncMock()
    item.disconnect = AsyncMock()
    item.is_user_authorized = AsyncMock(return_value=True)
    item.get_me = AsyncMock(return_value=SimpleNamespace(id=123, bot=False))
    item.get_entity = AsyncMock(side_effect=lambda value: bot if isinstance(value, str) else group)
    item.side_effect = request
    monkeypatch.setattr(creator, '_client', lambda: item)
    return item


def create(creator, request_id='first'):
    return creator.create('remote chat', request_id, 'Contact name', 'example_bot', 7)


def creation_calls(client):
    return [c for c in client.call_args_list if isinstance(c.args[0], functions.channels.CreateChannelRequest)]


def test_disabled_does_not_touch_disk(tmp_path):
    creator = GroupCreator({}, tmp_path / 'unused', 123)
    with pytest.raises(GroupCreationError, match='尚未启用'):
        create(creator)
    assert not creator.data_dir.exists()


def test_creates_non_forum_supergroup_and_invites_main_bot(creator, client):
    assert create(creator) == -1000000000456
    requests = [c.args[0] for c in client.call_args_list]
    assert requests[0].megagroup and not requests[0].forum
    assert requests[0].title == 'Contact name'
    assert isinstance(requests[1], functions.channels.InviteToChannelRequest)
    assert isinstance(requests[2], functions.channels.EditAdminRequest)
    assert requests[2].admin_rights.pin_messages
    assert creator._receipt('remote chat')['chat_id'] == -1000000000456
    client.disconnect.assert_awaited_once()


def test_invite_failure_reuses_saved_group_after_restart(creator, client, monkeypatch):
    normal_request = client.side_effect

    async def fail_invite(call):
        if isinstance(call, functions.channels.InviteToChannelRequest):
            raise RuntimeError('private-test-value')
        return await normal_request(call)

    client.side_effect = fail_invite
    with pytest.raises(GroupCreationError) as error:
        create(creator)
    assert 'private-test-value' not in str(error.value)
    restarted = GroupCreator(creator.config, creator.data_dir, 123)
    monkeypatch.setattr(restarted, '_client', lambda: client)
    client.side_effect = normal_request
    assert create(restarted, 'new-panel') == -1000000000456
    assert len(creation_calls(client)) == 1


def test_uncertain_creation_is_not_repeated(creator, client):
    client.side_effect = asyncio.TimeoutError()
    with pytest.raises(GroupCreationError):
        create(creator)
    with pytest.raises(GroupCreationError, match='返回结果不确定'):
        create(creator, 'new-panel')
    assert len(creation_calls(client)) == 1


def test_explicit_flood_wait_can_be_retried(creator, client):
    original = client.side_effect
    client.side_effect = FloodWaitError(request=None, capture=30)
    with pytest.raises(GroupCreationError, match='30 秒'):
        create(creator)
    assert creator._receipt('remote chat') is None
    client.side_effect = original
    create(creator)
    assert len(creation_calls(client)) == 2


@pytest.mark.parametrize('authorized,user_id,is_bot', [(False, 123, False), (True, 99, False), (True, 123, True)])
def test_requires_authorized_matching_user(creator, client, authorized, user_id, is_bot):
    client.is_user_authorized.return_value = authorized
    client.get_me.return_value = SimpleNamespace(id=user_id, bot=is_bot)
    with pytest.raises(GroupCreationError):
        create(creator)
    client.assert_not_called()
    assert creator._receipt('remote chat') is None


def test_completed_request_is_idempotent_but_new_panel_can_relink(creator, client):
    create(creator)
    creator.mark_bound('remote chat')
    create(creator)
    assert len(creation_calls(client)) == 1
    create(creator, 'deliberate-relink')
    assert len(creation_calls(client)) == 2


@pytest.fixture
def binding(creator):
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.channel = SimpleNamespace(config={'admins': [123, 999]}, channel_id='blueset.telegram',
                                      _=lambda s: s, flag=lambda key: True)
    manager.bot = Mock()
    manager.bot.me = SimpleNamespace(id=7, username='example_bot')
    manager.db = Mock()
    manager.msg_storage = {}
    manager.group_creator = creator
    manager._group_jobs = set()
    manager._group_jobs_lock = threading.Lock()
    manager._group_executor = Mock()
    manager.logger = logging.getLogger(__name__)
    manager.migrate_chat_history = Mock()
    manager.send_history_link = Mock()
    manager.create_topic = Mock(return_value=21)
    return manager


def remote_chat():
    return SimpleNamespace(module_id='slave', uid='user', full_name='Contact',
                           chat_title='Contact', linked=[], link=Mock())


def panel(binding):
    chat = remote_chat()
    data = ChatListStorage([chat])
    binding.msg_storage[(123, 10)] = data
    return chat, data


def callback(user_id=123, chat_type=telegram.Chat.PRIVATE):
    query = Mock()
    return SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=123, type=chat_type),
                           effective_user=SimpleNamespace(id=user_id),
                           effective_message=SimpleNamespace(message_id=10))


@pytest.mark.parametrize('user_id,chat_type', [(999, telegram.Chat.PRIVATE), (123, telegram.Chat.SUPERGROUP),
                                             (456, telegram.Chat.PRIVATE)])
def test_only_owner_in_private_can_create(binding, user_id, chat_type):
    panel(binding)
    update = callback(user_id, chat_type)
    binding.create_group_and_link(update)
    binding._group_executor.submit.assert_not_called()
    assert update.callback_query.answer.call_args.kwargs['show_alert']


def test_callback_acknowledged_and_duplicate_job_rejected(binding):
    panel(binding)
    update = callback()
    assert binding.create_group_and_link(update) == Flags.LINK_EXEC
    update.callback_query.answer.assert_called_once()
    binding.create_group_and_link(callback())
    binding._group_executor.submit.assert_called_once()


def test_expired_panel_never_creates(binding):
    assert binding.create_group_and_link(callback()) == -1
    binding._group_executor.submit.assert_not_called()


def test_button_is_optional_and_existing_buttons_survive(binding):
    chat, _ = panel(binding)
    for enabled in (False, True):
        binding.group_creator.enabled = enabled
        binding.build_link_action_message(chat, 123, 10)
        buttons = binding.bot.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
        flattened = [button for row in buttons for button in row]
        assert any(button.url and 'startgroup=' in button.url for button in flattened)
        assert any(button.callback_data == 'manual_link 0' for button in flattened)
        assert any(button.callback_data == 'create_group 0' for button in flattened) == enabled


@pytest.mark.parametrize('linked,override,backfill,history', [
    (False, None, True, False), (True, None, False, True),
    (False, False, False, False), (True, True, True, False)])
def test_shared_binding_preserves_history_and_topic_cleanup(binding, monkeypatch, linked, override, backfill, history):
    monkeypatch.setattr('efb_telegram_master.chat_binding.coordinator.get_module_by_id', lambda _: object())
    chat, data = panel(binding)
    chat.linked = ['old binding'] if linked else []
    group = SimpleNamespace(id=-100456, is_forum=False)
    on_bound = Mock()
    binding._link_chat_to_destination(chat, group, (123, 10), data, override, on_bound)
    chat.link.assert_called_once_with('blueset.telegram', str(group.id), True)
    binding.db.remove_topic_assoc.assert_called_once_with(slave_uid=utils.chat_id_to_str(chat=chat))
    binding.create_topic.assert_not_called()
    assert binding.migrate_chat_history.called == backfill
    assert binding.send_history_link.called == history
    on_bound.assert_called_once()
    assert (123, 10) not in binding.msg_storage


def test_existing_start_link_still_honors_no_backfill(binding, monkeypatch):
    monkeypatch.setattr('efb_telegram_master.chat_binding.coordinator.get_module_by_id', lambda _: object())
    chat, _ = panel(binding)
    group = telegram.Chat(id=-100456, type=telegram.Chat.SUPERGROUP, is_forum=False)
    message = telegram.Message(message_id=20, date=datetime.now(timezone.utc), chat=group)
    update = telegram.Update(update_id=1, message=message)
    code = utils.b64en(utils.message_id_to_str(123, 10))
    binding.link_chat(update, [code, 'false'])
    chat.link.assert_called_once_with('blueset.telegram', '-100456', True)
    binding.migrate_chat_history.assert_not_called()
    binding.send_history_link.assert_not_called()


def test_existing_forum_binding_still_creates_topic(binding, monkeypatch):
    monkeypatch.setattr('efb_telegram_master.chat_binding.coordinator.get_module_by_id', lambda _: object())
    chat, data = panel(binding)
    binding._update_single_topic_info = Mock()
    group = SimpleNamespace(id=-100456, is_forum=True)
    binding._link_chat_to_destination(chat, group, (123, 10), data)
    remote_uid = utils.chat_id_to_str(chat=chat)
    binding.create_topic.assert_called_once_with(slave_uid=remote_uid, telegram_chat_id=-100456)
    binding.migrate_chat_history.assert_called_once_with(remote_uid, -100456, 21)


def test_binding_failure_keeps_retry_and_receipt(binding):
    chat, data = panel(binding)
    binding.group_creator = Mock()
    binding.group_creator.create.return_value = -100456
    binding.bot.get_chat_member.return_value.status = 'administrator'
    binding._link_chat_to_destination = Mock(side_effect=RuntimeError())
    binding._create_group_worker(chat, data, (123, 10), 'remote')
    binding.group_creator.mark_bound.assert_not_called()
    assert (123, 10) in binding.msg_storage
    buttons = binding.bot.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
    assert buttons[0][0].callback_data == 'create_group 0'


def test_notification_failure_after_binding_never_offers_recreation(binding):
    chat, data = panel(binding)
    binding.group_creator = Mock()
    binding.group_creator.create.return_value = -100456
    binding.bot.get_chat_member.return_value.status = 'administrator'

    def bind_then_fail(*args, on_bound, **kwargs):
        on_bound()
        raise RuntimeError()

    binding._link_chat_to_destination = bind_then_fail
    binding._create_group_worker(chat, data, (123, 10), 'remote')
    binding.group_creator.mark_bound.assert_called_once_with('remote')
    assert (123, 10) not in binding.msg_storage
    assert not binding.bot.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
