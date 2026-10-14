// Mount in your signed-in app. Proxy through YOUR authenticated, CSRF-protected
// backend with a business-scoped service key. Never expose that key in JavaScript.
// Supply a unique storageKey per business and clear it at logout.
export async function connectWhatsApp(container, status, backend = '/api/whatsapp', storageKey = 'business') {
  const savedKey = 'chakra-signup:' + storageKey;
  let widget = null, disposed = false, busy = false;
  async function request(path, body) {
    const response = await fetch(backend + path, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
      signal: AbortSignal.timeout(20000),
    });
    const result = await response.json();
    if (!response.ok) {
      // The platform returns only the owning business's previous session ID.
      if (response.status === 409 && result.detail?.session_id) {
        sessionStorage.setItem(savedKey, result.detail.session_id);
      }
      throw new Error('Connection could not complete. Check saved status before retrying.');
    }
    return result;
  }
  function button(text, action) {
    const element = document.createElement('button'); element.textContent = text;
    element.onclick = async () => {
      if (busy || disposed) return;
      busy = true; element.disabled = true;
      try { await action(); } catch (error) { status.textContent = error.message; }
      finally { busy = false; element.disabled = false; }
    };
    container.append(element);
  }
  async function open(session) {
    if (!window.ChakraWhatsappConnect) {
      await new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = 'https://embed.chakrahq.com/whatsapp-partner-connect/v1_0_1/sdk.js';
        script.onload = resolve;
        script.onerror = () => reject(new Error('Chakra could not load. Check saved status later.'));
        document.head.appendChild(script);
      });
    }
    if (disposed) return;
    widget?.destroy(); container.replaceChildren();
    widget = window.ChakraWhatsappConnect.init({
      connectToken: session.connect_token, container,
      onReady: () => { if (!disposed) status.textContent = 'Connect your business number with Facebook.'; },
      onError: () => { if (!disposed) status.textContent = 'Connection interrupted. Check saved status before restarting.'; },
      onSuccess: async data => {
        if (disposed) return;
        try {
          await request(`/onboarding/${encodeURIComponent(session.id)}/complete`, {plugin_id: data.pluginId});
          await refresh();
        } catch { if (!disposed) status.textContent = 'Authorization received, but confirmation is pending. Check saved status.'; }
      },
    });
    button('Check saved status', refresh);
  }
  async function refresh() {
    const id = sessionStorage.getItem(savedKey);
    const saved = id ? await request('/onboarding/' + encodeURIComponent(id)) : null;
    if (disposed) return;
    widget?.destroy(); widget = null; container.replaceChildren();
    if (!saved) {
      status.textContent = 'Connect your business WhatsApp number.';
      button('Connect WhatsApp', async () => {
        const session = await request('/onboarding', {});
        sessionStorage.setItem(savedKey, session.id); // Token stays in memory only.
        await open(session);
      });
    } else {
      status.textContent = saved.message;
      if (saved.next_action === 'connected') sessionStorage.removeItem(savedKey);
      if (['resume', 'renew'].includes(saved.next_action)) {
        button(saved.next_action === 'renew' ? 'Renew reviewed authorization' : 'Continue authorization', async () => {
          await open(await request(`/onboarding/${encodeURIComponent(id)}/${saved.next_action}`, {}));
        });
      }
    }
    button('Check saved status', refresh);
  }
  await refresh();
  return () => { disposed = true; widget?.destroy(); container.replaceChildren(); };
}
