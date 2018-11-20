from sanic_proxy import proxy, ProxyProtocol
from sanic import Sanic

app = Sanic()
proxy(app)

if __name__ == '__main__':
    app.run(protocol=ProxyProtocol)
