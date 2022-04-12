from BinWeb import App, RESTController


class IndexController(RESTController):
    async def get(self):
        self.ctx.body = {"msg": "这里是首页"}


app = App()
app.routes = {
    # 目前只支持静态路由
    '/': IndexController
}

app.run()
