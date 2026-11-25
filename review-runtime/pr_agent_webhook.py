"""按已配置的仓库加载连接参数，交给 PR-Agent 官方 Gitea 路由。"""
import copy
import json
import os
from pathlib import Path
import re
import time

from fastapi import HTTPException, Request
import httpx
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, Response
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.config_loader import global_settings
from pr_agent.servers import gitea_app
from pr_agent.servers.request_body_limit import create_server_app

ROOT = Path(os.environ.get('PR_AGENT_CHANNEL_DIR', '/app/pr-agent-channels'))
CHANNEL = re.compile(r'/pr-agent/([1-9][0-9]*)/api/v1/gitea_webhooks')
# 仅用于一键接入的签名投递测试。审查和评论由官方路由处理。
receipts = {}
GITEA_HEADERS = ('content-type', 'x-gitea-signature', 'x-gitea-event', 'x-gitea-event-type', 'x-gitea-delivery')


class RequestSettings:
    def __deepcopy__(self, memo):
        return copy.deepcopy(context['settings'], memo)


# 官方入口在验签后复制默认配置，为后台任务建立独立上下文。
# 此处让它复制当前仓库的配置。同进程处理其他仓库时不会共享密钥或模型配置。
gitea_app.global_settings = RequestSettings()


def channel_config(channel):
    try:
        return json.loads((ROOT / (channel + '.json')).read_bytes())
    except FileNotFoundError:
        return None


async def forward_to_manager(channel, scope, receive):
    # 切换到其他审查引擎时保留同一 Gitea Webhook 和原始签名字节。
    request = Request(scope, receive)
    headers = {name: request.headers[name] for name in GITEA_HEADERS if name in request.headers}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            reply = await client.post(f'http://backend:5000/api/ai-review/webhooks/{channel}',
                                      headers=headers, content=await request.body())
    except httpx.RequestError:
        return JSONResponse({'detail': 'Manager 审查通道暂不可用'}, status_code=503)
    return Response(reply.content, status_code=reply.status_code, media_type=reply.headers.get('content-type'))


class ChannelSettings:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        match = CHANNEL.fullmatch(scope.get('path', '')) if scope['type'] == 'http' else None
        if not match:
            await self.app(scope, receive, send)
            return

        channel = match[1]
        config = channel_config(channel)
        if not config:
            await JSONResponse({'detail': '仓库通道不存在'}, status_code=404)(scope, receive, send)
            return

        headers = dict(scope['headers'])
        event = headers.get(b'x-gitea-event')
        probe = event == b'push'
        maintenance = Path('/app/review-maintenance/.upgrade-maintenance').is_file()
        if not probe and (not config['enabled'] or maintenance):
            await JSONResponse({'detail': '审查通道未启用'}, status_code=202)(scope, receive, send)
            return
        if not probe and config.get('legacy'):
            reply = await forward_to_manager(channel, scope, receive)
            await reply(scope, receive, send)
            return

        settings = copy.deepcopy(global_settings)
        for name, value in config['settings'].items():
            settings.set(name, value)
        context['settings'] = settings
        scope = dict(scope, path='/api/v1/gitea_webhooks')

        async def record(message):
            if probe and message['type'] == 'http.response.start' and message['status'] == 200:
                receipts[channel] = time.time()
            await send(message)

        await self.app(scope, receive, record)


app = create_server_app(middleware=[Middleware(RawContextMiddleware), Middleware(ChannelSettings)])
# 仅装载官方入口。原始地址从独立服务内部访问时也必须经过通道配置。
official = gitea_app.handle_gitea_webhooks


@app.post('/api/v1/gitea_webhooks')
async def official_webhook(background_tasks: gitea_app.BackgroundTasks, request: gitea_app.Request,
                           response: gitea_app.Response):
    if 'settings' not in context:
        raise HTTPException(status_code=404)
    return await official(background_tasks, request, response)


@app.get('/health')
async def health():
    return {'ready': True}


@app.get('/channels/{channel}')
async def status(channel: int):
    if channel < 1 or not channel_config(str(channel)):
        raise HTTPException(status_code=404)
    return {'received': receipts.get(str(channel), 0)}
