import asyncio
import itertools
import string

from .users import UserMethods
from .. import utils
from ..requestiter import RequestIter
from ..tl import types, functions, custom

_MAX_PARTICIPANTS_CHUNK_SIZE = 200
_MAX_ADMIN_LOG_CHUNK_SIZE = 100


class _ChatAction:
    _str_mapping = {
        'typing': types.SendMessageTypingAction(),
        'contact': types.SendMessageChooseContactAction(),
        'game': types.SendMessageGamePlayAction(),
        'location': types.SendMessageGeoLocationAction(),

        'record-audio': types.SendMessageRecordAudioAction(),
        'record-voice': types.SendMessageRecordAudioAction(),  # alias
        'record-round': types.SendMessageRecordRoundAction(),
        'record-video': types.SendMessageRecordVideoAction(),

        'audio': types.SendMessageUploadAudioAction(1),
        'voice': types.SendMessageUploadAudioAction(1),  # alias
        'round': types.SendMessageUploadRoundAction(1),
        'video': types.SendMessageUploadVideoAction(1),

        'photo': types.SendMessageUploadPhotoAction(1),
        'document': types.SendMessageUploadDocumentAction(1),
        'file': types.SendMessageUploadDocumentAction(1),  # alias
        'song': types.SendMessageUploadDocumentAction(1),  # alias

        'cancel': types.SendMessageCancelAction()
    }

    def __init__(self, client, chat, action, *, delay, auto_cancel):
        self._client = client
        self._chat = chat
        self._action = action
        self._delay = delay
        self._auto_cancel = auto_cancel
        self._request = None
        self._task = None
        self._running = False

    async def __aenter__(self):
        self._chat = await self._client.get_input_entity(self._chat)

        # Since `self._action` is passed by reference we can avoid
        # recreating the request all the time and still modify
        # `self._action.progress` directly in `progress`.
        self._request = functions.messages.SetTypingRequest(
            self._chat, self._action)

        self._running = True
        self._task = self._client.loop.create_task(self._update())

    async def __aexit__(self, *args):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

            self._task = None

    def __enter__(self):
        if self._client.loop.is_running():
            raise RuntimeError(
                'You must use "async with" if the event loop '
                'is running (i.e. you are inside an "async def")'
            )

        return self._client.loop.run_until_complete(self.__aenter__())

    def __exit__(self, *args):
        return self._client.loop.run_until_complete(self.__aexit__(*args))

    async def _update(self):
        try:
            while self._running:
                await self._client(self._request)
                await asyncio.sleep(self._delay)
        except ConnectionError:
            pass
        except asyncio.CancelledError:
            if self._auto_cancel:
                await self._client(functions.messages.SetTypingRequest(
                    self._chat, types.SendMessageCancelAction()))

    def progress(self, current, total):
        if hasattr(self._action, 'progress'):
            self._action.progress = 100 * round(current / total)


class _ParticipantsIter(RequestIter):
    async def _init(self, entity, filter, search, aggressive):
        if isinstance(filter, type):
            if filter in (types.ChannelParticipantsBanned,
                          types.ChannelParticipantsKicked,
                          types.ChannelParticipantsSearch,
                          types.ChannelParticipantsContacts):
                # These require a `q` parameter (support types for convenience)
                filter = filter('')
            else:
                filter = filter()

        entity = await self.client.get_input_entity(entity)
        if search and (filter
                       or not isinstance(entity, types.InputPeerChannel)):
            # We need to 'search' ourselves unless we have a PeerChannel
            search = search.lower()

            self.filter_entity = lambda ent: (
                search in utils.get_display_name(ent).lower() or
                search in (getattr(ent, 'username', '') or None).lower()
            )
        else:
            self.filter_entity = lambda ent: True

        # Only used for channels, but we should always set the attribute
        self.requests = []

        if isinstance(entity, types.InputPeerChannel):
            self.total = (await self.client(
                functions.channels.GetFullChannelRequest(entity)
            )).full_chat.participants_count

            if self.limit <= 0:
                raise StopAsyncIteration

            self.seen = set()
            if aggressive and not filter:
                self.requests.extend(functions.channels.GetParticipantsRequest(
                    channel=entity,
                    filter=types.ChannelParticipantsSearch(x),
                    offset=0,
                    limit=_MAX_PARTICIPANTS_CHUNK_SIZE,
                    hash=0
                ) for x in (search or string.ascii_lowercase))
            else:
                self.requests.append(functions.channels.GetParticipantsRequest(
                    channel=entity,
                    filter=filter or types.ChannelParticipantsSearch(search),
                    offset=0,
                    limit=_MAX_PARTICIPANTS_CHUNK_SIZE,
                    hash=0
                ))

        elif isinstance(entity, types.InputPeerChat):
            full = await self.client(
                functions.messages.GetFullChatRequest(entity.chat_id))
            if not isinstance(
                    full.full_chat.participants, types.ChatParticipants):
                # ChatParticipantsForbidden won't have ``.participants``
                self.total = 0
                raise StopAsyncIteration

            self.total = len(full.full_chat.participants.participants)

            users = {user.id: user for user in full.users}
            for participant in full.full_chat.participants.participants:
                user = users[participant.user_id]
                if not self.filter_entity(user):
                    continue

                user = users[participant.user_id]
                user.participant = participant
                self.buffer.append(user)

            return True
        else:
            self.total = 1
            if self.limit != 0:
                user = await self.client.get_entity(entity)
                if self.filter_entity(user):
                    user.participant = None
                    self.buffer.append(user)

            return True

    async def _load_next_chunk(self):
        if not self.requests:
            return True

        # Only care about the limit for the first request
        # (small amount of people, won't be aggressive).
        #
        # Most people won't care about getting exactly 12,345
        # members so it doesn't really matter not to be 100%
        # precise with being out of the offset/limit here.
        self.requests[0].limit = min(
            self.limit - self.requests[0].offset, _MAX_PARTICIPANTS_CHUNK_SIZE)

        if self.requests[0].offset > self.limit:
            return True

        results = await self.client(self.requests)
        for i in reversed(range(len(self.requests))):
            participants = results[i]
            if not participants.users:
                self.requests.pop(i)
                continue

            self.requests[i].offset += len(participants.participants)
            users = {user.id: user for user in participants.users}
            for participant in participants.participants:
                user = users[participant.user_id]
                if not self.filter_entity(user) or user.id in self.seen:
                    continue

                self.seen.add(participant.user_id)
                user = users[participant.user_id]
                user.participant = participant
                self.buffer.append(user)


class _AdminLogIter(RequestIter):
    async def _init(
            self, entity, admins, search, min_id, max_id,
            join, leave, invite, restrict, unrestrict, ban, unban,
            promote, demote, info, settings, pinned, edit, delete
    ):
        if any((join, leave, invite, restrict, unrestrict, ban, unban,
                promote, demote, info, settings, pinned, edit, delete)):
            events_filter = types.ChannelAdminLogEventsFilter(
                join=join, leave=leave, invite=invite, ban=restrict,
                unban=unrestrict, kick=ban, unkick=unban, promote=promote,
                demote=demote, info=info, settings=settings, pinned=pinned,
                edit=edit, delete=delete
            )
        else:
            events_filter = None

        self.entity = await self.client.get_input_entity(entity)

        admin_list = []
        if admins:
            if not utils.is_list_like(admins):
                admins = (admins,)

            for admin in admins:
                admin_list.append(await self.client.get_input_entity(admin))

        self.request = functions.channels.GetAdminLogRequest(
            self.entity, q=search or '', min_id=min_id, max_id=max_id,
            limit=0, events_filter=events_filter, admins=admin_list or None
        )

    async def _load_next_chunk(self):
        self.request.limit = min(self.left, _MAX_ADMIN_LOG_CHUNK_SIZE)
        r = await self.client(self.request)
        entities = {utils.get_peer_id(x): x
                    for x in itertools.chain(r.users, r.chats)}

        self.request.max_id = min((e.id for e in r.events), default=0)
        for ev in r.events:
            if isinstance(ev.action,
                          types.ChannelAdminLogEventActionEditMessage):
                ev.action.prev_message._finish_init(
                    self.client, entities, self.entity)

                ev.action.new_message._finish_init(
                    self.client, entities, self.entity)

            elif isinstance(ev.action,
                            types.ChannelAdminLogEventActionDeleteMessage):
                ev.action.message._finish_init(
                    self.client, entities, self.entity)

            self.buffer.append(custom.AdminLogEvent(ev, entities))

        if len(r.events) < self.request.limit:
            return True


class ChatMethods(UserMethods):

    # region Public methods

    def iter_participants(
            self, entity, limit=None, *, search='',
            filter=None, aggressive=False
    ):
        """
        Iterator over the participants belonging to the specified chat.

        Args:
            entity (`entity`):
                The entity from which to retrieve the participants list.

            limit (`int`):
                Limits amount of participants fetched.

            search (`str`, optional):
                Look for participants with this string in name/username.

                If ``aggressive is True``, the symbols from this string will
                be used.

            filter (:tl:`ChannelParticipantsFilter`, optional):
                The filter to be used, if you want e.g. only admins
                Note that you might not have permissions for some filter.
                This has no effect for normal chats or users.

                .. note::

                    The filter :tl:`ChannelParticipantsBanned` will return
                    *restricted* users. If you want *banned* users you should
                    use :tl:`ChannelParticipantsKicked` instead.

            aggressive (`bool`, optional):
                Aggressively looks for all participants in the chat.

                This is useful for channels since 20 July 2018,
                Telegram added a server-side limit where only the
                first 200 members can be retrieved. With this flag
                set, more than 200 will be often be retrieved.

                This has no effect if a ``filter`` is given.

        Yields:
            The :tl:`User` objects returned by :tl:`GetParticipantsRequest`
            with an additional ``.participant`` attribute which is the
            matched :tl:`ChannelParticipant` type for channels/megagroups
            or :tl:`ChatParticipants` for normal chats.
        """
        return _ParticipantsIter(
            self,
            limit,
            entity=entity,
            filter=filter,
            search=search,
            aggressive=aggressive
        )

    async def get_participants(self, *args, **kwargs):
        """
        Same as `iter_participants`, but returns a
        `TotalList <telethon.helpers.TotalList>` instead.
        """
        return await self.iter_participants(*args, **kwargs).collect()

    def iter_admin_log(
            self, entity, limit=None, *, max_id=0, min_id=0, search=None,
            admins=None, join=None, leave=None, invite=None, restrict=None,
            unrestrict=None, ban=None, unban=None, promote=None, demote=None,
            info=None, settings=None, pinned=None, edit=None, delete=None):
        """
        Iterator over the admin log for the specified channel.

        Note that you must be an administrator of it to use this method.

        If none of the filters are present (i.e. they all are ``None``),
        *all* event types will be returned. If at least one of them is
        ``True``, only those that are true will be returned.

        Args:
            entity (`entity`):
                The channel entity from which to get its admin log.

            limit (`int` | `None`, optional):
                Number of events to be retrieved.

                The limit may also be ``None``, which would eventually return
                the whole history.

            max_id (`int`):
                All the events with a higher (newer) ID or equal to this will
                be excluded.

            min_id (`int`):
                All the events with a lower (older) ID or equal to this will
                be excluded.

            search (`str`):
                The string to be used as a search query.

            admins (`entity` | `list`):
                If present, the events will be filtered by these admins
                (or single admin) and only those caused by them will be
                returned.

            join (`bool`):
                If ``True``, events for when a user joined will be returned.

            leave (`bool`):
                If ``True``, events for when a user leaves will be returned.

            invite (`bool`):
                If ``True``, events for when a user joins through an invite
                link will be returned.

            restrict (`bool`):
                If ``True``, events with partial restrictions will be
                returned. This is what the API calls "ban".

            unrestrict (`bool`):
                If ``True``, events removing restrictions will be returned.
                This is what the API calls "unban".

            ban (`bool`):
                If ``True``, events applying or removing all restrictions will
                be returned. This is what the API calls "kick" (restricting
                all permissions removed is a ban, which kicks the user).

            unban (`bool`):
                If ``True``, events removing all restrictions will be
                returned. This is what the API calls "unkick".

            promote (`bool`):
                If ``True``, events with admin promotions will be returned.

            demote (`bool`):
                If ``True``, events with admin demotions will be returned.

            info (`bool`):
                If ``True``, events changing the group info will be returned.

            settings (`bool`):
                If ``True``, events changing the group settings will be
                returned.

            pinned (`bool`):
                If ``True``, events of new pinned messages will be returned.

            edit (`bool`):
                If ``True``, events of message edits will be returned.

            delete (`bool`):
                If ``True``, events of message deletions will be returned.

        Yields:
            Instances of `telethon.tl.custom.adminlogevent.AdminLogEvent`.
        """
        return _AdminLogIter(
            self,
            limit,
            entity=entity,
            admins=admins,
            search=search,
            min_id=min_id,
            max_id=max_id,
            join=join,
            leave=leave,
            invite=invite,
            restrict=restrict,
            unrestrict=unrestrict,
            ban=ban,
            unban=unban,
            promote=promote,
            demote=demote,
            info=info,
            settings=settings,
            pinned=pinned,
            edit=edit,
            delete=delete
        )

    async def get_admin_log(self, *args, **kwargs):
        """
        Same as `iter_admin_log`, but returns a ``list`` instead.
        """
        return await self.iter_admin_log(*args, **kwargs).collect()

    def action(self, entity, action, *, delay=4, auto_cancel=True):
        """
        Returns a context-manager object to represent a "chat action".

        Chat actions indicate things like "user is typing", "user is
        uploading a photo", etc. Normal usage is as follows:

        .. code-block:: python

            async with client.action(chat, 'typing'):
                await asyncio.sleep(2)  # type for 2 seconds
                await client.send_message(chat, 'Hello world! I type slow ^^')

        If the action is ``'cancel'``, you should just ``await`` the result,
        since it makes no sense to use a context-manager for it:

        .. code-block:: python

            await client.action(chat, 'cancel')

        Args:
            entity (`entity`):
                The entity where the action should be showed in.

            action (`str` | :tl:`SendMessageAction`):
                The action to show. You can either pass a instance of
                :tl:`SendMessageAction` or better, a string used while:

                * ``'typing'``: typing a text message.
                * ``'contact'``: choosing a contact.
                * ``'game'``: playing a game.
                * ``'location'``: choosing a geo location.
                * ``'record-audio'``: recording a voice note.
                  You may use ``'record-voice'`` as alias.
                * ``'record-round'``: recording a round video.
                * ``'record-video'``: recording a normal video.
                * ``'audio'``: sending an audio file (voice note or song).
                  You may use ``'voice'`` and ``'song'`` as aliases.
                * ``'round'``: uploading a round video.
                * ``'video'``: uploading a video file.
                * ``'photo'``: uploading a photo.
                * ``'document'``: uploading a document file.
                  You may use ``'file'`` as alias.
                * ``'cancel'``: cancel any pending action in this chat.

                Invalid strings will raise a ``ValueError``.

            delay (`int` | `float`):
                The delay, in seconds, to wait between sending actions.
                For example, if the delay is 5 and it takes 7 seconds to
                do something, three requests will be made at 0s, 5s, and
                7s to cancel the action.

            auto_cancel (`bool`):
                Whether the action should be cancelled once the context
                manager exists or not. The default is ``True``, since
                you don't want progress to be shown when it has already
                completed.

        If you are uploading a file, you may do
        ``progress_callback=chat.progress`` to update the progress of
        the action. Some clients don't care about this progress, though,
        so it's mostly not needed, but still available.
        """
        if isinstance(action, str):
            try:
                action = _ChatAction._str_mapping[action.lower()]
            except KeyError:
                raise ValueError('No such action "{}"'.format(action)) from None
        elif not isinstance(action, types.TLObject) or action.SUBCLASS_OF_ID != 0x20b2cc21:
            # 0x20b2cc21 = crc32(b'SendMessageAction')
            if isinstance(action, type):
                raise ValueError('You must pass an instance, not the class')
            else:
                raise ValueError('Cannot use {} as action'.format(action))

        if isinstance(action, types.SendMessageCancelAction):
            # ``SetTypingRequest.resolve`` will get input peer of ``entity``.
            return self(functions.messages.SetTypingRequest(
                entity, types.SendMessageCancelAction()))

        return _ChatAction(
            self, entity, action, delay=delay, auto_cancel=auto_cancel)

    # endregion
