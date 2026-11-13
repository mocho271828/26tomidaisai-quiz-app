"""redis-py 6.xのTLS接続に検証済みContextを再利用する境界。"""

import ssl
from socket import socket

from redis.asyncio.connection import SSLConnection as AsyncSSLConnection
from redis.connection import SSLConnection as SyncSSLConnection


class SharedAsyncSSLConnection(AsyncSSLConnection):
    def __init__(self, *, shared_ssl_context: ssl.SSLContext, **kwargs: object) -> None:
        super().__init__(**kwargs)
        # redis-py 6.xのContext cacheへ起動時生成済みの値を注入する。
        # 接続ごとのCA読み込みでevent loopを止めず、TLSのhostname検証は維持する。
        self.ssl_context.context = shared_ssl_context


class SharedSyncSSLConnection(SyncSSLConnection):
    def __init__(self, *, shared_ssl_context: ssl.SSLContext, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._shared_ssl_context = shared_ssl_context

    def _wrap_socket_with_ssl(self, sock: socket) -> ssl.SSLSocket:
        return self._shared_ssl_context.wrap_socket(sock, server_hostname=self.host)
