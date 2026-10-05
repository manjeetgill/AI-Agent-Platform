"""Validated clinic choices rendered as native WhatsApp controls."""
import re

def interactive_reply(text, options):
    if options is None:
        return None
    if not isinstance(options, dict) or not isinstance(options.get('button'), str) or not 1 <= len(options['button']) <= 20:
        raise ValueError('Invalid choice menu')
    choices = options.get('choices')
    if not isinstance(choices, list) or not 1 <= len(choices) <= 10:
        raise ValueError('Invalid choice count')
    ids = set()
    for c in choices:
        if not isinstance(c, dict) or not re.fullmatch(r'sc:[a-f0-9]{20}:\d', c.get('id', '')):
            raise ValueError('Invalid choice id')
        if c['id'] in ids or not isinstance(c.get('title'), str) or not 1 <= len(c['title']) <= 24:
            raise ValueError('Invalid choice title')
        ids.add(c['id'])
        if 'description' in c and (not isinstance(c['description'], str) or len(c['description']) > 72):
            raise ValueError('Invalid choice description')
    if not text or len(text) > 1024:
        return None  # Preserve the full text when it exceeds interactive body limits.
    if len(choices) <= 3 and all(len(c['title']) <= 20 and not c.get('description') for c in choices):
        return {'type':'button','body':{'text':text},'action':{'buttons':[{'type':'reply','reply':{'id':c['id'],'title':c['title']}} for c in choices]}}
    return {'type':'list','body':{'text':text},'action':{'button':options['button'],'sections':[{'rows':[{k:c[k] for k in ('id','title','description') if k in c} for c in choices]}]}}
