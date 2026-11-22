"""Persistent, validated Gateway service configuration (schema inference-gateway.service-config/1).

The file holds paths and policy only, never secrets. Validation is strict:
unknown keys, relative paths, plaintext on a LAN address, a missing ledger or an
unloadable certificate are refused before the service binds anything.
"""
import ipaddress
import json
import ssl
import urllib.parse
from pathlib import Path

SCHEMA='inference-gateway.service-config/1'
# 0.4.1 defaults for 9 heads x up to 10 slots (90 concurrent calls) and 32+ flows: see docs/RELEASE-NOTES-0.4.1.md (item A3).
DEFAULTS={'service_id':'shared-inference','limits':{'max_connections':256,'max_pending':512},
          'upstream_tls':{'ca':None,'certificate':None,'key':None},'shutdown':{'drain_seconds':90},
          'logging':{'max_bytes':10*1024*1024,'backup_count':10},'health_interval_seconds':15,
          'observation':{'output':None,'state':None,'interval_seconds':5,'overlap':100,'limit':1000}}
KEYS={'schema','service_id','ledger','listen','advertised_url','tls','upstream_tls','limits','shutdown','logging','health_interval_seconds','observation'}


class ConfigError(ValueError):
    def __init__(self,problems):super().__init__('; '.join(problems));self.problems=problems


def _merge(base,override):
    out=dict(base)
    for key,value in override.items():out[key]=_merge(base[key],value) if isinstance(value,dict) and isinstance(base.get(key),dict) else value
    return out


def load(path,*,check_files=True):
    raw=json.loads(Path(path).read_text(encoding='utf-8'))
    problems=[]
    if not isinstance(raw,dict):raise ConfigError(['Configuration must be a JSON object'])
    if raw.get('schema')!=SCHEMA:problems.append('schema must be '+SCHEMA)
    problems+=['unknown key: '+k for k in sorted(set(raw)-KEYS)]
    settings=_merge(DEFAULTS,raw)
    def absolute(value,name,*,optional=False,exists=None):
        if value is None and optional:return
        if not isinstance(value,str) or not Path(value).is_absolute():problems.append(name+' must be an absolute path');return
        if check_files and exists=='file' and not Path(value).is_file():problems.append(name+' does not exist: '+value)
        if check_files and exists=='dir' and not Path(value).is_dir():problems.append(name+' folder does not exist: '+value)
    absolute(settings.get('ledger'),'ledger',exists='file')
    listen=settings.get('listen') or {}
    host,port=listen.get('host'),listen.get('port')
    try:loopback=ipaddress.ip_address(host).is_loopback
    except (ValueError,TypeError):problems.append('listen.host must be an IP literal');loopback=True
    if isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535:problems.append('listen.port must be 1–65535')
    tls=settings.get('tls') or {}
    if not loopback and not (tls.get('certificate') and tls.get('key')):problems.append('A non-loopback listener requires tls.certificate and tls.key')
    for name in ('certificate','key'):absolute(tls.get(name),'tls.'+name,optional=True,exists='file')
    for name in ('ca','certificate','key'):absolute(settings['upstream_tls'].get(name),'upstream_tls.'+name,optional=True,exists='file')
    advertised=urllib.parse.urlsplit(settings.get('advertised_url') or '')
    if advertised.scheme!=('https' if tls.get('certificate') else 'http') or advertised.path not in ('','/') or advertised.query or not advertised.hostname:
        problems.append('advertised_url must be an origin using the listener protocol')
    elif advertised.port!=port:problems.append('advertised_url port must match listen.port')
    limits=settings['limits']
    if any(isinstance(limits.get(k),bool) or not isinstance(limits.get(k),int) or limits[k]<1 for k in ('max_connections','max_pending')):problems.append('limits must be positive integers')
    drain=settings['shutdown'].get('drain_seconds')
    if isinstance(drain,bool) or not isinstance(drain,(int,float)) or not 0<=drain<=900:problems.append('shutdown.drain_seconds must be 0–900')
    logging=settings['logging'];absolute(logging.get('directory'),'logging.directory',exists='dir')
    if not isinstance(logging.get('max_bytes'),int) or not 64*1024<=logging['max_bytes']<=1024**3 or not isinstance(logging.get('backup_count'),int) or not 1<=logging['backup_count']<=100:
        problems.append('logging.max_bytes 64 KiB–1 GiB and backup_count 1–100 required')
    if not 5<=settings['health_interval_seconds']<=60:problems.append('health_interval_seconds must be 5–60')
    observation=settings['observation']
    if observation.get('output') or observation.get('state'):
        # In-service publisher: its freshness then also proves the service is running.
        absolute(observation.get('output'),'observation.output');absolute(observation.get('state'),'observation.state')
        if not 1<=observation['interval_seconds']<=30 or not 0<=observation['overlap']<observation['limit']<=1000:problems.append('observation interval 1–30 s and overlap < limit <= 1000 required')
        if check_files and not problems:
            from .observation_publisher import check_layout
            try:check_layout(settings['ledger'],observation['output'],observation['state'])
            except ValueError as exc:problems.append('observation: '+str(exc))
    if check_files and not problems and tls.get('certificate'):
        try:ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(tls['certificate'],tls['key'])
        except (ssl.SSLError,OSError) as exc:problems.append('TLS certificate/key cannot be loaded: '+type(exc).__name__)
        else:
            sans=_sans(tls['certificate'])
            if sans is not None and advertised.hostname not in sans:problems.append('certificate SAN does not cover the advertised host '+advertised.hostname)
    if problems:raise ConfigError(problems)
    return settings


def _sans(certificate):
    """Certificate SAN values (IP and DNS), or None if this Python cannot decode them."""
    try:decoded=ssl._ssl._test_decode_cert(certificate)
    except Exception:return None
    return {value for kind,value in decoded.get('subjectAltName',()) if kind in ('IP Address','DNS')}


def summary(settings):
    """Non-secret view for logs and validation output."""
    return {'service_id':settings['service_id'],'ledger':settings['ledger'],'listen':settings['listen'],'advertised_url':settings['advertised_url'],
            'tls':bool(settings.get('tls',{}).get('certificate')),'upstream_tls':{k:bool(v) for k,v in settings['upstream_tls'].items()},
            'limits':settings['limits'],'drain_seconds':settings['shutdown']['drain_seconds'],'log_directory':settings['logging']['directory'],
            'observation_publisher':'in-service' if settings['observation'].get('output') else 'external or disabled'}
