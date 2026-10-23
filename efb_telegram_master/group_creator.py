"""User-account group creation, with a durable receipt before binding.

Login is deliberately a separate terminal command; bot callbacks never prompt
for a phone number, login code or password.
"""

import argparse
import asyncio
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Optional


class GroupCreationError(Exception):
    """An actionable, credential-free message safe to display to an admin."""


class GroupCreator:
    def __init__(self, config: Dict[str, Any], data_dir: Path, default_owner: int):
        self.config = config
        self.enabled = config.get('enabled', False) is True
        self.owner_id = int(config.get('owner_id', default_owner))
        self.data_dir = Path(data_dir)
        self.session_path = self.data_dir / 'group-creator.session'
        self.receipts_path = self.data_dir / 'group-creation.sqlite3'
        self._lock = threading.Lock()

    def _database(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.receipts_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(str(self.receipts_path))
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS creations (
            remote_uid TEXT PRIMARY KEY, request_id TEXT NOT NULL,
            state TEXT NOT NULL, chat_id INTEGER, owner_id INTEGER NOT NULL
        )''')
        db.commit()
        return db

    def _receipt(self, remote_uid: str) -> Optional[Dict[str, Any]]:
        db = self._database()
        try:
            row = db.execute('SELECT * FROM creations WHERE remote_uid = ?', (remote_uid,)).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def _save(self, remote_uid: str, request_id: str, state: str, chat_id: Optional[int]):
        db = self._database()
        try:
            with db:
                db.execute('INSERT OR REPLACE INTO creations VALUES (?, ?, ?, ?, ?)',
                           (remote_uid, request_id, state, chat_id, self.owner_id))
        finally:
            db.close()

    def mark_bound(self, remote_uid: str):
        db = self._database()
        try:
            with db:
                db.execute("UPDATE creations SET state = 'bound' WHERE remote_uid = ?", (remote_uid,))
        finally:
            db.close()

    def forget_pending(self, remote_uid: str):
        """Remove only a local receipt, never a Telegram group or EFB binding."""
        db = self._database()
        try:
            with db:
                db.execute('DELETE FROM creations WHERE remote_uid = ?', (remote_uid,))
        finally:
            db.close()

    def _client(self):
        try:
            from telethon import TelegramClient
        except ImportError:
            raise GroupCreationError('请先安装可选依赖：pip install "efb-telegram-master[group-creation]"。') from None
        api_id, api_hash = self.config.get('api_id'), self.config.get('api_hash')
        if not api_id or not api_hash:
            raise GroupCreationError('请配置 group_creation.api_id 和 api_hash，并在终端完成用户账号登录。')
        proxy = None
        if self.config.get('proxy'):
            import socks
            conf = self.config['proxy']
            kind = {'socks5': socks.SOCKS5, 'socks4': socks.SOCKS4, 'http': socks.HTTP}.get(conf.get('type'))
            if kind is None:
                raise GroupCreationError('group_creation.proxy.type 只支持 socks5、socks4 或 http。')
            proxy = (kind, conf['host'], int(conf['port']), True,
                     conf.get('username'), conf.get('password'))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.session_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        return TelegramClient(str(self.session_path), int(api_id), str(api_hash),
                              proxy=proxy, request_retries=0, connection_retries=1,
                              flood_sleep_threshold=0, timeout=15)

    async def _check_owner(self, client):
        me = await client.get_me()
        if not me or me.bot or me.id != self.owner_id:
            raise GroupCreationError('建群 session 必须登录 group_creation.owner_id 对应的 Telegram 用户账号。')

    def create(self, remote_uid: str, request_id: str, title: str, bot_username: str, bot_id: int) -> int:
        if not self.enabled:
            raise GroupCreationError('尚未启用 group_creation.enabled。')
        if not self._lock.acquire(blocking=False):
            raise GroupCreationError('另一个建群任务正在执行，请稍后重试。')
        try:
            return asyncio.run(asyncio.wait_for(
                self._create(remote_uid, request_id, title, bot_username, bot_id), timeout=60))
        except GroupCreationError:
            raise
        except Exception as exc:
            # Exception text from a client may include configuration; expose only
            # its type and the FloodWait duration, never tokens or API secrets.
            wait = getattr(exc, 'seconds', None)
            hint = '，请等待 {} 秒后重试'.format(wait) if isinstance(wait, int) else ''
            raise GroupCreationError('建群或添加 Bot 失败（{}{}）。已取得群 ID 的任务会复用原群；'
                                     '请检查账号、代理和 Bot 权限后重试。'.format(type(exc).__name__, hint)) from None
        finally:
            self._lock.release()

    async def _create(self, remote_uid: str, request_id: str, title: str, bot_username: str, bot_id: int) -> int:
        receipt = self._receipt(remote_uid)
        if receipt and receipt['owner_id'] != self.owner_id:
            raise GroupCreationError('此聊天存在另一个用户账号的建群记录，请先检查本地建群记录。')
        if receipt and receipt['state'] == 'creating':
            raise GroupCreationError('上次建群的返回结果不确定，已暂停再次建群以避免重复。'
                                     '请查看 Telegram 是否已出现新群，按文档手动绑定或恢复。远端标识：' + remote_uid)
        reuse = receipt and (receipt['state'] != 'bound' or receipt['request_id'] == request_id)
        if not self.session_path.exists():
            raise GroupCreationError('请先按 docs/group-creation.md 在终端登录 Telegram 用户账号。')
        client = self._client()
        from telethon import functions, types, utils
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise GroupCreationError('Telegram 用户 session 未登录或已失效，请在终端重新登录。')
            await self._check_owner(client)
            bot = await client.get_entity(bot_username)
            if not isinstance(bot, types.User) or not bot.bot or bot.id != bot_id:
                raise GroupCreationError('无法确认转发 Bot 身份，请检查 Bot 用户名。')
            if reuse:
                assert receipt is not None
                chat_id = int(receipt['chat_id'])
                group = await client.get_entity(chat_id)
            else:
                # Commit before sending: a timeout/crash after Telegram accepted
                # creation must never cause an automatic second CreateChannel.
                self._save(remote_uid, request_id, 'creating', None)
                try:
                    result = await client(functions.channels.CreateChannelRequest(
                        title=(title.strip() or 'EFB chat')[:128], about='', megagroup=True))
                except Exception as exc:
                    from telethon.errors import RPCError
                    if isinstance(exc, RPCError) and exc.code < 500:
                        # Telegram explicitly rejected the request, so retrying
                        # cannot create a duplicate. Transport failures stay held.
                        self.forget_pending(remote_uid)
                    raise
                group = next(c for c in result.chats if isinstance(c, types.Channel) and c.megagroup)
                chat_id = utils.get_peer_id(group)
                self._save(remote_uid, request_id, 'created', chat_id)
            from telethon.errors import UserAlreadyParticipantError
            try:
                await client(functions.channels.InviteToChannelRequest(group, [bot]))
            except UserAlreadyParticipantError:
                pass
            await client(functions.channels.EditAdminRequest(
                channel=group, user_id=bot,
                admin_rights=types.ChatAdminRights(change_info=True, delete_messages=True,
                                                  invite_users=True, pin_messages=True),
                rank='EFB'))
            return chat_id
        finally:
            await client.disconnect()

    def login(self):
        async def perform_login():
            client = self._client()
            try:
                await client.start()
                await self._check_owner(client)
            finally:
                await client.disconnect()
        asyncio.run(perform_login())


def main():
    parser = argparse.ArgumentParser(description='Login the optional Telegram group-creation user account.')
    parser.add_argument('--config', required=True, type=Path, help='ETM config.yaml')
    parser.add_argument('--data-dir', required=True, type=Path, help='ETM channel data directory')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--login', action='store_true')
    action.add_argument('--forget-pending', metavar='REMOTE_UID',
                        help='Forget a receipt ONLY after inspecting Telegram for an already-created group')
    args = parser.parse_args()
    from ruamel.yaml import YAML
    with args.config.open() as stream:
        config = YAML(typ='safe').load(stream)
    admins = config['admins']
    default_owner = admins[0] if isinstance(admins, list) else admins
    creator = GroupCreator(config.get('group_creation', {}) or {}, args.data_dir, int(default_owner))
    if args.login:
        creator.login()
        print('用户账号已登录。session 保存在指定 data-dir；重启 EFB 后即可使用建群按钮。')
    else:
        if input('已检查 Telegram，并确认不会重复建群？输入 YES 清除本地记录：') != 'YES':
            parser.exit(message='未修改记录。\n')
        creator.forget_pending(args.forget_pending)
        print('已清除本地建群记录；未删除任何 Telegram 群或聊天绑定。')


if __name__ == '__main__':
    main()
