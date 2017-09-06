import asyncio
import typing
from types import TracebackType
from typing import Any, Awaitable, Callable, Mapping, Type, Union, cast
from .exceptions import KeyDecodeError, ValueDecodeError
from .streams import current_event
from .types import (
    AppT, CodecArg, FutureMessage, K, Message,
    MessageSentCallback, ModelArg, PendingMessage, RecordMetadata, V,
)
from .types.channels import ChannelT, EventT
from .types.streams import StreamCoroutine, StreamT
from .utils.futures import maybe_async
from .utils.logging import get_logger

if typing.TYPE_CHECKING:
    from .app import App
else:
    class App: ...  # noqa

__all__ = ['Event', 'Channel']
logger = get_logger(__name__)

USE_EXISTING_KEY = object()
USE_EXISTING_VALUE = object()


class Event(EventT):
    """An event received on a channel.

    Notes:
        - Events are delivered to channels/topics::

            async for event in channel:
                ...

        - Streams consume channels and iterates as ``event.value``::

            async for value in channel.stream()  # value is event.value
                ...

        - Stream can also iterate over original event::

            async for event in channel.stream.events():
                ...

        - And you can also access the current_event related to a value
          in a stream::

            stream = channel.stream()
            async for value in stream:
                event = stream.current_event

    You can retrieve the current event in a stream to:

        - Get access to the serialized key+value.
        - Get access to message properties like, what topic+partition
          the value was received on, or its offset.

    Note that if you want access to both key and value, you should use
    ``stream.items()`` instead::

        async for key, value in stream.items():
            ...
    """

    def __init__(self,
                 app: AppT,
                 key: K,
                 value: V,
                 message: Message) -> None:
        self.app: AppT = app
        self.key: K = key
        self.value: V = value
        self.message: Message = message

    async def send(self, channel: Union[str, ChannelT],
                   key: K = USE_EXISTING_KEY,
                   value: V = USE_EXISTING_VALUE,
                   partition: int = None,
                   key_serializer: CodecArg = None,
                   value_serializer: CodecArg = None,
                   callback: MessageSentCallback = None,
                   force: bool = False) -> Awaitable[RecordMetadata]:
        """Send object to channel."""
        if key is USE_EXISTING_KEY:
            key = self.key
        if value is USE_EXISTING_VALUE:
            value = self.value
        return await self._send(
            channel, key, value, partition,
            key_serializer, value_serializer, callback,
            force=force,
        )

    async def forward(self, channel: Union[str, ChannelT],
                      key: K = USE_EXISTING_KEY,
                      value: V = USE_EXISTING_VALUE,
                      partition: int = None,
                      key_serializer: CodecArg = None,
                      value_serializer: CodecArg = None,
                      callback: MessageSentCallback = None,
                      force: bool = False) -> Awaitable[RecordMetadata]:
        """Forward original message (will not be reserialized)."""
        if key is USE_EXISTING_KEY:
            key = self.message.key
        if value is USE_EXISTING_VALUE:
            value = self.message.value
        return await self._send(
            channel, key, value, partition,
            key_serializer, value_serializer, callback,
            force=force,
        )

    async def _send(self, channel: Union[str, ChannelT],
                    key: K = None,
                    value: V = None,
                    partition: int = None,
                    key_serializer: CodecArg = None,
                    value_serializer: CodecArg = None,
                    callback: MessageSentCallback = None,
                    force: bool = False) -> Awaitable[RecordMetadata]:
        return await cast(App, self.app)._maybe_attach(
            channel, key, value, partition,
            key_serializer, value_serializer, callback,
            force=force)

    def _attach(
            self,
            channel: Union[ChannelT, str],
            key: K = None,
            value: V = None,
            partition: int = None,
            key_serializer: CodecArg = None,
            value_serializer: CodecArg = None,
            callback: MessageSentCallback = None) -> Awaitable[RecordMetadata]:
        return cast(App, self.app)._send_attached(
            self.message, channel, key, value,
            partition=partition,
            key_serializer=key_serializer,
            value_serializer=value_serializer,
            callback=callback,
        )

    async def ack(self) -> None:
        message = self.message
        # decrement the reference count
        message.decref()
        # if no more references, ack message
        if not message.refcount:
            await self.app.consumer.ack(message)

    def __repr__(self) -> str:
        return f'{type(self).__name__}: k={self.key!r} v={self.value!r}'

    async def __aenter__(self) -> EventT:
        return self

    async def __aexit__(self,
                        exc_type: Type[Exception],
                        exc_val: Exception,
                        exc_tb: TracebackType) -> None:
        await self.ack()


class Channel(ChannelT):
    app: AppT
    key_type: ModelArg
    value_type: ModelArg
    loop: asyncio.AbstractEventLoop = None
    is_iterator: bool
    clone_shares_queue: bool = True

    _queue: asyncio.Queue = None

    def __init__(self, app: AppT,
                 *,
                 key_type: ModelArg = None,
                 value_type: ModelArg = None,
                 is_iterator: bool = False,
                 queue: asyncio.Queue = None,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.app = app
        self.loop = loop
        self.key_type = key_type
        self.value_type = value_type
        self.is_iterator = is_iterator
        self._queue = queue
        self.deliver = self._compile_deliver()  # type: ignore
        self.decode = self._compile_decode()    # type: ignore

    @property
    def queue(self) -> asyncio.Queue:
        if self._queue is None:
            # this should only be set after clone = channel.__aiter__()
            # which means the loop is not accessed by merely defining
            # a channel at module scope.
            self._queue = self.app.FlowControlQueue(
                maxsize=1,
                loop=self.loop,
                clear_on_resume=True,
            )
        return self._queue

    def clone(self, *, is_iterator: bool = None) -> ChannelT:
        return type(self)(
            is_iterator=(is_iterator if is_iterator is not None
                         else self.is_iterator),
            **self._clone_args())

    def _clone_args(self) -> Mapping:
        return {
            'app': self.app,
            'loop': self.loop,
            'key_type': self.key_type,
            'value_type': self.value_type,
            'queue': self.queue if self.clone_shares_queue else None,
        }

    def stream(self, coroutine: StreamCoroutine = None,
               **kwargs: Any) -> StreamT:
        """Create stream from channel."""
        return self.app.stream(self, coroutine, **kwargs)

    async def send(
            self,
            key: K = None,
            value: V = None,
            partition: int = None,
            key_serializer: CodecArg = None,
            value_serializer: CodecArg = None,
            callback: MessageSentCallback = None,
            force: bool = False) -> Awaitable[RecordMetadata]:
        """Send message to channel."""
        if not force:
            event = current_event()
            if event is not None:
                return cast(Event, event)._attach(
                    self, key, value,
                    partition=partition,
                    key_serializer=key_serializer,
                    value_serializer=value_serializer,
                    callback=callback,
                )
        return await self._send_now(
            key, value,
            partition=partition,
            key_serializer=key_serializer,
            value_serializer=value_serializer,
            callback=callback,
        )

    def as_future_message(
            self,
            key: K = None,
            value: V = None,
            partition: int = None,
            key_serializer: CodecArg = None,
            value_serializer: CodecArg = None,
            callback: MessageSentCallback = None) -> FutureMessage:
        return FutureMessage(PendingMessage(
            self,
            self.prepare_key(key, key_serializer),
            self.prepare_value(value, value_serializer),
            key_serializer=key_serializer,
            value_serializer=value_serializer,
            partition=partition,
            callback=callback,
        ))

    async def _send_now(
            self,
            key: K = None,
            value: V = None,
            partition: int = None,
            key_serializer: CodecArg = None,
            value_serializer: CodecArg = None,
            callback: MessageSentCallback = None) -> Awaitable[RecordMetadata]:
        return await self.publish_message(self.as_future_message(
            key, value, partition, key_serializer, value_serializer, callback))

    async def publish_message(
            self, fut: FutureMessage,
            wait: bool = True) -> Awaitable[RecordMetadata]:
        event = self._create_event(fut.message.key, fut.message.value)
        await self.put(event)
        return await self._finalize_message(
            fut, RecordMetadata(None, None, None, None))

    async def _finalize_message(
            self, fut: FutureMessage, result: RecordMetadata) -> FutureMessage:
        fut.set_result(result)
        if fut.message.callback:
            await maybe_async(fut.message.callback(fut))
        return fut

    def send_soon(
            self,
            key: K = None,
            value: V = None,
            partition: int = None,
            key_serializer: CodecArg = None,
            value_serializer: CodecArg = None,
            callback: MessageSentCallback = None) -> Awaitable[RecordMetadata]:
        """Send message to channel (asynchronous version).

        Notes:
            This can be used from non-async functions, but with the caveat
            that sending of the message will be scheduled in the event loop.
            This will buffer up the message, making backpressure a concern.
        """
        return self.app.send_soon(
            self, key, value, partition,
            key_serializer, value_serializer, callback)

    async def maybe_declare(self) -> None:
        ...

    async def declare(self) -> None:
        ...

    def prepare_key(self, key: K, key_serializer: CodecArg) -> Any:
        return key

    def prepare_value(self, value: V, value_serializer: CodecArg) -> Any:
        return value

    async def decode(self, message: Message) -> EventT:
        ...  # closure compiled at __init__

    def _compile_decode(self) -> Callable[[Message], Awaitable[EventT]]:
        app = self.app
        key_type = self.key_type
        value_type = self.value_type
        loads_key = app.serializers.loads_key
        loads_value = app.serializers.loads_value
        create_event = self._create_event

        async def decode(message: Message) -> Any:
            try:
                k = loads_key(key_type, message.key)
            except KeyDecodeError as exc:
                await self.on_key_decode_error(exc, message)
            else:
                try:
                    v = loads_value(value_type, message.value)
                except ValueDecodeError as exc:
                    await self.on_value_decode_error(exc, message)
                else:
                    return create_event(k, v, message)
        return decode

    async def deliver(self, message: Message) -> None:
        ...  # closure compiled at __init__

    def _compile_deliver(self) -> Callable[[Message], Awaitable[None]]:
        app = self.app
        key_type = self.key_type
        value_type = self.value_type
        loads_key = app.serializers.loads_key
        loads_value = app.serializers.loads_value
        create_event = self._create_event
        put = None

        async def deliver(message: Message) -> None:
            nonlocal put
            if put is None:
                # NOTE circumvents self.put, using queue directly
                put = self.queue.put
            try:
                k = loads_key(key_type, message.key)
            except KeyDecodeError as exc:
                await self.on_key_decode_error(exc, message)
            else:
                try:
                    v = loads_value(value_type, message.value)
                except ValueDecodeError as exc:
                    await self.on_value_decode_error(exc, message)
                else:
                    await put(create_event(k, v, message))
        return deliver

    def _create_event(self, key: K, value: V,
                      message: Message = None) -> EventT:
        return Event(self.app, key, value, message)

    async def put(self, value: Any) -> None:
        if not self.is_iterator and not self.clone_shares_queue:
            raise RuntimeError(
                'Cannot put on this channel before aiter(channel)')
        await self.queue.put(value)

    async def get(self) -> Any:
        return await self.queue.get()

    async def on_key_decode_error(
            self, exc: Exception, message: Message) -> None:
        logger.exception('Cannot decode key: %r: %r', message.key, exc)

    async def on_value_decode_error(
            self, exc: Exception, message: Message) -> None:
        logger.exception('Cannot decode value for key=%r (%r): %r',
                         message.key, message.value, exc)

    def __aiter__(self) -> ChannelT:
        return self if self.is_iterator else self.clone(is_iterator=True)

    async def __anext__(self) -> EventT:
        if not self.is_iterator:
            raise RuntimeError('Need to call channel.__aiter__()')
        return await self.queue.get()

    def __repr__(self) -> str:
        return f'<{self.label}>'

    @property
    def label(self) -> str:
        return f'{type(self).__name__}: {self}'

    def __str__(self) -> str:
        return f'{id(self):#x}'
