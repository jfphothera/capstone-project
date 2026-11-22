"""Managed Gateway runtime shared by the Windows service host and console mode.

start(): exclusive ledger owner, authority claim, resume a service-made drain,
restart reconciliation without replay, TLS listener, enrolled-head health
refresh. stop(): reject new submissions, persistent service-stop drain, bounded
wait for active allocations, close the listener, release ownership. Logs carry
lifecycle metadata and sanitized capacity event types only: never prompts,
outputs, tokens or keys.
"""
import json
import logging
import logging.handlers
import threading
import time
import traceback
from pathlib import Path
from .capacity_gateway import Gateway
from .capacity_gateway_server import Server,server_tls
from . import capacity_lifecycle as lifecycle

LOGGED_EVENTS_PER_CYCLE=500


def service_logger(settings,name='inference_gateway.service'):
    log=logging.getLogger(name);log.setLevel(logging.INFO);log.propagate=False
    if not any(getattr(h,'gateway_service',False) for h in log.handlers):
        handler=logging.handlers.RotatingFileHandler(Path(settings['logging']['directory'])/'gateway-service.log',
            maxBytes=settings['logging']['max_bytes'],backupCount=settings['logging']['backup_count'],encoding='utf-8')
        handler.gateway_service=True;handler.setFormatter(logging.Formatter('%(message)s'));log.addHandler(handler)
    return log


class GatewayRuntime:
    RESULT_GRACE_SECONDS=5
    HANDLER_GRACE_SECONDS=2

    def __init__(self,settings,log=None,*,transport=None):
        self.settings=settings;self.log=log or service_logger(settings);self.transport=transport
        self.gateway=self.server=None;self._owner=None;self._threads=[];self._stopped=threading.Event()
        self.failure=None

    def emit(self,event,**fields):
        self.log.info(json.dumps({'at':time.time(),'event':event,**fields},default=str))

    def start(self):
        s=self.settings;self.emit('service.starting',service_id=s['service_id'],listen=s['listen'])
        self._owner=lifecycle.service_owner(s['ledger']);self._owner.__enter__()
        try:
            up=s['upstream_tls']
            self.gateway=Gateway(s['ledger'],transport=self.transport,max_pending=s['limits']['max_pending'],
                upstream_ca=up['ca'],upstream_certificate=up['certificate'],upstream_key=up['key'])
            broker=self.gateway.broker
            self.gateway.emit=self.emit # 0.4.1: lease renewal and scheduler failures go to the service log
            lifecycle.claim_authority(broker,s['service_id'])
            if lifecycle.resume_after_service_stop(broker):self.emit('service.admission_resumed_after_stop')
            recovered=self.gateway.recover_service_startup();self.emit('service.recovered',**recovered)
            # 0.4.4: the history size every unindexed scan used to pay for (and the WAL size), once per start.
            try:self.emit('ledger.opened',**broker.ledger_size())
            except Exception as exc:self.emit('ledger.size_failed',error=type(exc).__name__)
            tls=s.get('tls') or {}
            self.server=Server((s['listen']['host'],s['listen']['port']),self.gateway,s['service_id'],s['limits']['max_connections'],
                tls=server_tls(tls['certificate'],tls['key']) if tls.get('certificate') else None)
            with broker.connect() as db:self._cursor=db.execute('SELECT coalesce(max(event_id),0) FROM capacity_events').fetchone()[0]
            self._spawn(self._serve,'gateway-listener');self._spawn(self._monitor,'enrolled-resource-health')
            self._spawn(self._recover,'gateway-recovery')
            if s['observation'].get('output'):
                # Isolated from inference: its failures are logged, never stop the service.
                self._publisher=threading.Thread(target=self._publish,daemon=True,name='observation-publisher');self._publisher.start()
            self.emit('service.listening',host=s['listen']['host'],port=self.server.server_port,protocol='https' if tls.get('certificate') else 'http',
                      admission_open=lifecycle.status(broker)['admission_open'])
        except BaseException:
            self.emit('service.start_failed',error=traceback.format_exc(limit=8))
            self._release();raise
        return self

    def _spawn(self,target,name):
        thread=threading.Thread(target=target,daemon=True,name=name);thread.start();self._threads.append(thread)

    def _serve(self):
        try:self.server.serve_forever()
        except BaseException as exc:
            if not self._stopped.is_set():self.failure=exc;self.emit('service.failed',error=traceback.format_exc(limit=8))

    def _publish(self):
        from .observation_publisher import run
        o=self.settings['observation']
        try:run(self.settings['ledger'],o['output'],o['state'],service_id=self.settings['service_id'],interval=o['interval_seconds'],
                overlap=o['overlap'],limit=o['limit'],stop=self._stopped,log=self.log)
        except Exception:self.emit('observation.publisher_failed',error=traceback.format_exc(limit=4))

    def _monitor(self):
        while True:
            try:self.gateway.refresh_health()
            except Exception:pass # stale freshness independently stops admission
            try:self._log_capacity_events()
            except Exception:pass
            try:self.gateway.publish_counters(**self.settings['limits'])
            except Exception:pass
            if self._stopped.wait(self.settings['health_interval_seconds']):return

    def _recover(self):
        """0.4.0: probe due suspensions and holds. Separate from health refresh: a probe may take up to its timeout."""
        while True:
            try:self.gateway.run_recovery()
            except Exception:self.emit('recovery.cycle_failed',error=traceback.format_exc(limit=4))
            if self._stopped.wait(self.settings['health_interval_seconds']):return

    def _log_capacity_events(self):
        with self.gateway.broker.connect() as db:
            rows=db.execute('SELECT event_id,app_id,request_id,event_type FROM capacity_events WHERE event_id>? ORDER BY event_id LIMIT ?',
                (self._cursor,LOGGED_EVENTS_PER_CYCLE)).fetchall()
        for row in rows:
            # Sanitized: identifiers and type only, the same fields Arena receives.
            self.emit('capacity.'+row['event_type'],event_id=row['event_id'],app_id=row['app_id'],request_id=row['request_id'])
            self._cursor=row['event_id']

    def running(self):
        return self.server is not None and not self._stopped.is_set() and self.failure is None and all(t.is_alive() for t in self._threads)

    def stop(self,*,progress=None,drain_seconds=None):
        drain=self.settings['shutdown']['drain_seconds'] if drain_seconds is None else drain_seconds
        broker=self.gateway.broker;self.emit('service.stop_requested',drain_seconds=drain)
        self.server.stopping=True
        drained=lifecycle.service_stop_drain(broker)
        deadline=time.monotonic()+drain;state=lifecycle.status(broker)
        while state['active_allocations'] and time.monotonic()<deadline:
            if progress:progress(state)
            time.sleep(min(1,max(0,deadline-time.monotonic())));state=lifecycle.status(broker)
            self.emit('service.drain_progress',active_allocations=state['active_allocations'],queued_requests=state['queued_requests'])
        # 0.4.4: work that finished during the drain is durable, but its caller polls every 0.25-2 s. Keep the
        # listener up a little longer (at most RESULT_GRACE_SECONDS, within the drain budget) so callers collect
        # their results instead of retrying against a stopped service until their deadline.
        grace=min(time.monotonic()+self.RESULT_GRACE_SECONDS,deadline)
        while not state['active_allocations'] and self.gateway.unread_results() and time.monotonic()<grace:time.sleep(.2)
        # 0.4.6: a status read counts as collected when its handler starts; let running handlers send their replies
        # (at most HANDLER_GRACE_SECONDS) so a result read just before the stop is not lost on a closed connection.
        # Seen as test_graceful_stop_drains_in_flight_work... failing 3 of 4 runs on 0.4.5 on the development laptop.
        handlers_end=time.monotonic()+self.HANDLER_GRACE_SECONDS
        while self.server.active_handlers() and time.monotonic()<handlers_end:time.sleep(.05)
        unread=self.gateway.unread_results()
        if unread:self.emit('service.stop_results_unread',results=unread)
        self._stopped.set()
        try:self.server.shutdown();self.server.server_close()
        finally:
            for thread in self._threads+([self._publisher] if getattr(self,'_publisher',None) else []):thread.join(10)
            self._release()
        summary={'drained_by_service':drained,'active_allocations_remaining':state['active_allocations'],
                 'queued_requests_remaining':state['queued_requests'],'uncertain_resources':state['uncertain_resources'],
                 'note':'remaining allocations become unknown at next start and are never replayed' if state['active_allocations'] else None}
        self.emit('service.stopped',**summary)
        return summary

    def _release(self):
        if self._owner:
            owner,self._owner=self._owner,None;owner.__exit__(None,None,None)
