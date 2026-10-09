"""Configure one app's live email privately; never sends a test or changes other routes."""
import argparse
import fcntl
import getpass
import json
import os
import sys
from uuid import UUID

from local import CONFIG, ROOT, load, save

sys.path.insert(0, str(ROOT / 'src'))
from agent_platform.email_config import Config, Limits, digest, keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-id', required=True, type=UUID)
    parser.add_argument('--provider', required=True, choices=['resend', 'brevo'])
    parser.add_argument('--sender', required=True, help='Verified sender email address')
    parser.add_argument('--sender-name', default='Notifications')
    parser.add_argument('--link-host', action='append', default=[])
    parser.add_argument('--daily', type=int, default=10)
    parser.add_argument('--monthly', type=int, default=100)
    parser.add_argument('--per-minute', type=int, default=5)
    parser.add_argument('--public-url', help='Existing public HTTPS platform origin; not a tunnel creation command')
    args = parser.parse_args()
    from urllib.parse import urlsplit
    origin = args.public_url.rstrip('/') if args.public_url else None
    if origin:
        url = urlsplit(origin)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
            parser.error('Use a public HTTPS origin without a path, credentials or query.')
    app = str(args.app_id)
    key_env = 'AI_EMAIL_KEY_' + args.app_id.hex.upper()
    secret_env = 'AI_EMAIL_WEBHOOK_' + args.app_id.hex.upper() + '_' + args.provider.upper()
    settings = Config(provider=args.provider, from_email=args.sender, from_name=args.sender_name,
                      key_env=key_env, allowed_link_hosts=args.link_host,
                      limits=Limits(daily=args.daily, monthly=args.monthly, per_minute=args.per_minute))
    print('This switches only the selected app to live email after service restart. Provider fees may apply.')
    key = getpass.getpass('Provider API key (hidden): ').strip()
    webhook = getpass.getpass('Webhook signing secret (Resend) or private bearer token (Brevo), hidden; blank preserves existing: ').strip()
    if not key or any(c in key for c in '\r\n'):
        raise ValueError('A single-line provider key is required.')
    # Lock and reload after prompts so another app's recently saved settings survive.
    lock = os.open(str(CONFIG)+'.email.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        values = load()
        os.environ.update(values)
        keys(); digest({})  # Preserve the existing encryption keyring; never silently replace it.
        routes = json.loads(values.get('AI_EMAIL_APPS', '{}'))
        if app not in routes:
            raise ValueError('Configure this application in the platform first; no existing email route was found.')
        routes[app] = settings.model_dump(exclude_none=True)
        values['AI_EMAIL_APPS'] = json.dumps(routes)
        values[key_env] = key
        if webhook:
            from agent_platform.email_delivery import webhook_ready
            hooks = json.loads(values.get('AI_EMAIL_WEBHOOKS', '{}'))
            hooks.setdefault(app, {})[args.provider] = {'key_env': secret_env}
            values['AI_EMAIL_WEBHOOKS'] = json.dumps(hooks)
            values[secret_env] = webhook
            os.environ.update(values)
            if not webhook_ready(app, args.provider):
                raise ValueError('Webhook authentication secret is invalid; nothing was saved.')
        save(CONFIG, values)
    finally:
        os.close(lock)
    print('Saved privately. Restart API and worker together. No email sent; provider domain verification remains external.')
    if origin:
        print('Register callback: '+origin+'/v1/notifications/email/webhooks/'+app+'/'+args.provider)
    print('Open /monitor/email and check configuration, limits and webhook status before sending one approved receipt test.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, TypeError, KeyError):
        raise SystemExit('Configuration invalid. Check the existing app route, encryption settings, sender and private credentials; no configuration was saved.') from None
