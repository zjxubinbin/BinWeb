import asyncio
import multiprocessing
import os
import socket
from functools import partial
from http import HTTPStatus

import httptools
import orjson as json
import uvloop

__version__ = '0.0.1'


# TODO:
# 使用更强大的路由组件: sanic-routing
# 支持HTML响应和文件响应

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

NEWLINE = '\r\n'.encode()
DOUBLE_NEWLINE = '\r\n\r\n'.encode()


class HTTPException(Exception):
    def __init__(self, status, msg=None, properties=None):
        self.properties = properties
        self.msg = msg
        self.status = status


class Request:
    def __init__(self):
        self.headers = {}
        self.method = "HEAD"
        self.url = "/"
        self.raw = None
        self.ip = None
        self.path = None

    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, key, value):
        setattr(self, key, value)


class Response:
    def __init__(self):
        self.body = b""
        self.status = 200
        self.msg = ""
        self.headers = {
            'Content-Type': 'text/plain',
            'Server': 'BinServer'
        }

    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def encode(self):
        '''编码所有的响应数据为二进制'''
        http_status = HTTPStatus(self.status)
        http_status_bytes = f"HTTP/1.1 {http_status.value} {http_status.phrase}".encode()
        # http_body_bytes = self.body.encode()  # 如果json解析器用了ujson,这里就要对字符串进行编码
        http_body_bytes = self.body
        # 计算响应的内容长度并将其添加到响应的headers
        self.headers['Content-Length'] = len(http_body_bytes)
        http_headers_bytes = "\r\n".join(
            [f'{k}: {v}' for k, v in self.headers.items()]).encode()
        return http_status_bytes + NEWLINE + \
            http_headers_bytes + DOUBLE_NEWLINE + http_body_bytes


class Context:
    def __init__(self):
        self.req = Request()
        self.resp = Response()
        self.write = None

    def send(self, _):
        self.write(self.resp.encode())

    def check(self, value, status=400, msg='', properties=""):
        if not value:
            self.abort(status=status, msg=msg, properties=properties)

    def abort(self, status, msg="", properties=""):
        raise HTTPException(status=status, msg=msg, properties=properties)

    def __getattr__(self, item):
        return getattr(self.req, item)

    def __getitem__(self, item):
        return getattr(self, item)

    def __setitem__(self, key, value):
        setattr(self, key, value)

    @property
    def headers(self):
        return self.resp.headers

    @property
    def json(self):
        return json.loads(self.body)

    @property
    def body(self):
        return self.resp.body

    @body.setter
    def body(self, value):
        self.resp.body = value

    @property
    def status(self):
        return self.resp.status

    @status.setter
    def status(self, value):
        self.resp.status = value

    @property
    def msg(self):
        return self.resp.msg

    @msg.setter
    def msg(self, value):
        self.resp.msg = value


class HTTPProtocol(asyncio.Protocol):

    def __init__(self, handler, loop):
        self.parser = None
        self.transport = None
        self.handler = handler
        self.loop = loop
        self.ctx = None

    def connection_made(self, transport):
        self.parser = httptools.HttpRequestParser(self)
        self.transport = transport

    def on_url(self, url):
        self.ctx = Context()
        self.ctx.write = self.transport.write
        url = httptools.parse_url(url)
        self.ctx.req.path = url.path.decode()
        self.ctx.req.method = self.parser.get_method().decode()

    def on_header(self, name, value):
        self.ctx.req.headers[name.decode()] = value.decode()

    def on_body(self, body):
        self.ctx.req.raw += body

    def on_message_complete(self):
        # 在事件循环引擎上创建一个任务
        task = self.loop.create_task(self.handler(self.ctx))
        task.add_done_callback(self.ctx.send)
        # print("message_completed")

    def data_received(self, data):
        self.parser.feed_data(data)

    def connection_lost(self, exc):
        self.transport.close()


class App:
    def __init__(self):
        self.workers = []
        self.routes: dict[str, Controller] = {}

    def serve(self, sock):
        loop = asyncio.new_event_loop()
        server = loop.create_server(
            partial(HTTPProtocol, loop=loop, handler=self), sock=sock)
        loop.create_task(server)
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            server.close()
            loop.close()

    def run(self, port=8000, host="0.0.0.0", workers=multiprocessing.cpu_count()):
        pid = os.getpid()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 设置sock为非阻塞模式,详见:
        # https://docs.python.org/zh-cn/3/library/socket.html#socket.socket.setblocking
        sock.setblocking(False)
        sock.bind((host, port))
        os.set_inheritable(sock.fileno(), True)

        try:
            print(f'[{pid}] Listening at: http://{host}:{port}')
            print(f'[{pid}] Total Workers: {workers}')
            mp = multiprocessing.get_context("fork")
            for _ in range(workers):
                worker = mp.Process(
                    target=self.serve, kwargs=dict(sock=sock))
                worker.daemon = True
                worker.start()
                print(f'[{pid}] Worker started with pid: {worker.pid}')
                self.workers.append(worker)
            for worker in self.workers:
                worker.join()
        except KeyboardInterrupt:
            print('\r', end='\r')
            print(f'[{pid}] Server is stopping')
            for worker in self.workers:
                # 终止所有Workers
                worker.terminate()
            print(f'[{pid}] Server stopped successfully!')
        sock.close()

    async def __call__(self, ctx: Context):
        try:
            # 从路由表里边查找路由对应的控制器
            controller: Controller = self.routes.get(ctx.req.path)
            if not controller:
                raise HTTPException(404)
            # 找到对应的路由控制器之后调用处理请求的方法
            await controller(ctx).handler()
        except HTTPException as e:
            ctx.status = e.status
            ctx.body = e.msg or HTTPStatus(e.status).phrase
            ctx.msg = e.properties


class Controller:
    '''路由控制器'''

    def __init__(self, ctx):
        self.ctx: Context = ctx

    async def handler(self):
        # 从当前对象获取HTTP Method对应的方法
        handler = getattr(self, self.ctx.req.method.lower(), None)
        if not handler:
            raise HTTPException(405)
        await handler()


class RESTController(Controller):
    '''用于RESTFul的控制器'''
    async def handler(self):
        self.ctx.headers['Content-Type'] = 'application/json'
        await super().handler()
        self.ctx.body = json.dumps(self.ctx.body)
