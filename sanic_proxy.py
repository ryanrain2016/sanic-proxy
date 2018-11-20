import asyncio
from sanic.server import HttpProtocol, Signal
from sanic.request import Request
from sanic.exceptions import InvalidUsage, PayloadTooLarge
from sanic.response import StreamingHTTPResponse

from sanic import server as sanic_server
from sanic import Sanic, response, app

from httptools.parser.errors import HttpParserUpgrade, HttpParserInvalidURLError, \
            HttpParserError, HttpParserInvalidMethodError
from httptools import HttpRequestParser
from httptools.parser.errors import HttpParserError, HttpParserInvalidMethodError
from httptools import parse_url
from multidict import CIMultiDict

from async_timeout import timeout


class ProxyRequest(Request):
    __slots__ = (
        'app', 'headers', 'version', 'method', '_cookies', 'transport',
        'body', 'parsed_json', 'parsed_args', 'parsed_form', 'parsed_files',
        '_ip', '_parsed_url', 'uri_template', 'stream', '_remote_addr',
        '_socket', '_port', 'raw_url'
    )

    def __init__(self, url_bytes, headers, version, method, transport):
        self.raw_url = url_bytes
        # TODO: Content-Encoding detection
        try:
            self._parsed_url = parse_url(url_bytes)
        except HttpParserInvalidURLError:  
        # connection request url is example.com:433, parse it will raise error
            self._parsed_url = parse_url(b'https://' + url_bytes + b'/')
        self.app = None

        self.headers = headers
        self.version = version
        self.method = method
        self.transport = transport

        # Init but do not inhale
        self.body = []
        self.parsed_json = None
        self.parsed_form = None
        self.parsed_files = None
        self.parsed_args = None
        self.uri_template = None
        self._cookies = None
        self.stream = None

class ProxyProtocol(HttpProtocol):
    __slots__ = (
        # event loop, connection
        'loop', 'transport', 'connections', 'signal',
        # request params
        'parser', 'request', 'url', 'headers',
        # request config
        'request_handler', 'request_timeout', 'response_timeout',
        'keep_alive_timeout', 'request_max_size', 'request_class',
        'is_request_stream', 'router',
        # enable or disable access log purpose
        'access_log',
        # connection management
        '_total_request_size', '_request_timeout_handler',
        '_response_timeout_handler', '_keep_alive_timeout_handler',
        '_last_request_time', '_last_response_time', '_is_stream_handler',
        '_not_paused')
    def __init__(self, *, loop, request_handler, error_handler,
                 signal=Signal(), connections=set(), request_timeout=60,
                 response_timeout=60, keep_alive_timeout=5,
                 request_max_size=None, request_class=None, access_log=True,
                 keep_alive=True, is_request_stream=False, router=None,
                 state=None, debug=False, **kwargs):
        self.loop = loop
        self.transport = None
        self.request = None
        self.parser = None
        self.url = None
        self.headers = None
        self.router = router
        self.signal = signal
        self.access_log = access_log
        self.connections = connections
        self.request_handler = request_handler
        self.error_handler = error_handler
        self.request_timeout = request_timeout
        self.response_timeout = response_timeout
        self.keep_alive_timeout = keep_alive_timeout
        self.request_max_size = request_max_size
        self.request_class = request_class or ProxyRequest
        self.is_request_stream = is_request_stream
        self._is_stream_handler = False
        self._not_paused = asyncio.Event(loop=loop)
        self._total_request_size = 0
        self._request_timeout_handler = None
        self._response_timeout_handler = None
        self._keep_alive_timeout_handler = None
        self._last_request_time = None
        self._last_response_time = None
        self._request_handler_task = None
        self._request_stream_task = None
        self._keep_alive = keep_alive
        self._header_fragment = b''
        self.state = state if state else {}
        if 'requests_count' not in self.state:
            self.state['requests_count'] = 0
        self._debug = debug
        self._not_paused.set()

        self._raw = asyncio.Queue()   # 原始数据都保存
        self._is_proxy = False  # for proxy

    def data_received(self, data):
        self._raw.put_nowait(data)   # 原始数据都保存
        # Check for the request itself getting too large and exceeding
        # memory limits
        self._total_request_size += len(data)
        if self._total_request_size > self.request_max_size:
            exception = PayloadTooLarge('Payload Too Large')
            self.write_error(exception)
        if self._is_proxy:  # 如果是代理过程那么就不要再去parse了
            return
        # Create parser if this is the first time we're receiving data
        if self.parser is None:
            assert self.request is None
            self.headers = []
            self.parser = HttpRequestParser(self)

        # requests count
        self.state['requests_count'] = self.state['requests_count'] + 1

        # Parse request chunk or close connection
        try:
            self.parser.feed_data(data)
        except HttpParserInvalidMethodError as e:  # CONNECT 包
            pass
        except HttpParserUpgrade:  # CONNECT 包
            pass
        except HttpParserError:
            message = 'Bad Request'
            if self._debug:
                message += '\n' + traceback.format_exc()
            exception = InvalidUsage(message)
            self.write_error(exception)
        

    def cleanup(self):
        """This is called when KeepAlive feature is used,
        it resets the connection in order for it to be able
        to handle receiving another request on the same connection."""
        self.parser = None
        self.request = None
        self.url = None
        self.headers = None
        self._request_handler_task = None
        self._request_stream_task = None
        self._total_request_size = 0
        self._is_stream_handler = False

        self._raw = asyncio.Queue()  # for proxy 清理
        self._is_proxy = False      # for proxy

    def on_headers_complete(self):
        self.request = self.request_class(
            url_bytes=self.url,
            headers=CIMultiDict(self.headers),
            version=self.parser.get_http_version(),
            method=self.parser.get_method().decode(),
            transport=self.transport
        )
        self.request['_raw'] = self._raw  # 原始数据包交给request处理函数
        # Remove any existing KeepAlive handler here,
        # It will be recreated if required on the new request.
        if self._keep_alive_timeout_handler:
            self._keep_alive_timeout_handler.cancel()
            self._keep_alive_timeout_handler = None
        if self.is_request_stream:
            self._is_stream_handler = self.router.is_stream_handler(
                self.request)
            if self._is_stream_handler:
                self.request.stream = asyncio.Queue()
                self.execute_request_handler()

    def on_message_complete(self):
        # Entire request (headers and whole body) is received.
        # We can cancel and remove the request timeout handler now.
        if self._request_timeout_handler:
            self._request_timeout_handler.cancel()
            self._request_timeout_handler = None
        if self.is_request_stream and self._is_stream_handler:
            self._request_stream_task = self.loop.create_task(
                self.request.stream.put(None))
            return
        self.request.body = b''.join(self.request.body)
        if self.request.method == 'CONNECT' or self.request.raw_url.startswith(b'http'):
            self._is_proxy = True      # CONNET 包或者url是完整url的包就是代理包
        self.execute_request_handler()

    def write_response(self, response):
        """
        Writes response content synchronously to the transport.
        """
        if self._response_timeout_handler:
            self._response_timeout_handler.cancel()
            self._response_timeout_handler = None
        try:
            keep_alive = self.keep_alive
            self.transport.write(
                response.output(
                    self.request.version, keep_alive,
                    self.keep_alive_timeout))
            self.log_response(response)
        except AttributeError:
            logger.error('Invalid response object for url %s, '
                         'Expected Type: HTTPResponse, Actual Type: %s',
                         self.url, type(response))
            self.write_error(ServerError('Invalid response type'))
        except RuntimeError:
            if self._debug:
                logger.error('Connection lost before response written @ %s',
                             self.request.ip)
            keep_alive = False
        except Exception as e:
            self.bail_out(
                "Writing response failed, connection closed {}".format(
                    repr(e)))
        finally:
            if not keep_alive:
                self.transport.close()
                self.transport = None
            else:
                self._keep_alive_timeout_handler = self.loop.call_later(
                    self.keep_alive_timeout,
                    self.keep_alive_timeout_callback)
                self._last_response_time = sanic_server.current_time

                if self.request.method == 'CONNECT':
                    return   #https 代理的握手请求完了，可不能清理，后面还要代理的
                self.cleanup()


class ProxyResponse(StreamingHTTPResponse):
    def get_headers(
            self, version="1.1", keep_alive=False, keep_alive_timeout=None):
        # This is all returned in a kind-of funky way
        # We tried to make this as fast as possible in pure python
        timeout_header = b''
        if keep_alive and keep_alive_timeout is not None:
            timeout_header = b'Keep-Alive: %d\r\n' % keep_alive_timeout

        # self.headers['Transfer-Encoding'] = 'chunked'   #这些头返回时不需要
        # self.headers.pop('Content-Length', None)
        # self.headers['Content-Type'] = self.headers.get(
        #     'Content-Type', self.content_type)

        headers = self._parse_headers()

        if self.status is 200:
            status = b'Connection established'
        else:
            status = http.STATUS_CODES.get(self.status)

        return (b'HTTP/%b %d %b\r\n'
                b'%b'
                b'%b\r\n') % (
                   version.encode(),
                   self.status,
                   status,
                   timeout_header,
                   headers
               )

    async def write(self, data):
        """Writes a chunk of data to the streaming response.

        :param data: bytes-ish data to be written.
        """
        if type(data) != bytes:
            data = self._encode_body(data)

        # self.protocol.push_data(
        #     b"%x\r\n%b\r\n" % (len(data), data))
        self.protocol.push_data(data)  # return directly
        await self.protocol.drain()

    async def stream(
            self, version="1.1", keep_alive=False, keep_alive_timeout=None):
        """Streams headers, runs the `streaming_fn` callback that writes
        content to the response body, then finalizes the response body.
        """
        headers = self.get_headers(
            version, keep_alive=keep_alive,
            keep_alive_timeout=keep_alive_timeout)
        self.protocol.push_data(headers)
        await self.protocol.drain()
        await self.streaming_fn(self)
        # self.protocol.push_data(b'0\r\n\r\n')
        # no need to await drain here after this write, because it is the
        # very last thing we write and nothing needs to wait for it.

class RawResponse(StreamingHTTPResponse):
    async def write(self, data):
        """Writes a chunk of data to the streaming response.

        :param data: bytes-ish data to be written.
        """
        if type(data) != bytes:
            data = self._encode_body(data)

        self.protocol.push_data(data)  # return directly
        await self.protocol.drain()

    async def stream(
            self, version="1.1", keep_alive=False, keep_alive_timeout=None):
        await self.streaming_fn(self)

def stream(
        streaming_fn, status=200, headers=None,
        content_type="text/plain; charset=utf-8"):
    return ProxyResponse(
        streaming_fn,
        headers=headers,
        content_type=content_type,
        status=status
    )

def raw_stream(
        streaming_fn, status=200, headers=None,
        content_type="text/plain; charset=utf-8"):
    return RawResponse(
        streaming_fn,
        headers=headers,
        content_type=content_type,
        status=status
    )

async def proxy_middleware(request):
    req_transport = request['_raw']
    if request.method == 'CONNECT':  # https/tls/websocket代理
        while not req_transport.empty():
            await req_transport.get()
            req_transport.task_done()
        host = request._parsed_url.host
        port = request._parsed_url.port
        async with timeout(30) as cm:
            reader, writer = await asyncio.open_connection(
                host, port)
        if cm.expired:
            return response.text(status=502)
        async def streaming_fn(r):
            async def req():
                while True:
                    chunk = await req_transport.get()
                    if chunk is None:
                        break
                    writer.write(chunk)
                    req_transport.task_done()
            async def rep():
                while True:
                    chunk = await reader.read(8192)
                    if not chunk:
                        break
                    await r.write(chunk)
            done, pending = await asyncio.wait([req(), rep()], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        return stream(streaming_fn, content_type='text/plain')

    if request.raw_url.startswith(b'http'): # http proxy
        host = request._parsed_url.host.decode()
        port = request._parsed_url.port or 80
        async with timeout(30) as cm:
            reader, writer = await asyncio.open_connection(
                host, port)
        if cm.expired:
            return response.text(status=502)
        raw_req = b''
        while not req_transport.empty():
            raw_req += await req_transport.get()
            req_transport.task_done()
        writer.write(raw_req)
        async def streaming_fn(r):
            while True:
                chunk = await reader.read(8192)
                if not chunk:
                    break
                await r.write(chunk)
        return raw_stream(streaming_fn)

    del request['_raw']



def proxy(user_app):
    user_app.middleware('request')(proxy_middleware)

if __name__ == '__main__':
    app = Sanic()
    proxy(app)
    app.run(port = 12345, protocol=ProxyProtocol)