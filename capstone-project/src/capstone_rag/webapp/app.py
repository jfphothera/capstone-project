"""The local application: one process, one API, one set of workers.

Launch with ``python run_app.py``. The same process serves the API, the
representative workspace, the customer chatbot, and owns the single inference
worker and the arrival simulator.

Why FastAPI and not a second Streamlit page: the existing pages are thin call
sites over `capstone_rag`, and the problems with them were layout, selection,
staleness and process ownership rather than anything in the business logic.
An HTTP boundary fixes all four without touching the modules underneath - and
it makes the approval gates testable as *requests*, which is the thing a UI
button must never be trusted to do on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import generation
from .routers import (
    analytics as analytics_router, cases, chat as chat_router, erp,
    evaluation, ops, quality, trace,
)
from .runtime import Runtime, get_runtime, set_runtime

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "static")


def _developer_token(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    value=os.environ.get('CAPSTONE_DEVELOPER_TOKEN')
    secret_file=os.environ.get('CAPSTONE_DEVELOPER_TOKEN_FILE')
    if value and secret_file:
        raise RuntimeError('Configure the developer token by environment or protected file, not both')
    if secret_file:
        from pathlib import Path
        path=Path(secret_file)
        if not path.is_file():raise RuntimeError('Configured developer token file is unavailable')
        value=path.read_text(encoding='utf-8').strip()
    return value or None


def create_app(*, prefer_llm: Optional[bool] = None, store=None, agent=None,
               representative: str = "rep.jordan",
               start_workers: bool = True,
               developer_token: Optional[str] = None) -> FastAPI:
    """Build the application.

    `start_workers=False` is for tests: the routes and the rules are exactly
    the same, but no thread is started, so a test suite does not spawn an
    inference worker per test module.
    """
    if prefer_llm is None:
        # The deployed laptop is Gateway-only.  A temporarily offline Gateway
        # must fail requests visibly; startup must never convert that outage
        # into a process-lifetime deterministic/local fallback.
        from .. import gateway_transport
        prefer_llm = True if gateway_transport.enabled() else generation.llm_available()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = Runtime(prefer_llm=prefer_llm, store=store, agent=agent,
                          representative=representative)
        set_runtime(runtime)
        runtime.bus.bind_loop(asyncio.get_running_loop())
        if start_workers:
            runtime.start(asyncio.get_running_loop())
        try:
            yield
        finally:
            if start_workers:
                runtime.stop()
            set_runtime(None)

    from ..version import get_version
    app = FastAPI(title="Capstone customer service (simulated)",
                  version=get_version(), lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.developer_token = _developer_token(developer_token)
    app.state.developer_sessions = {}
    # Existing route tests build a workerless app without external auth. A
    # production/default app with no configured token denies developer APIs.
    app.state.developer_test_bypass = bool(not start_workers and not app.state.developer_token)

    @app.middleware('http')
    async def no_stale_ui(request:Request,call_next):
        response=await call_next(request)
        if request.url.path.startswith('/static/') or response.headers.get('content-type','').startswith('text/html'):
            response.headers['Cache-Control']='no-cache, max-age=0, must-revalidate'
        return response

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def _permission_error(request:Request,exc:PermissionError):
        return JSONResponse(status_code=403,content={'detail':str(exc)})

    app.include_router(cases.router)
    app.include_router(erp.router)
    app.include_router(chat_router.router)
    app.include_router(quality.router)
    app.include_router(analytics_router.router)
    app.include_router(ops.router)
    from .developer_auth import require_developer,require_viewer
    developer_dependencies=[Depends(require_developer)]
    app.include_router(evaluation.router,dependencies=developer_dependencies)
    app.include_router(trace.router,dependencies=developer_dependencies)
    from .routers import workflows
    app.include_router(workflows.router)
    from .routers import coordination
    app.include_router(coordination.router)
    from .routers import ai
    # Retired configuration routes disclose no state and always return the
    # Gateway-only 409 contract, including before developer authentication.
    app.include_router(ai.router)
    from .routers import experiments
    # The Testing Lab is a synthetic, isolated research tool on the dedicated
    # Capstone laptop. It intentionally has no browser credential gate.
    app.include_router(experiments.router)
    from .routers import lab
    app.include_router(lab.router)
    if os.environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')=='1':
        from .routers import cluster
        app.include_router(cluster.router)
    from .routers import arena
    app.include_router(arena.router,dependencies=[Depends(require_viewer)])

    @app.post('/api/arena/session')
    async def arena_session(request:Request):
        from .developer_auth import create_session,COOKIE,VIEWER_SESSION_SECONDS
        body=await request.json()
        try:
            session_id,csrf=create_session(request,str(body.get('token') or ''),role='viewer')
        except Exception:
            return JSONResponse(status_code=403,content={'detail':'Developer authorization required'})
        response=JSONResponse({'ok':True,'role':'viewer','expires_in':VIEWER_SESSION_SECONDS,
                               'csrf_token':csrf})
        response.set_cookie(COOKIE,session_id,httponly=True,samesite='strict',
                            secure=request.url.scheme=='https',max_age=VIEWER_SESSION_SECONDS)
        return response

    @app.post('/api/developer/session')
    async def developer_session(request:Request):
        from .developer_auth import create_session,COOKIE,SESSION_SECONDS
        body=await request.json()
        session_id,csrf=create_session(request,str(body.get('token') or ''),role='developer')
        response=JSONResponse({'ok':True,'role':'developer','expires_in':SESSION_SECONDS,'csrf_token':csrf})
        response.set_cookie(COOKIE,session_id,httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=SESSION_SECONDS)
        return response

    @app.get('/api/developer/session')
    async def developer_session_status(request:Request):
        from .developer_auth import renew_developer_session,COOKIE,SESSION_SECONDS
        row=renew_developer_session(request)
        response=JSONResponse({'authenticated':bool(row and row['role']=='developer'),
                'role':row.get('role') if row else None,'csrf_token':row.get('csrf') if row else None,
                'expires_at':row.get('expires_at') if row else None,'expires_in':SESSION_SECONDS if row and row['role']=='developer' else None})
        if row and row['role']=='developer':
            response.set_cookie(COOKIE,request.cookies.get(COOKIE) or '',httponly=True,samesite='strict',
                                secure=request.url.scheme=='https',max_age=SESSION_SECONDS)
        return response

    @app.post('/api/developer/logout')
    async def developer_logout(request:Request):
        from .developer_auth import require_csrf,COOKIE
        require_csrf(request,request.headers.get('x-csrf-token'))
        app.state.developer_sessions.pop(request.cookies.get(COOKIE) or '',None)
        response=JSONResponse({'ok':True})
        response.delete_cookie(COOKIE,samesite='strict',secure=request.url.scheme=='https')
        return response

    @app.post('/api/arena/logout')
    async def arena_logout(request:Request):
        from .developer_auth import require_csrf,COOKIE
        require_csrf(request,request.headers.get('x-csrf-token'))
        import hashlib
        get_runtime().store.execute('DELETE FROM arena_viewer_sessions WHERE session_hash=?',
            (hashlib.sha256((request.cookies.get(COOKIE) or '').encode()).hexdigest(),))
        app.state.developer_sessions.pop(request.cookies.get(COOKIE) or '',None)
        response=JSONResponse({'ok':True})
        response.delete_cookie(COOKIE,samesite='strict',secure=request.url.scheme=='https')
        return response

    @app.get("/api/health")
    def health(request:Request):
        try:
            rt = get_runtime()
        except RuntimeError:
            return {"ok": False, "reason": "runtime not started"}
        from .developer_auth import authorized
        # Health is an application-liveness endpoint.  Gateway verification is
        # refreshed in the background so an offline Gateway cannot make this
        # endpoint hang until the SDK's network timeout.
        snapshot=rt.snapshot(probe_model=False)
        degraded=snapshot['model'].get('verification')!='verified' or not snapshot['model'].get('available')
        if authorized(request,request.headers.get('x-capstone-developer-token')):
            return {"ok":True,"degraded":degraded,"version":get_version(),**snapshot}
        # Preserve representative UI status and the external adapter's `ok`
        # probe without exposing case IDs, DB paths, endpoint URLs or errors.
        queue=snapshot['queue'];sim=snapshot['simulator'];model=snapshot['model']
        return {'ok':True,'degraded':degraded,'version':get_version(),'representative':snapshot['representative'],
            'queue':{**{k:queue.get(k) for k in ('queued','completed','failed','dropped_full','running','maxsize')},
                     'current':'active task' if queue.get('current') else None},
            'simulator':{k:sim.get(k) for k in ('running','pool_available','next_due_in')},
            'model':{k:model.get(k) for k in ('available','model','prefer_llm')}}

    @app.get('/api/health/details',dependencies=developer_dependencies)
    def health_details():
        snapshot=get_runtime().snapshot(probe_model=False)
        return {'ok':True,'degraded':snapshot['model'].get('verification')!='verified' or not snapshot['model'].get('available'),**snapshot}

    if os.path.isdir(STATIC_DIR):
        class NoCacheStatic(StaticFiles):
            async def get_response(self,path,scope):
                response=await super().get_response(path,scope)
                response.headers['Cache-Control']='no-cache, max-age=0, must-revalidate'
                return response
        app.mount("/static", NoCacheStatic(directory=STATIC_DIR),
                  name="static")

        @app.get("/")
        def index():
            return FileResponse(os.path.join(STATIC_DIR, "index.html"))

        @app.get("/chat")
        def chat_page():
            return FileResponse(os.path.join(STATIC_DIR, "chat.html"))

        @app.get('/trace')
        def trace_page(request:Request):
            from .developer_auth import authorized,_session
            row=_session(request)
            if row and row['role']=='viewer':return JSONResponse({'detail':'Developer authorization required'},status_code=403)
            if not authorized(request,request.headers.get('x-capstone-developer-token')):return RedirectResponse('/testing-lab-login?next=/trace',status_code=303)
            return FileResponse(os.path.join(STATIC_DIR, 'trace.html'))

        @app.get('/pressure-testing')
        def pressure_testing(request: Request):
            if 'developer_token' in request.query_params:raise __import__('fastapi').HTTPException(403,'Credentials in URLs are forbidden')
            return FileResponse(os.path.join(STATIC_DIR, 'pressure_testing.html'),headers={'Cache-Control':'no-cache, max-age=0, must-revalidate'})

        @app.get('/testing-lab-login')
        def testing_lab_login():
            return FileResponse(os.path.join(STATIC_DIR,'testing_lab_login.html'))

        # The Agent Arena UI is its own project (PycharmProjects/AgentArena). Capstone
        # only reports its activity (telemetry.emit -> arena_events, /api/arena/*).

    return app


def main(host: str = "127.0.0.1", port: int = 8000, reload: bool = False,
         prefer_llm: Optional[bool] = None):
    import uvicorn
    app = create_app(prefer_llm=prefer_llm)
    # One worker. Two would mean two inference queues and two arrival
    # simulators drawing from the same ticket pool.
    uvicorn.run(app, host=host, port=port, log_level="info",
                workers=1, reload=reload)
