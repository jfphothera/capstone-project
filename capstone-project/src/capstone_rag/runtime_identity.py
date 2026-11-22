"""Logical process identity, distinct from transport addresses and machine names."""
import os
import re
import socket
import uuid

_INSTANCE=uuid.uuid4().hex

def identity():
    app=os.environ.get('CAPSTONE_APPLICATION_ID','capstone')
    orchestrator=os.environ.get('CAPSTONE_ORCHESTRATOR_ID','main')
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',value) for value in (app,orchestrator)):
        raise ValueError('Logical application/orchestrator IDs must use lowercase letters, digits and dashes')
    return {'application_id':app,'application_display_name':'Ticketing' if app=='capstone' else app,
            'orchestrator_id':orchestrator,'coordinator_id':f'orch:{app}/{orchestrator}',
            'application_instance_id':os.environ.get('CAPSTONE_INSTANCE_ID',_INSTANCE),
            'execution_host':socket.gethostname(),'execution_process_id':os.getpid()}

def coordinator_id():return identity()['coordinator_id']
