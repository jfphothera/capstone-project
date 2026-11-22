"""Synthetic, bounded technical-capability fixtures for head qualification.

Each fixture has one exact expected result. A capability passes only when the
response matches it exactly; resemblance, partial JSON or extra calls fail.
Fixtures are synthetic: no client prompt, credential or customer data. These
prove technical compatibility on one head, never task suitability.
"""
import base64
import hashlib
import json
import struct
import uuid
import zlib

VERSION=1
TOOL={'type':'function','function':{'name':'record_value','description':'Record one integer value.',
      'parameters':{'type':'object','properties':{'value':{'type':'integer'}},'required':['value']}}}
SCHEMA={'type':'object','properties':{'status':{'type':'string','enum':['READY']},'count':{'type':'integer'}},
        'required':['status','count'],'additionalProperties':False}
CODE_WORD='MARIGOLD-7'
CHAT_SCHEMA={'type':'object','properties':{'reply':{'type':'string','enum':['READY']}},'required':['reply'],'additionalProperties':False}


def _png(width=32,height=32,rgb=(255,0,0)):
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    rows=b''.join(b'\x00'+bytes(rgb)*width for _ in range(height))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')


def _options(max_context,num_predict=24):
    return {'num_ctx':max_context,'num_predict':num_predict,'temperature':0,'seed':42}


def _exact_int(value,expected):
    return isinstance(value,int) and not isinstance(value,bool) and value==expected


def _chat(response):
    content=(response.get('message') or {}).get('content')
    return content.strip()=='READY',{'content_matches':isinstance(content,str) and content.strip()=='READY'}


def _tools(response):
    calls=(response.get('message') or {}).get('tool_calls')
    if not isinstance(calls,list) or len(calls)!=1 or not isinstance(calls[0],dict):return False,{'tool_calls':len(calls) if isinstance(calls,list) else 0}
    function=calls[0].get('function') or {};arguments=function.get('arguments')
    if isinstance(arguments,str):
        try:arguments=json.loads(arguments)
        except ValueError:return False,{'tool_calls':1,'arguments':'unparseable'}
    passed=function.get('name')=='record_value' and isinstance(arguments,dict) and set(arguments)=={'value'} and _exact_int(arguments['value'],42)
    return passed,{'tool_calls':1,'name_matches':function.get('name')=='record_value','arguments_match':passed}


def _structured(response):
    try:value=json.loads(response.get('response',''))
    except (TypeError,ValueError):return False,{'json':False}
    passed=isinstance(value,dict) and set(value)=={'status','count'} and value['status']=='READY' and _exact_int(value['count'],3)
    return passed,{'json':True,'schema_match':passed}


def _structured_chat(response):
    """0.4.6: the reply's message content is exactly {"reply": "READY"} (the schema allows nothing else)."""
    content=(response.get('message') or {}).get('content')
    try:value=json.loads(content) if isinstance(content,str) else None
    except ValueError:value=None
    passed=isinstance(value,dict) and set(value)=={'reply'} and value['reply']=='READY'
    return passed,{'json':isinstance(value,dict),'schema_match':passed,'tool_calls':len((response.get('message') or {}).get('tool_calls') or [])}


def _vision(response):
    answer=str(response.get('response','')).strip().strip('.').lower()
    return answer=='red',{'answer_matches':answer=='red'}


def _context(max_context):
    minimum=int(max_context*.4)
    def check(response):
        answer=str(response.get('response','')).strip().strip('.')
        evaluated=response.get('prompt_eval_count')
        # A truncated prompt loses the code word or reports too few evaluated tokens.
        used=isinstance(evaluated,int) and not isinstance(evaluated,bool) and evaluated>=minimum
        return answer==CODE_WORD and used,{'answer_matches':answer==CODE_WORD,'prompt_eval_count':evaluated if isinstance(evaluated,int) else None,'minimum_prompt_tokens':minimum}
    return check


def build(purpose,model,max_context):
    """(operation, payload, check, record) for one capability purpose."""
    nonce=uuid.uuid4().hex # defeats prompt caching; recorded, never reused
    if purpose=='chat':
        operation='/api/chat';payload={'messages':[{'role':'user','content':'Return exactly the word READY.'}],'options':_options(max_context)};check=_chat
    elif purpose=='tools':
        operation='/api/chat';check=_tools
        payload={'messages':[{'role':'user','content':'Call the record_value tool with value 42. Do not answer in text.'}],'tools':[TOOL],'options':_options(max_context,128)}
    elif purpose=='structured_output':
        operation='/api/generate';check=_structured
        payload={'prompt':'Return a JSON object whose status is READY and whose count is 3.','format':SCHEMA,'options':_options(max_context,64)}
    elif purpose=='structured_chat':
        # 0.4.6: system and user messages and a closed JSON schema, as Capstone's structured roles send them
        # (customer_communication: {"reply": string}, additionalProperties false). Synthetic content only.
        operation='/api/chat';check=_structured_chat
        payload={'messages':[{'role':'system','content':'You answer only with a JSON object that matches the requested schema.'},
                             {'role':'user','content':'Return a JSON object whose reply is READY.'}],
                 'format':CHAT_SCHEMA,'options':_options(max_context,64)}
    elif purpose=='vision':
        operation='/api/generate';check=_vision
        payload={'prompt':'What single color fills this image? Answer with one lowercase word.','images':[base64.b64encode(_png()).decode()],'options':_options(max_context,8)}
    elif purpose=='context':
        operation='/api/generate';check=_context(max_context)
        filler=' '.join(['alpha']*int(max_context*.5))
        payload={'prompt':f'Run {nonce}. The code word is {CODE_WORD}.\n{filler}\nWhat is the code word? Answer with the code word only.','options':_options(max_context,16)}
    else:raise ValueError('Unknown capability qualification purpose')
    canonical=json.dumps({'operation':operation,'payload':payload},sort_keys=True).replace(nonce,'<nonce>')
    record={'id':purpose+'/'+str(VERSION),'sha256':hashlib.sha256(canonical.encode()).hexdigest(),'nonce':nonce,'max_context':max_context}
    # Same dispatch controls as the existing completion qualification.
    payload={**payload,'model':model,'stream':False,'keep_alive':'2m','think':False}
    return operation,payload,check,record
