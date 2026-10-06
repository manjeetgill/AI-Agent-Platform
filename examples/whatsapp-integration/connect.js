// Mount in your signed-in app. Proxy these routes through YOUR authenticated
// backend to /v1/whatsapp/onboarding[/config|/:id]. Never expose service keys.
// Pass a storageKey unique to the signed-in business; clear it on sign-out.
export async function connectWhatsApp(container, status, backend = '/api/whatsapp', storageKey) {
  let stopped = false, timer;
  const key = storageKey ? 'meta-signup:' + storageKey : null;
  async function request(path, body) {
    const response = await fetch(backend + path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error('Connection unavailable. Check saved progress before starting again.');
    return response.json();
  }
  async function poll(id) {
    if (stopped) return;
    try {
      const result = await request('/onboarding/' + encodeURIComponent(id));
      if (stopped) return;
      status.textContent = result.message;
      if (['CONNECTED', 'CANCELLED', 'REAUTH_REQUIRED', 'DISCONNECTED'].includes(result.state)) {
        if (key) sessionStorage.removeItem(key);
        return;
      }
      // Action-required states are handled on the hosted connection page.
      if (['CONNECTING', 'EXCHANGING', 'WAITING_ASSETS', 'QUEUED', 'ASSETS_DISCOVERED',
        'PHONE_CONNECTING', 'WEBHOOK_CONFIGURING', 'VERIFYING'].includes(result.state))
        timer = setTimeout(() => poll(id), 3000);
    } catch (error) { if (!stopped) status.textContent = error.message; }
  }
  container.replaceChildren();
  const saved = key && sessionStorage.getItem(key);
  if (saved) {
    status.textContent = 'Checking your saved connection…';
    await poll(saved);
  } else {
    const config = await request('/onboarding/config');
    status.textContent = config.message;
    if (config.available) {
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = 'Connect WhatsApp';
      container.append(button);
      button.onclick = async () => {
        button.disabled = true;
        try {
          const session = await request('/onboarding', {});
          if (key) sessionStorage.setItem(key, session.id);
          if (stopped) return;
          const url = new URL(session.connect_url);
          if (url.protocol !== 'https:' || url.origin !== new URL(config.public_url).origin)
            throw new Error('Connection address rejected. Contact your setup team.');
          const link = document.createElement('a');
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
          link.textContent = 'Continue securely with Meta';
          container.replaceChildren(link);
          // Explicit link avoids popup blockers. Hosted page handles Meta SDK,
          // credentials, OTP and retry. Never store the capability URL locally.
          await poll(session.id);
        } catch (error) {
          status.textContent = error.message;
          // No automatic retry of a potentially successful session creation.
        }
      };
    }
  }
  return () => { stopped = true; clearTimeout(timer); container.replaceChildren(); };
}
