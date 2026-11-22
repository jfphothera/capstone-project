"""Interpret observed message content, never hidden archetypes or evaluator labels."""
import re

PHONE = (
 ('callback_request',r'(?:automated call-back request|call.?back request|please call me back)'),
 ('voicemail_without_transcript',r'voicemail received.{0,100}transcription not available'),
 ('phone_inquiry_without_transcript',r'(?:inquiry captured by phone ai|no further transcript recorded)'),
 ('ivr_refill_selection',r'caller selected refill-code option'),
)

def interpret(body,channel='unknown',message_id=None):
    kind='customer_request';origin='unknown';reply='draft_for_review';task=None;evidence=[]
    for name,pattern in PHONE:
        hit=re.search(pattern,body,re.I|re.S)
        if hit:
            kind=name;origin='phone';reply='not_now'
            task='callback' if name=='callback_request' else 'review_phone_record'
            evidence=[{'message_id':message_id,'start':hit.start(),'end':hit.end(),'quote':hit.group(0)}]
            break
    if kind=='customer_request':
        hit=re.search(r'\b(?:internal task|no (?:customer )?response (?:is )?(?:needed|required)|documentation only|do not (?:email|reply))\b',body,re.I)
        completed=re.search(r'\b(?:call completed|issue (?:was |is )?resolved|customer confirmed (?:the )?issue resolved)\b',body,re.I)
        if completed and not re.search(r'\b(?:call|phone|spoke)\b',body,re.I): completed=None
        if hit or completed:
            match=hit or completed;kind='internal_task' if hit else 'completed_call_record';reply='no_reply'
            task='internal_review' if hit else None
            evidence=[{'message_id':message_id,'start':match.start(),'end':match.end(),'quote':match.group(0)}]
    intents=[]
    for name,pattern in [('callback',r'call.?back'),('prior_authorization',r'prior authorization'),('shipping',r'track|shipping|shipped|order status'),('refill',r'refill'),('manual',r'guide|manual'),('missing_item',r'missing|wasn.t included|weren.t included'),('device_fault',r'not working|dark|stopped|fault|broken'),('return',r'return|repair'),('refund',r'refund')]:
        for match in list(re.finditer(pattern,body,re.I))[:1]:
            intents.append({'kind':name,'explicit':True,'evidence':{'message_id':message_id,'start':match.start(),'end':match.end(),'quote':match.group(0)}})
    return {'schema_version':1,'transport_channel':channel,'reported_origin':origin,'message_type':kind,
        'actionable_customer_request':'unknown' if 'without_transcript' in kind or kind=='ivr_refill_selection' else 'yes' if kind=='customer_request' else 'no',
        'outbound_message':{'decision':reply,'reason':'Observed customer request' if reply=='draft_for_review' else 'Document/review the observed record; do not invent a customer conversation'},
        'contact_task':task,'intents':intents,'evidence':evidence,'method':'source_content_rules_v1'}

def validate_finding(finding,body):
    spans=finding.get('evidence',[])+[i['evidence'] for i in finding.get('intents',[])]
    return all(body[s['start']:s['end']]==s['quote'] and 0<=s['start']<s['end']<=len(body) for s in spans)
