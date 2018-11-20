# sanic
sanic 是一款优秀的高性能web框架，基于asyncio模块实现异步，使用uvloop来加速，使用nodejs使用的http协议解析器的Cython封装（httptools）解析http协议。

# sanic_proxy
主要是扩展了sanic的`HttpProtocol`使其能处理代理请求, 而不修改之前的处理逻辑，使其能够同时作为web server服务和代理服务器。感兴趣的童鞋可以基于此设计做出具有web管理界面的代理服务器。

# 工作原理
继承修改sanic的`HttpProtocol`，使之能够像处理正常请求那样处理代理请求，然后使用sanic的中间件来拦截代理请求， 实行代理处理并直接返回, 不影响正常的服务业务

# Usage
```python
from sanic_proxy import proxy, ProxyProtocol
from sanic import Sanic

app = Sanic()
proxy(app)

if __name__ == '__main__':
    app.run(protocol=ProxyProtocol)
```