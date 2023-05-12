# stdlib
import binascii
import os
import socketserver
from typing import Optional

# third party
import gevent
from pydantic import validator
from zmq import Context
from zmq import Socket
import zmq.green as zmq

# relative
from ...serde.serializable import serializable
from ...types.syft_object import SYFT_OBJECT_VERSION_1
from ...types.syft_object import SyftObject
from ...types.uid import UID
from .base_queue import AbstractMessageHandler
from .base_queue import QueueClient
from .base_queue import QueueClientConfig
from .base_queue import QueueConfig
from .base_queue import QueuePublisher
from .base_queue import QueueSubscriber


@serializable()
class ZMQPublisher(QueuePublisher):
    def __init__(self, address: str) -> None:
        ctx = zmq.Context.instance()
        self.address = address
        self._publisher = ctx.socket(zmq.PUB)
        self._publisher.bind(address)

    def send(self, message: bytes, queue_name: str):
        try:
            queue_name_bytes = queue_name.encode()
            message_list = [queue_name_bytes, message]
            self._publisher.send_multipart(message_list)
            print("Message Queued Successfully !")
        except zmq.ZMQError as e:
            if e.errno == zmq.ETERM:
                print("Connection Interupted....")
            else:
                raise e

    def close(self):
        self._publisher.close()


@serializable(attrs=["_subscriber"])
class ZMQSubscriber(QueueSubscriber):
    def __init__(
        self,
        message_handler: AbstractMessageHandler,
        address: str,
        queue_name: str,
    ) -> None:
        self.address = address
        self.message_handler = message_handler
        self.queue_name = queue_name
        self.post_init()

    def post_init(self):
        ctx = zmq.Context.instance()
        self._subscriber = ctx.socket(zmq.SUB)

        self.recv_thread = None
        self._subscriber.connect(self.address)

        self._subscriber.setsockopt_string(zmq.SUBSCRIBE, self.queue_name)

    def receive(self):
        try:
            message_list = self._subscriber.recv_multipart()
            message = message_list[1]
            print("Message Received Successfully !")
        except zmq.ZMQError as e:
            if e.errno == zmq.ETERM:
                print("Subscriber connection Terminated")
            else:
                raise e

        self.message_handler.handle_message(message=message)

    def _run(self):
        while True:
            self.receive()

    def run(self):
        self.recv_thread = gevent.spawn(self._run)
        self.recv_thread.start()

    def close(self):
        if self.recv_thread is not None:
            self.recv_thread.kill()
        self._subscriber.close()


@serializable()
class ZMQClientConfig(SyftObject, QueueClientConfig):
    __canonical_name__ = "ZMQClientConfig"
    __version__ = SYFT_OBJECT_VERSION_1

    id: Optional[UID]
    pub_addr: Optional[str]
    sub_addr: Optional[str]

    @staticmethod
    def _get_free_tcp_addr():
        host = "127.0.0.1"
        with socketserver.TCPServer((host, 0), None) as s:
            free_port = s.server_address[1]

        addr = f"tcp://{host}:{free_port}"
        return addr

    @validator("pub_addr", pre=True, always=True)
    def make_pub_addr(cls, v: Optional[str]) -> str:
        return cls._get_free_tcp_addr() if v is None else v

    @validator("sub_addr", pre=True, always=True)
    def make_sub_addr(cls, v: Optional[str]) -> str:
        return cls._get_free_tcp_addr() if v is None else v


@serializable(attrs=["pub_addr", "sub_addr", "_context"])
class ZMQClient(QueueClient):
    def __init__(self, config: QueueClientConfig):
        self.pub_addr = config.pub_addr
        self.sub_addr = config.sub_addr
        self._context = None
        self.logger_thread = None
        self.thread = None

    @property
    def context(self):
        if self._context is None:
            self._context = zmq.Context.instance()
        return self._context

    @staticmethod
    def _setup_monitor(ctx: Context):
        mon_addr = "inproc://%s" % binascii.hexlify(os.urandom(8))
        mon_pub = ctx.socket(zmq.PAIR)
        mon_sub = ctx.socket(zmq.PAIR)

        mon_sub.linger = mon_sub.linger = 0

        mon_sub.hwm = mon_sub.hwm = 1
        mon_pub.bind(mon_addr)
        mon_sub.connect(mon_addr)
        return mon_pub, mon_sub, mon_addr

    def _setup_connections(self):
        self.xsub = self.context.socket(zmq.XSUB)
        self.xpub = self.context.socket(zmq.XPUB)

        self.xsub.connect(self.pub_addr)
        self.xpub.bind(self.sub_addr)

        self.mon_pub, self.mon_sub, self.mon_addr = self._setup_monitor(self.context)

    @staticmethod
    def _start_logger(mon_sub: Socket):
        print("Started Logging.")
        while True:
            try:
                mon_sub.recv_multipart()
                # message_str = " ".join(mess.decode() for mess in message_bytes)
                # print(message_str)
            except zmq.ZMQError as e:
                if e.errno == zmq.ETERM:
                    break  # Interrupted

    @staticmethod
    def _start(
        in_socket: Socket,
        out_socket: Socket,
        mon_socket: Socket,
        in_prefix: bytes,
        out_prefix: bytes,
    ):
        poller = zmq.Poller()
        poller.register(in_socket, zmq.POLLIN)
        poller.register(out_socket, zmq.POLLIN)

        while True:
            events = dict(poller.poll())

            if in_socket in events:
                message = in_socket.recv_multipart()
                out_socket.send_multipart(message)
                mon_socket.send_multipart([in_prefix] + message)

            if out_socket in events:
                message = out_socket.recv_multipart()
                in_socket.send_multipart(message)
                mon_socket.send_multipart([out_prefix] + message)

    def start(self, in_prefix: bytes = b"", out_prefix: bytes = b""):
        self._setup_connections()
        self.logger_thread = gevent.spawn(self._start_logger, self.mon_sub)
        self.thread = gevent.spawn(
            self._start,
            self.xpub,
            self.xsub,
            self.mon_pub,
            in_prefix,
            out_prefix,
        )

        self.logger_thread.start()
        self.thread.start()

    def check_logs(self, timeout: Optional[int]):
        try:
            if self.logger_thread:
                self.logger_thread.join(timeout=timeout)
        except KeyboardInterrupt:
            pass

    def close(self):
        self.thread.kill()
        self.logger_thread.kill()
        self.context.destroy()
        self.xpub.close()
        self.xpub.close()
        self.mon_pub.close()
        self.mon_sub.close()


@serializable()
class ZMQQueueConfig(QueueConfig):
    subscriber = ZMQSubscriber
    publisher = ZMQPublisher
    client_config = ZMQClientConfig
    client_type = ZMQClient
