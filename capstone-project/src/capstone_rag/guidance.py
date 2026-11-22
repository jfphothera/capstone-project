"""Independently authorized fictional general information and model aliases.

These capabilities clarify the request and locate the label. They do not authorize
operating steps or disclose account records. Existing controlled procedures keep
their exact model/revision and customer audience restrictions.
"""
import re

from .model_catalog import MODEL_ALIASES, resolve_reference


def tentative_model(text):
    reference = resolve_reference(text)
    if not reference:
        return None
    return {'model': reference.canonical,
            'raw_model_reference': reference.raw,
            'status': ('tentative_model_only' if reference.supported
                       else 'unsupported_model_reference'),
            'device_identified': False, 'revision': None}


def administrative(text):
    return bool(re.search(r'\b(refill|unlock|insurance|billing|training|guide|manual|how.*work)\b', text, re.I)) and not specific_symptom(text)


def specific_symptom(text):
    return bool(re.search(r'no power|won.?t (?:turn|power)|will not (?:turn|power)|completely dead|no lights|lamp|flicker|dim|timer|overheat|screen.*code|missing.*box', text, re.I))


LABEL_GUIDANCE = ('The serial number is printed on the device identification label. '
                  'Please read the label without opening the unit or removing any covers. '
                  'If you cannot find or read it, tell me and we can arrange another way to help.')
SYMPTOM_QUESTION = ('Could you tell me a little more about what the device is doing? '
                    'Does it fail to turn on completely, or does it turn on but the lamps stay off? '
                    'Please also mention any message on the display.')


def authorization(verified, device, revision_confirmed):
    return {
        'policy': 'fictional-support-scope-v1',
        'general_label_and_symptom_questions': True,
        'identity_verified': bool(verified),
        'exact_device_identified': bool(device),
        'hardware_revision_confirmed': bool(revision_confirmed),
        'public_model_operating_manual': False,
        'model_or_revision_troubleshooting': bool(verified and device and revision_confirmed),
        'account_specific_information': bool(verified),
        'consequential_movement': 'requires separate named human approval',
        'service_only_customer_instructions': False,
        'audit_only_customer_instructions': False,
        'note': 'Troubleshooting eligibility still requires a current customer-audience procedure and compatibility authorization.',
    }
